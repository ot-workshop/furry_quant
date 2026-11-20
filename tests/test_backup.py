"""Archive validation rejects partial, corrupted and unsafe backups before any restore."""

import contextlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

import finance_quant.backup as backup_module
from finance_quant.backup import FORMAT, restore, verify
from finance_quant.checksums import file_sha256, sha256_bytes


def archive(tmp_path, name="data/financials/current.json", payload=b'{"id":"saved"}'):
    (tmp_path / "database.dump").write_bytes(b"test archive")
    with tarfile.open(tmp_path / "storage.tar", "w") as output:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        output.addfile(info, io.BytesIO(payload))
    manifest = {
        "format": FORMAT,
        "files": {name: {"size": len(payload), "sha256": sha256_bytes(payload)}},
        "database_sha256": file_sha256(tmp_path / "database.dump"),
        "archive_sha256": file_sha256(tmp_path / "storage.tar"),
        "layout": {"data": "data"},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_complete_archive_verifies(tmp_path):
    expected = archive(tmp_path)
    assert verify(tmp_path) == expected


@pytest.mark.parametrize("name", ["../outside", "/absolute", "C:/absolute", "data\\escape"])
def test_unsafe_paths_cannot_be_restored(tmp_path, name):
    archive(tmp_path, name)
    with pytest.raises(ValueError, match="路径"):
        verify(tmp_path)


def test_corrupt_database_is_rejected_before_connecting(tmp_path):
    archive(tmp_path)
    (tmp_path / "database.dump").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="校验失败"):
        restore(tmp_path, root=tmp_path.parent / "restore", engine=None)


def test_restore_never_overwrites_existing_files(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    archive(backup)
    target = tmp_path / "existing"
    target.mkdir()
    saved = target / "data.txt"
    saved.write_text("keep", encoding="utf-8")
    # Real production logic rejects the non-empty target before the lock is ever taken.
    with pytest.raises(ValueError, match="空目录"):
        restore(backup, root=target, engine=None)
    assert saved.read_text(encoding="utf-8") == "keep"


def test_a_missing_archive_member_is_rejected(tmp_path):
    manifest = archive(tmp_path)
    manifest["files"]["data/industries/missing.parquet"] = {"size": 1, "sha256": "a" * 64}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="缺失"):
        verify(tmp_path)


def test_mount_targets_are_rejected_before_any_database_access(tmp_path, monkeypatch):
    target = tmp_path / "restore"
    target.mkdir()
    monkeypatch.setattr(backup_module, "_mount_point", lambda path, mountinfo=None: "/")
    with pytest.raises(ValueError, match="挂载点"):
        backup_module._validate_restore_target(target)


def test_symlinked_target_is_rejected_through_the_cli_entry(tmp_path):
    backup = tmp_path / "backup"
    backup.mkdir()
    archive(backup)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        # Creating a symlink needs extra privileges on Windows; the Linux R9 case covers the real link.
        pytest.skip("symlink creation not permitted on this platform")
    # restore() must judge the configured path itself, not its resolved target.
    with pytest.raises(ValueError, match="符号链接"):
        restore(backup, root=link, engine=None)
    assert list(real.iterdir()) == []


def test_cli_hands_the_configured_storage_root_to_restore(tmp_path, monkeypatch):
    captured = {}

    def fake_restore(directory, *, root, engine):
        captured["root"] = str(root)
        return {"files": {}}

    class Engine:
        def dispose(self):
            pass

    monkeypatch.setattr(backup_module, "restore", fake_restore)
    monkeypatch.setattr(backup_module, "create_engine", lambda *args, **kwargs: Engine())
    monkeypatch.setattr(backup_module, "database_url", lambda: "postgresql://user/db")
    monkeypatch.setenv("POSTGRES_HOST", "db")
    monkeypatch.setenv("STORAGE_ROOT", "/restores/link")
    monkeypatch.setattr(sys, "argv", ["backup", "restore", str(tmp_path)])
    backup_module.main()
    assert Path(captured["root"]) == Path("/restores/link")


def test_unwritable_parent_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_module.os, "access", lambda path, mode: False)
    with pytest.raises(ValueError, match="父目录.*不可写"):
        backup_module._validate_restore_target(tmp_path / "restore")


def test_validation_creates_and_removes_nothing(tmp_path):
    target = tmp_path / "restore"
    backup_module._validate_restore_target(target)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_staging_is_private_and_does_not_create_the_target(tmp_path):
    target = tmp_path / "restore"
    staging = backup_module._create_staging(target)
    assert not target.exists()
    assert staging.parent == tmp_path
    assert staging.name.startswith("restore.restore-")
    staging.rmdir()
    assert list(tmp_path.iterdir()) == []


def test_nonempty_target_is_rejected_before_database_access(tmp_path):
    target = tmp_path / "restore"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="空目录"):
        backup_module._validate_restore_target(target)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_mount_detection_ignores_an_unrelated_sibling_mount(tmp_path):
    target = tmp_path / "recovered"
    target.mkdir()
    sibling = tmp_path / "mounted-sibling"
    sibling.mkdir()
    mountinfo = (
        "36 25 0:32 / / rw,relatime shared:1 - overlay overlay rw\n"
        f"37 36 0:40 / {sibling} rw,relatime shared:2 - ext4 /dev/sda1 rw\n"
    )
    # The real parser sees only the sibling mount, so the ordinary target passes.
    assert backup_module._mount_point(target, mountinfo) is None
    assert backup_module._mount_point(sibling, mountinfo) == str(sibling.resolve())


def test_existing_empty_target_that_cannot_be_removed_fails_without_database(tmp_path, monkeypatch):
    target = tmp_path / "restore"
    target.mkdir()

    original = backup_module.Path.rmdir

    def refuse_remove(path):
        if path == target:
            raise PermissionError("Operation not permitted")
        return original(path)

    monkeypatch.setattr(backup_module.Path, "rmdir", refuse_remove)
    with pytest.raises(ValueError, match="不可移除"):
        backup_module._replace_for_restore(target)
    assert target.is_dir()


class _FakeResult:
    def one(self):
        return ("db", "public")

    def scalar_one(self):
        return 0


class _FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *exception):
        return False

    def execute(self, *args, **kwargs):
        return _FakeResult()


class _FakeEngine:
    url = type("Url", (), {"database": "db"})()

    def connect(self):
        return _FakeConnection()

    def begin(self):
        return _FakeConnection()


def _fake_restore_environment(monkeypatch, tmp_path, backup_dir, fail):
    target = tmp_path / "recovered"
    staging = tmp_path / "recovered.restore-test"
    staging.mkdir()
    monkeypatch.setattr(backup_module, "offline_lock", lambda engine: contextlib.nullcontext())
    monkeypatch.setattr(backup_module, "pg_command", fail)
    monkeypatch.setattr(backup_module, "_create_staging", lambda root: staging)
    return target, staging


def test_staging_creation_failure_removes_the_placeholder_it_created(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    backup.mkdir()
    archive(backup)
    target = tmp_path / "recovered"

    def refuse_staging(root):
        raise ValueError("恢复目标不可发布")

    monkeypatch.setattr(backup_module, "offline_lock", lambda engine: contextlib.nullcontext())
    monkeypatch.setattr(backup_module, "_create_staging", refuse_staging)
    with pytest.raises(ValueError, match="不可发布"):
        restore(backup, root=target, engine=_FakeEngine())
    assert not target.exists()


def test_failure_while_unpacking_removes_the_staging_copy(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    backup.mkdir()
    archive(backup)
    target, staging = _fake_restore_environment(monkeypatch, tmp_path, backup, lambda *args, **kwargs: None)

    def refuse_copy(*args, **kwargs):
        raise RuntimeError("extract failed")

    monkeypatch.setattr(backup_module.shutil, "copyfileobj", refuse_copy)
    with pytest.raises(RuntimeError, match="extract failed"):
        restore(backup, root=target, engine=_FakeEngine())
    assert not staging.exists()


def test_failure_after_pg_restore_keeps_the_staging_copy(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    backup.mkdir()
    archive(backup)
    target, staging = _fake_restore_environment(
        monkeypatch,
        tmp_path,
        backup,
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("pg_restore failed")),
    )
    with pytest.raises(RuntimeError, match="pg_restore failed"):
        restore(backup, root=target, engine=_FakeEngine())
    assert staging.exists()
    assert (staging / "data").exists()


def test_missing_writable_parent_is_created_inside_the_lock(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    backup.mkdir()
    archive(backup)
    target = tmp_path / "restores" / "fq_restore_demo"
    monkeypatch.setattr(backup_module, "offline_lock", lambda engine: contextlib.nullcontext())
    monkeypatch.setattr(backup_module, "pg_command", lambda *args, **kwargs: None)
    restore(backup, root=target, engine=_FakeEngine())
    assert target.is_dir()
    assert (target / "data" / "financials" / "current.json").exists()


def test_missing_parent_under_unwritable_ancestor_is_rejected_before_the_lock(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    backup.mkdir()
    archive(backup)
    target = tmp_path / "blocked" / "nested" / "recovered"
    (tmp_path / "blocked").mkdir()
    monkeypatch.setattr(backup_module.os, "access", lambda path, mode: False)
    with pytest.raises(ValueError, match="父目录.*不可写"):
        restore(backup, root=target, engine=None)
    assert not target.exists()


def test_target_written_concurrently_is_not_removed_on_failure(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    backup.mkdir()
    archive(backup)
    target = tmp_path / "recovered"

    def copy_and_write(engine):
        raise RuntimeError("unpack failed")

    monkeypatch.setattr(backup_module, "offline_lock", lambda engine: contextlib.nullcontext())

    def refuse_copy(source, destination):
        # Someone else writes into the target while this restore is still unpacking.
        target.joinpath("someone-else.txt").write_text("keep me", encoding="utf-8")
        raise RuntimeError("unpack failed")

    monkeypatch.setattr(backup_module.shutil, "copyfileobj", refuse_copy)
    with pytest.raises(RuntimeError, match="unpack failed"):
        restore(backup, root=target, engine=_FakeEngine())
    assert target.is_dir()
    assert (target / "someone-else.txt").read_text(encoding="utf-8") == "keep me"


def test_empty_target_created_here_is_removed_on_unpack_failure(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    backup.mkdir()
    archive(backup)
    target = tmp_path / "recovered"
    monkeypatch.setattr(backup_module, "offline_lock", lambda engine: contextlib.nullcontext())

    def refuse_copy(source, destination):
        raise RuntimeError("unpack failed")

    monkeypatch.setattr(backup_module.shutil, "copyfileobj", refuse_copy)
    with pytest.raises(RuntimeError, match="unpack failed"):
        restore(backup, root=target, engine=_FakeEngine())
    assert not target.exists()
