"""Industry reference-data planning, resumable execution and status.

The data layer (``finance_quant.data.industries``) owns the vendor contract and the
immutable version. This module only turns a web request into a queued task, records
progress for the UI, and keeps a failed run from replacing the current version.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

from finance_quant.checksums import sha256_text
from finance_quant.data import industries
from finance_quant.data.managed import atomic_json
from finance_quant.data.network_budget import DownloadPaused
from finance_quant.data.providers import TushareProvider, TushareSettings

PLAN_POLICY = "industry-download-v1"
LEVELS = industries.LEVELS


def default_provider():
    return TushareProvider(TushareSettings(TushareSettings.from_env().token))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_spec(document) -> dict:
    if not isinstance(document, dict) or set(document) != {"levels", "refresh"}:
        raise ValueError("下载参数须包含行业级别和是否刷新")
    levels = document["levels"]
    if not isinstance(levels, list) or not levels:
        raise ValueError("请至少选择一个行业级别")
    if any(not isinstance(level, str) or level not in LEVELS for level in levels):
        raise ValueError("行业级别须为 L1、L2 或 L3")
    if len(set(levels)) != len(levels):
        raise ValueError("行业级别不能重复")
    refresh = document["refresh"]
    if not isinstance(refresh, bool):
        raise ValueError("是否刷新须为布尔值")
    return {"levels": [level for level in LEVELS if level in levels], "refresh": refresh}


def _plan_dir(root: Path, identifier: str) -> Path:
    if not isinstance(identifier, str) or len(identifier) != 24 or any(c not in "0123456789abcdef" for c in identifier):
        raise ValueError("下载计划编号无效")
    return root / "jobs" / identifier


def load_plan(root: Path, identifier: str) -> dict:
    plan = json.loads((_plan_dir(root, identifier) / "plan.json").read_text(encoding="utf-8"))
    check = {key: value for key, value in plan.items() if key != "id"}
    if sha256_text(json.dumps(check, sort_keys=True, ensure_ascii=False))[:24] != identifier:
        raise ValueError("下载计划校验失败")
    return plan


def _requests(levels, sectors) -> tuple[int, dict]:
    counts = {}
    members = 0
    for taxonomy in ("SW2014", "SW2021"):
        for level in levels:
            count = int(sectors.loc[sectors.classification.eq(taxonomy) & sectors.level.eq(level)].index_code.nunique())
            counts[f"{taxonomy}-{level}"] = count
            members += count * 2
    catalogs = len(levels) * 2
    return catalogs + members, counts


def preview(document: dict, *, root=None, provider=None) -> dict:
    """Resolve sector counts before downloading; only six small catalog calls."""
    spec = validate_spec(document)
    root = Path(root or industries.industry_root())
    provider = provider or default_provider()
    sectors = industries.fetch_catalogs(provider, spec["levels"])
    requests, counts = _requests(spec["levels"], sectors)
    current = industries.optional_industries(root=root)
    plan = {
        "schema_version": PLAN_POLICY,
        "spec": spec,
        "sectors": counts,
        "estimated_requests": requests,
        "estimated_minutes": math.ceil(requests / 120),
        "current_version": current.manifest["id"] if current else None,
        "current_levels": sorted(current.levels) if current else [],
        "note": (
            "按申万2014/2021分类逐板块请求；单板块达到接口行数上限时拒绝发布，不会截断。"
            "已完成的板块会缓存，可暂停后续传；全部成功才发布新版本，旧版本保持可用。"
        ),
        "point_in_time_certified": False,
    }
    plan["id"] = sha256_text(json.dumps(plan, sort_keys=True, ensure_ascii=False))[:24]
    directory = _plan_dir(root, plan["id"])
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / "plan.json").exists():
        atomic_json(directory / "plan.json", plan)
    return plan


def active(root=None) -> bool:
    root = Path(root or industries.industry_root())
    root.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(root / "download.lock", timeout=0):
            return False
    except Timeout:
        return True


def status(root=None) -> dict:
    root = Path(root or industries.industry_root())
    root.mkdir(parents=True, exist_ok=True)
    try:
        current = industries.optional_industries(root=root)
    except ValueError:
        current = None
    versions = []
    for path in sorted((root).glob("*/manifest.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.parent.name in {"raw", "jobs"}:
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        versions.append(
            {
                "id": manifest["id"],
                "schema_version": manifest["schema_version"],
                "rows": manifest["rows"],
                "instruments": manifest["instruments"],
                "levels": manifest.get("levels") or {},
                "data_asof": manifest.get("data_asof"),
                "created_at": manifest.get("created_at"),
                "current": current is not None and current.manifest["id"] == manifest["id"],
            }
        )
    jobs = []
    for path in sorted((root / "jobs").glob("*/progress.json"), key=lambda item: item.stat().st_mtime, reverse=True)[
        :20
    ]:
        row = json.loads(path.read_text(encoding="utf-8"))
        if row["status"] == "running" and not active(root):
            row = {**row, "status": "interrupted", "message": "进程已停止，可续传；旧版本仍可用"}
        jobs.append(row)
    from finance_quant.data.network_budget import NetworkBudget

    return {
        "worker_running": active(root),
        "version": (
            {
                "id": current.manifest["id"],
                "rows": current.manifest["rows"],
                "instruments": current.manifest["instruments"],
                "levels": current.levels,
                "data_asof": current.data_asof,
                "created_at": current.manifest.get("created_at"),
                "policy": current.manifest.get("policy"),
            }
            if current
            else None
        ),
        "versions": versions,
        "jobs": jobs,
        "budget": NetworkBudget().status(),
    }


def run(root=None, identifier=None, *, provider=None, on_progress=None) -> dict:
    root = Path(root or industries.industry_root())
    plan = load_plan(root, identifier)
    directory = _plan_dir(root, identifier)
    provider = provider or default_provider()
    progress = {
        "id": identifier,
        "status": "running",
        "total": plan["estimated_requests"],
        "completed": 0,
        "failures": [],
        "levels": plan["spec"]["levels"],
        "message": "准备行业分类目录",
        "updated_at": utc_now(),
    }

    def save():
        progress["updated_at"] = utc_now()
        atomic_json(directory / "progress.json", progress)

    def report(event):
        if event.get("stage") in {"members", "failure"}:
            progress["completed"] = min(progress["total"], progress["completed"] + 1)
        if event.get("stage") == "members":
            progress["message"] = f"{event['taxonomy']} {event['level']} {event['code']}：{event.get('rows', 0)} 行"
        elif event.get("stage") == "failure":
            progress["message"] = f"{event['taxonomy']} {event['level']} {event['code']} 失败"
        if event.get("stage") == "failure":
            progress["failures"].append(event)
        save()
        if on_progress:
            on_progress(progress)

    (root / "PAUSE").unlink(missing_ok=True)
    save()
    try:
        history = industries.download_industries(
            root=root,
            provider=provider,
            progress=report,
            refresh=plan["spec"]["refresh"],
            levels=plan["spec"]["levels"],
            pause_file=root / "PAUSE",
        )
    except DownloadPaused as error:
        progress.update(status="paused", message=str(error))
        save()
        return progress
    except industries.IndustryDownloadIncomplete as error:
        progress.update(status="partial", failures=error.failures, message=str(error))
        save()
        return progress
    except BaseException as error:
        progress.update(status="interrupted", message=f"下载中断：{type(error).__name__}")
        save()
        raise
    progress.update(
        status="complete",
        version_id=history.manifest["id"],
        message="行业数据已发布，旧版本保留",
        completed=progress["total"],
    )
    save()
    if on_progress:
        on_progress(progress)
    return progress


def start_task(task_store, *, root=None, plan_id: str) -> dict:
    root = Path(root or industries.industry_root())
    plan = load_plan(root, plan_id)
    if active(root):
        raise ValueError("已有行业数据下载任务运行，请先暂停或等待完成")
    return task_store.create_download(
        "industry_download",
        {"name": "行业数据下载", "plan_id": plan_id},
        {"root": str(root.resolve()), "plan_id": plan_id},
        "industry-" + uuid.uuid4().hex,
        total=max(1, plan["estimated_requests"]),
    )
