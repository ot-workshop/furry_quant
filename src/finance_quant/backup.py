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

from sqlalchemy import create_engine, text, update

from finance_quant.checksums import file_sha256, stream_sha256
from finance_quant.paths import data_root, quota_database, runs_root, storage_root
from finance_quant.persistence.database import database_url
from finance_quant.persistence.maintenance import offline_lock, write_gate

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
    from finance_quant.data.benchmarks import benchmark_root
    from finance_quant.data.industries import industry_root

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


def restore(directory, *, root, engine):
    directory, root = Path(directory).resolve(), Path(root).resolve()
    manifest = verify(directory)
    if root == Path(root.anchor) or directory.is_relative_to(root) or root.is_relative_to(directory):
        raise ValueError("恢复目录与备份目录须相互独立，且不能是磁盘根目录")
    if root.exists() and any(root.iterdir()):
        raise ValueError("恢复只允许空目录，不会覆盖现有数据")
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
        staging = root.parent / ("." + root.name + ".restore-" + uuid.uuid4().hex)
        staging.mkdir(parents=True)
        with tarfile.open(directory / "storage.tar") as archive:
            for member in archive:
                target = staging.joinpath(*_safe_name(member.name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                assert source is not None
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        # No --clean: a restore can never erase existing database objects.
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
        from finance_quant.persistence.schema import research_task

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
        if root.exists():
            root.rmdir()  # Checked empty; never recursively remove a target.
        staging.rename(root)
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
        from dotenv import load_dotenv

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
            root = storage_root()
            if args.action == "create":
                manifest = create(args.directory, root=root, engine=engine)
            else:
                manifest = restore(args.directory, root=root, engine=engine)
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
