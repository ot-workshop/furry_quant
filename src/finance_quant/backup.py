"""Complete database + storage backups, restored only into empty targets."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from dotenv import load_dotenv
from sqlalchemy import create_engine, text, update

from finance_quant.checksums import file_sha256, stream_sha256
from finance_quant.data.benchmarks import benchmark_root
from finance_quant.data.industries import industry_root
from finance_quant.paths import data_root, quota_database, runs_root, storage_root
from finance_quant.persistence.database import database_url
from finance_quant.persistence.maintenance import offline_lock, write_gate
from finance_quant.persistence.schema import research_task

FORMAT = "finance-quant-backup-v2"


def pg_environment(url):
    # Never put credentials on a command line or print the connection URL.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    environment.update(
        PGHOST=url.host or "localhost",
        PGPORT=str(url.port or 5432),
        PGUSER=url.username or "",
        PGPASSWORD=url.password or "",
        PGDATABASE=url.database or "",
    )
    return environment


def pg_command(tool, arguments, url):
    executable = str(Path(os.environ["POSTGRES_BIN"]) / tool) if os.environ.get("POSTGRES_BIN") else shutil.which(tool)
    if not executable:
        raise ValueError(f"找不到 {tool}；请安装 PostgreSQL 客户端或设置 POSTGRES_BIN")
    result = subprocess.run([executable, *map(str, arguments)], env=pg_environment(url), capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{tool} 失败（退出码 {result.returncode}）；备份未标记为完成，或恢复目标需检查")


def _safe_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise ValueError("备份含无效文件路径")
    return path


def _configured_layout(root):

    layout = {}
    for name, location in {
        "data": data_root(),
        "runs": runs_root(),
        "quota": quota_database(),
        "industry": industry_root(),
        "benchmark": benchmark_root(),
    }.items():
        if not location.resolve().is_relative_to(root):
            raise ValueError(f"{name} 配置位于 STORAGE_ROOT 之外，请先统一存储位置再备份；不会移动现有数据")
        layout[name] = location.resolve().relative_to(root).as_posix()
    return layout


def create(destination, *, root, engine, layout=None, worker_owned=False):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError("备份目录与存储目录须相互独立")
    if not root.is_dir() or destination.exists():
        raise ValueError("存储目录不存在，或备份目标已存在")
    layout = layout or _configured_layout(root)
    destination.mkdir(parents=True)
    manifest = {
        "format": FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(root),
        "layout": layout,
        "files": {},
    }
    # A partial directory intentionally has no manifest and cannot be restored.
    # In a queued backup the parent process already owns the sole worker lock.
    with write_gate(engine, exclusive=True) if worker_owned else offline_lock(engine):
        with engine.connect() as connection:
            if connection.execute(text("SELECT current_schema()")).scalar_one() != "public":
                raise ValueError("完整备份需要数据库 public schema；不接受隐式 search_path")
        pg_command("pg_dump", ["--format=custom", "--no-owner", "--file", destination / "database.dump"], engine.url)
        with tarfile.open(destination / "storage.tar", "w", dereference=True) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_symlink():
                    raise ValueError("存储目录包含符号链接；请改为存储目录内的实际文件")
                if not path.is_file() or path.name.endswith(".lock"):
                    continue
                name = path.relative_to(root).as_posix()
                manifest["files"][name] = {"size": path.stat().st_size, "sha256": file_sha256(path)}
                archive.add(path, arcname=name, recursive=False)
    manifest["database_sha256"] = file_sha256(destination / "database.dump")
    manifest["archive_sha256"] = file_sha256(destination / "storage.tar")
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def verify(directory):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise ValueError("备份格式不受支持")
    for location in manifest.get("layout", {}).values():
        _safe_name(location)
    for filename, key in (("database.dump", "database_sha256"), ("storage.tar", "archive_sha256")):
        if file_sha256(directory / filename) != manifest[key]:
            raise ValueError(f"备份校验失败：{filename}")
    found = set()
    with tarfile.open(directory / "storage.tar") as archive:
        for member in archive:
            _safe_name(member.name)
            if not member.isfile() or member.name in found or member.name not in manifest["files"]:
                raise ValueError("备份含未登记、重复或非普通文件")
            source = archive.extractfile(member)
            assert source is not None
            with source:
                digest = stream_sha256(source)
            expected = manifest["files"][member.name]
            if member.size != expected["size"] or digest != expected["sha256"]:
                raise ValueError(f"文件校验失败：{member.name}")
            found.add(member.name)
    if found != set(manifest["files"]):
        raise ValueError("备份文件缺失")
    return manifest


def _mount_point(path, mountinfo=None):
    """Find a mount at path itself; unrelated mounts under the same parent are irrelevant."""
    try:
        here, above = path.stat(), path.parent.stat()
    except OSError:
        return None
    try:
        text = mountinfo if mountinfo is not None else Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        # Without the mount table a device change is the only local hint.
        return str(path) if here.st_dev != above.st_dev else None
    target = os.path.realpath(path)
    for line in text.splitlines():
        fields = line.split()
        if len(fields) > 4 and os.path.realpath(fields[4].replace("\\040", " ")) == target:
            return target
    return None


def _validate_restore_target(root):
    """Reject unusable targets before any directory is created, removed or written."""
    parent = root.parent
    if root == Path(root.anchor):
        raise ValueError("恢复目录不能是文件系统根目录")
    if root.is_symlink():
        raise ValueError("恢复目标不能是符号链接")
    if root.exists():
        # Name the real reason first: a mounted target is a mount point even when /app itself is not writable.
        if _mount_point(root) is not None:
            raise ValueError(
                f"恢复目标 {root} 是挂载点本身；请挂载可写的宿主父目录，并选择其下的普通子目录（例如 /restores/recovered）"
            )
        if not root.is_dir():
            raise ValueError("恢复目标必须是目录")
        if any(root.iterdir()):
            raise ValueError("恢复只允许空目录，不会覆盖现有数据")
    # The parent may not exist yet (the web backup job restores into RESTORE_ROOT/<name>);
    # require the nearest existing ancestor to be creatable instead.
    anchor = _existing_ancestor(parent)
    if not anchor.is_dir() or not os.access(anchor, os.W_OK | os.X_OK):
        raise ValueError(f"恢复目标父目录 {anchor} 不可写，请换用可写父目录下的普通子目录")


def _existing_ancestor(path):
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current


def _replace_for_restore(root):
    """Prove the checked empty target can be removed and replaced, then make a fresh placeholder."""
    if not root.exists():
        return
    try:
        root.rmdir()
        root.mkdir()
    except OSError as error:
        raise ValueError(f"恢复目标 {root} 不可移除（{error}）；请换用可写父目录下的普通子目录") from error


def _create_staging(root):
    """Create the private staging directory that publishes onto root at the end."""
    staging = Path(str(root) + ".restore-" + uuid.uuid4().hex)
    try:
        staging.mkdir()
    except OSError as error:
        raise ValueError(f"恢复目标 {root} 不可发布（{error}）；请换用可写父目录下的普通子目录") from error
    return staging


def _release_created_target(root, created_target):
    """Drop only the empty target directory this call created; never a directory that gained content."""
    if not created_target:
        return
    try:
        root.rmdir()
    except OSError:
        pass


def restore(directory, *, root, engine):
    original, directory = Path(root), Path(directory).resolve()
    manifest = verify(directory)
    root = original.resolve()
    if original.is_symlink():
        raise ValueError("恢复目标不能是符号链接")
    if root == Path(root.anchor):
        raise ValueError("恢复目录不能是文件系统根目录")
    if directory.is_relative_to(root) or root.is_relative_to(directory):
        raise ValueError("恢复目录与备份目录须相互独立")
    # Read-only target checks run before the lock so an unusable target never reaches the database.
    _validate_restore_target(root)
    with offline_lock(engine):
        with engine.connect() as connection:
            identity = connection.execute(text("SELECT current_database(), current_schema()")).one()
            if identity != (engine.url.database, "public"):
                raise ValueError("恢复连接与显式目标不匹配")
            count = connection.execute(
                text(
                    "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' "
                    "AND c.relkind IN ('r','p','v','m','S')"
                )
            ).scalar_one()
            if count:
                raise ValueError("恢复只允许空数据库；已迁移或已有数据的数据库不能作为目标")
        # Only an empty, checked target is replaced, and only after the database was proven empty.
        # Re-check inside the lock: the target may have changed while this restore waited for it.
        _validate_restore_target(root)
        try:
            root.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ValueError(
                f"恢复目标父目录 {root.parent} 不可创建（{error}）；请换用可写父目录下的普通子目录"
            ) from error
        _replace_for_restore(root)
        created_target = False
        if not root.exists():
            try:
                root.mkdir()
                created_target = True
            except OSError as error:
                raise ValueError(f"恢复目标 {root} 不可创建（{error}）；请换用可写父目录下的普通子目录") from error
        try:
            staging = _create_staging(root)
        except BaseException:
            _release_created_target(root, created_target)
            raise
        database_written = False
        try:
            with tarfile.open(directory / "storage.tar") as archive:
                for member in archive:
                    target = staging.joinpath(*_safe_name(member.name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    assert source is not None
                    with source, target.open("wb") as output:
                        shutil.copyfileobj(source, output)
            # No --clean: a restore can never erase existing database objects.
            database_written = True
            pg_command(
                "pg_restore",
                [
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                    "--dbname",
                    engine.url.database,
                    directory / "database.dump",
                ],
                engine.url,
            )

            with engine.begin() as connection:
                # Restoring must not unexpectedly execute a preserved queue on worker startup.
                connection.execute(
                    update(research_task)
                    .where(research_task.c.status.in_(["running", "queued"]))
                    .values(
                        status="interrupted",
                        stage="finished",
                        message="从备份恢复，请确认后重新运行",
                        error="备份恢复后暂停原队列",
                        finished_at=datetime.now(timezone.utc),
                    )
                )
            root.rmdir()  # Private empty placeholder created by this restore.
            staging.rename(root)
        except BaseException:
            # Before any database write both staging and an empty target created here are ours to drop.
            if not database_written:
                shutil.rmtree(staging, ignore_errors=True)
                _release_created_target(root, created_target)
            raise
        # Keep the schedule switch inside the lock: a worker must never read the restored plan as enabled.
        schedule = root / manifest["layout"]["data"] / "updates/schedule.json"
        if schedule.exists():
            document = json.loads(schedule.read_text(encoding="utf-8"))
            document["enabled"] = False
            schedule.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "verify", "restore"])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--env-file", type=Path, help="Explicit native configuration; Compose passes settings directly")
    args = parser.parse_args()
    if args.env_file:
        if not args.env_file.is_file():
            parser.error("配置文件不存在")
        load_dotenv(args.env_file, override=False)
    try:
        if args.action == "verify":
            manifest = verify(args.directory)
        else:
            if not os.environ.get("POSTGRES_HOST"):
                parser.error("请显式配置目标数据库，不会自动读取开发环境配置")
            engine = create_engine(database_url(), hide_parameters=True)
            if args.action == "create":
                manifest = create(args.directory, root=storage_root(), engine=engine)
            else:
                # The CLI must hand over the configured path as written, so symlinked targets stay visible.
                configured = os.environ.get("STORAGE_ROOT")
                manifest = restore(
                    args.directory, root=Path(configured) if configured else storage_root(), engine=engine
                )
            engine.dispose()
        print(
            json.dumps(
                {
                    "status": "ok",
                    "action": args.action,
                    "files": len(manifest["files"]),
                    "directory": str(args.directory),
                },
                ensure_ascii=False,
            )
        )
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(2, f"{error}\n")


if __name__ == "__main__":
    main()
