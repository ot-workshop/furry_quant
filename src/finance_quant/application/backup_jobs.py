"""Web-managed maintenance jobs; restore always targets a separately named database."""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from finance_quant import backup
from finance_quant.data.managed import atomic_json
from finance_quant.paths import storage_root


def backup_root():
    return Path(os.environ.get("BACKUP_ROOT", storage_root().parent / "backups")).resolve()


def restore_root():
    return Path(os.environ.get("RESTORE_ROOT", storage_root().parent / "restores")).resolve()


def backup_path(identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r"backup-[a-f0-9]{24}", identifier):
        raise ValueError("备份编号无效")
    return backup_root() / identifier


def status(store):
    items = []
    for path in sorted(backup_root().glob("backup-*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        items.append(
            {
                "id": path.parent.name,
                "created_at": manifest["created_at"],
                "files": len(manifest["files"]),
                "bytes": sum(p.stat().st_size for p in path.parent.iterdir() if p.is_file()),
                "format": manifest["format"],
            }
        )
    jobs = [row for row in store.list()["items"] if row.get("kind") == "maintenance"]
    return {
        "backups": items,
        "jobs": jobs,
        "backup_root": str(backup_root()),
        "restore_root": str(restore_root()),
        "coverage": [
            "数据库",
            "行情与原始响应",
            "财务与行业版本",
            "基准",
            "下载检查点",
            "研究数据集与结果",
            "日志与配额记录",
        ],
    }


def validate_request(document, current_database):
    if not isinstance(document, dict) or document.get("action") not in {"create", "verify", "restore"}:
        raise ValueError("请选择创建、校验或恢复备份")
    action = document["action"]
    expected = {"action"} if action == "create" else {"action", "backup_id"}
    if action == "restore":
        expected |= {"database"}
    if set(document) != expected:
        raise ValueError("备份操作参数不完整")
    if action != "create":
        if not (backup_path(document["backup_id"]) / "manifest.json").is_file():
            raise ValueError("备份不存在或尚未完成")
    if action == "restore":
        name = document["database"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"fq_restore_[a-z0-9_]{1,45}", name)
            or name == current_database
        ):
            raise ValueError("恢复必须使用独立的 fq_restore_* 数据库，不能是当前数据库")
        if (restore_root() / name).is_relative_to(storage_root()):
            raise ValueError("RESTORE_ROOT 必须放在当前 STORAGE_ROOT 之外，使用独立恢复目录")
        if (restore_root() / name).exists() and any((restore_root() / name).iterdir()):
            raise ValueError("恢复存储目标已有文件，请使用新的恢复环境名称")
    return dict(document)


def start(store, document):
    engine = getattr(store, "engine", None)
    validated = validate_request(document, engine.url.database if engine is not None else os.environ.get("POSTGRES_DB"))
    if validated["action"] == "create":
        validated["backup_id"] = "backup-" + uuid.uuid4().hex[:24]
    return store.create_download(
        "maintenance",
        {
            "name": {"create": "完整数据备份", "verify": "校验备份", "restore": "恢复至独立环境"}[validated["action"]],
            **validated,
        },
        validated,
        "maintenance-" + uuid.uuid4().hex,
        total=1,
    )


def execute(store, task, *, root=None):
    identifier = task["id"]
    document = task["execution"]
    engine = store.engine
    directory = backup_path(document["backup_id"])
    action = document["action"]
    store.stage(identifier, "download", "正在处理完整备份" if action == "create" else "正在校验或恢复备份", 0)
    if action == "create":
        manifest = backup.create(directory, root=root or storage_root(), engine=engine, worker_owned=True)
        result = {
            "backup_id": document["backup_id"],
            "files": len(manifest["files"]),
            "message": "数据库与完整文件存储已备份",
        }
    elif action == "verify":
        manifest = backup.verify(directory)
        result = {
            "backup_id": document["backup_id"],
            "files": len(manifest["files"]),
            "message": "归档与全部文件校验通过",
        }
    else:
        validate_request({k: document[k] for k in ("action", "backup_id", "database")}, engine.url.database)
        # Verify the complete archive BEFORE creating any target database.
        backup.verify(directory)
        name = document["database"]
        target_url = engine.url.set(
            database=name,
            username=os.environ.get("RESTORE_POSTGRES_USER", engine.url.username),
            password=os.environ.get("RESTORE_POSTGRES_PASSWORD", engine.url.password),
        )
        admin = create_engine(
            target_url.set(database=engine.url.database), isolation_level="AUTOCOMMIT", hide_parameters=True
        )
        try:
            with admin.connect() as connection:
                exists = connection.execute(
                    text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": name}
                ).scalar()
                if not exists:
                    connection.execute(text(f'CREATE DATABASE "{name}"'))
        except DBAPIError as error:
            raise ValueError(
                "无法创建独立恢复数据库，请管理员预建此空库或配置 RESTORE_POSTGRES_USER/PASSWORD"
            ) from error
        finally:
            admin.dispose()
        target = create_engine(target_url, hide_parameters=True)
        try:
            restored = restore_root() / name
            backup.restore(directory, root=restored, engine=target)
        finally:
            target.dispose()
        result = {
            "backup_id": document["backup_id"],
            "database": name,
            "storage_root": str(restored),
            "message": "已恢复至独立数据库和目录，未切换当前环境；队列与自动更新保持暂停",
        }
    result.update(action=action, verified_at=datetime.now(timezone.utc).isoformat())
    atomic_json(directory / ("last-" + action + ".json"), result)
    store.finish(identifier, "succeeded", result=result)
    return result
