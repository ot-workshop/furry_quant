"""Archive validation rejects partial, corrupted and unsafe backups before any restore."""

import io
import json
import tarfile

import pytest

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
    with pytest.raises(ValueError, match="空目录"):
        restore(backup, root=target, engine=None)
    assert saved.read_text(encoding="utf-8") == "keep"


def test_a_missing_archive_member_is_rejected(tmp_path):
    manifest = archive(tmp_path)
    manifest["files"]["data/industries/missing.parquet"] = {"size": 1, "sha256": "a" * 64}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="缺失"):
        verify(tmp_path)
