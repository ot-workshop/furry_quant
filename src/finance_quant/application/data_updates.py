"""One serial update job for shared market, financial, industry and benchmark data."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from filelock import FileLock, Timeout

from finance_quant.application import financial_download, industry_download, market_download
from finance_quant.checksums import sha256_text
from finance_quant.data import benchmarks, industries
from finance_quant.data.financials import utc_now
from finance_quant.data.managed import atomic_json
from finance_quant.task_contracts import TaskConflict

STAGES = {"market": "行情与参考数据", "financial": "财务报表", "industry": "历史行业", "benchmarks": "评价基准"}


def root(data_root):
    return Path(data_root) / "updates"


def validate_spec(document):
    fields = {"market", "market_version", "financial", "industry", "benchmarks"}
    if not isinstance(document, dict) or set(document) != fields:
        raise ValueError("更新参数须包含行情范围、已有行情版本、财务、行业和基准配置")
    spec = dict(document)
    if spec["market"] is not None:
        spec["market"] = market_download.validate_spec(spec["market"])
    version = spec["market_version"]
    if version is not None and (not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", version)):
        raise ValueError("已有行情版本无效")
    if spec["financial"] is not None:
        financial_download.validate_spec({"market_version": version or "pending", **spec["financial"]})
        if spec["market"] is None and version is None:
            raise ValueError("财务更新需要启用行情更新，或选择一个已有行情版本")
    if spec["industry"] is not None:
        industry_download.validate_spec(spec["industry"])
    if not isinstance(spec["benchmarks"], list):
        raise ValueError("基准须为列表")
    spec["benchmarks"] = sorted({benchmarks.normalize_benchmark(code) for code in spec["benchmarks"]})
    if spec["benchmarks"] and spec["market"] is None and version is None:
        raise ValueError("基准更新需要启用行情更新，或选择一个已有行情版本")
    if not any(spec[key] is not None for key in ("market", "financial", "industry")) and not spec["benchmarks"]:
        raise ValueError("请至少选择一类更新数据")
    return spec


def job_dir(data_root, identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{24}", identifier):
        raise ValueError("统一更新计划编号无效")
    return root(data_root) / "jobs" / identifier


def preview(data_root, document, *, provider=None):
    spec = validate_spec(document)
    plans = {}
    if spec["market"] is not None:
        plans["market"] = market_download.prepare(
            market_download.shared_root(data_root), spec["market"], provider=provider
        )
    if spec["market_version"]:
        financial_download._market_version(Path(data_root), spec["market_version"])
    if spec["industry"] is not None:
        plans["industry"] = industry_download.preview(spec["industry"], provider=provider)
    if spec["financial"] is not None and spec["market"] is None:
        plans["financial"] = financial_download.preview(
            Path(data_root), {"market_version": spec["market_version"], **spec["financial"]}
        )
    plan = {
        "spec": spec,
        "plans": plans,
        "created_at": utc_now(),
        "stages": [key for key in STAGES if spec[key] is not None and spec[key] != []],
        "note": "按行情→财务→行业→基准串行更新；每项保留独立版本和检查点，失败项可续传。",
    }
    plan["id"] = sha256_text(json.dumps(plan, sort_keys=True, ensure_ascii=False))[:24]
    atomic_json(job_dir(data_root, plan["id"]) / "plan.json", plan)
    return plan


def load_plan(data_root, identifier):
    plan = json.loads((job_dir(data_root, identifier) / "plan.json").read_text(encoding="utf-8"))
    if (
        sha256_text(json.dumps({k: v for k, v in plan.items() if k != "id"}, sort_keys=True, ensure_ascii=False))[:24]
        != identifier
    ):
        raise ValueError("统一更新计划校验失败")
    return plan


def _calendar(data_root, version):
    shared_root = market_download.shared_root(data_root)
    manifest = financial_download._market_version(Path(data_root), version)
    plan_dir, plan = market_download.load_plan(shared_root, manifest["plan_id"])
    calendar = pd.read_parquet(plan_dir / "reference/calendar.parquet")
    return pd.DatetimeIndex(calendar.loc[calendar.is_open & calendar.date.between(plan["start"], plan["end"]), "date"])


def run(data_root, identifier, *, provider=None, secondary=None, on_progress=None):
    plan = load_plan(data_root, identifier)
    directory = job_dir(data_root, identifier)
    progress_path = directory / "progress.json"
    progress = (
        json.loads(progress_path.read_text(encoding="utf-8"))
        if progress_path.exists()
        else {
            "id": identifier,
            "steps": {},
            "spec": plan["spec"],
            "total": len(plan["stages"]),
            "completed": 0,
        }
    )

    def save():
        progress["updated_at"] = utc_now()
        progress["completed"] = sum(step["status"] == "complete" for step in progress["steps"].values())
        atomic_json(progress_path, progress)
        if on_progress:
            on_progress(progress)

    with FileLock(root(data_root) / "worker.lock", timeout=0):
        progress["status"] = "running"
        (root(data_root) / "PAUSE").unlink(missing_ok=True)
        version = progress.get("market_version", plan["spec"]["market_version"])
        save()
        for key in plan["stages"]:
            step = progress["steps"].setdefault(key, {})
            if step.get("status") == "complete":
                continue
            if (root(data_root) / "PAUSE").exists():
                progress["status"] = "paused"
                save()
                return progress
            progress["current_stage"] = key
            step.update(status="running", message=STAGES[key])
            save()
            try:
                if key == "market":
                    (market_download.shared_root(data_root) / "PAUSE").unlink(missing_ok=True)
                    result = market_download.run(
                        market_download.shared_root(data_root),
                        plan["plans"][key]["id"],
                        provider=provider,
                        secondary=secondary,
                    )
                    if result["status"] == "complete":
                        version = result["version_id"]
                        progress["market_version"] = version
                elif key == "financial":
                    if "market" in progress["steps"] and progress["steps"]["market"]["status"] != "complete":
                        step.update(status="blocked", message="等待行情完整发布，不会改用旧股票范围")
                        save()
                        continue
                    if not step.get("plan_id"):
                        subplan = plan["plans"].get(key) or financial_download.preview(
                            Path(data_root), {"market_version": version, **plan["spec"][key]}
                        )
                        step["plan_id"] = subplan["id"]
                        save()
                    result = financial_download.run(Path(data_root), step["plan_id"], provider=provider)
                elif key == "industry":
                    result = industry_download.run(identifier=plan["plans"][key]["id"], provider=provider)
                else:
                    if not version or (
                        "market" in progress["steps"] and progress["steps"]["market"]["status"] != "complete"
                    ):
                        step.update(status="blocked", message="等待行情及交易日历完整发布")
                        save()
                        continue
                    items = step.setdefault("items", {})
                    for code in plan["spec"][key]:
                        if code not in items:
                            _, metadata = benchmarks.ensure_series(
                                code, _calendar(data_root, version), provider=provider
                            )
                            items[code] = metadata
                            save()
                    result = {"status": "complete", "message": "基准增量覆盖已核验"}
                step.update(status=result["status"], result=result, message=result.get("message", ""))
                if result["status"] == "paused":
                    progress["status"] = "paused"
                    save()
                    return progress
            except Exception as error:
                step.update(
                    status="failed",
                    message=str(error)[:300]
                    if isinstance(error, ValueError)
                    else f"{STAGES[key]}更新失败（{type(error).__name__}），可查看任务日志后续传",
                )
            save()
        progress["status"] = "complete" if progress["completed"] == progress["total"] else "partial"
        progress["message"] = (
            "各类数据已完成更新"
            if progress["status"] == "complete"
            else "存在失败或依赖未就绪的项目，可续传；已成功项目不会重跑"
        )
        save()
        return progress


def start_task(data_root, store, identifier):
    plan = load_plan(data_root, identifier)
    return store.create_download(
        "data_update",
        {"name": "统一数据更新", "plan_id": identifier},
        {"root": str(root(data_root).resolve()), "plan_id": identifier},
        "update-" + uuid.uuid4().hex,
        total=len(plan["stages"]),
    )


def pause(data_root):
    root(data_root).mkdir(parents=True, exist_ok=True)
    (root(data_root) / "PAUSE").write_text("pause", encoding="utf-8")
    # Forward to the currently active downloader; all use serial provider access.
    for directory in (
        market_download.shared_root(data_root),
        Path(data_root) / "financials",
        industries.industry_root(),
    ):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "PAUSE").write_text("pause", encoding="utf-8")


def status(data_root):
    jobs = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(root(data_root).glob("jobs/*/progress.json"), key=lambda p: p.stat().st_mtime, reverse=True)[
            :20
        ]
    ]
    schedule_path = root(data_root) / "schedule.json"
    directory = root(data_root)
    active = False
    if directory.exists():
        try:
            with FileLock(directory / "worker.lock", timeout=0):
                pass
        except Timeout:
            active = True
    if not active:
        jobs = [
            {**job, "status": "interrupted", "message": "进程已停止，可续传"} if job["status"] == "running" else job
            for job in jobs
        ]
    schedule = json.loads(schedule_path.read_text(encoding="utf-8")) if schedule_path.exists() else None
    return {
        "jobs": jobs,
        "schedule": schedule,
        "market_versions": market_download.status(market_download.shared_root(data_root))["versions"],
    }


def save_schedule(data_root, document):
    if not isinstance(document, dict) or set(document) != {"enabled", "hour", "spec"}:
        raise ValueError("自动更新设置须包含开关、小时和更新范围")
    if (
        not isinstance(document["enabled"], bool)
        or type(document["hour"]) is not int
        or not 0 <= document["hour"] <= 23
    ):
        raise ValueError("自动更新小时须为0到23，时间使用北京时间")
    schedule = {**document, "spec": validate_spec(document["spec"])}
    atomic_json(root(data_root) / "schedule.json", schedule)
    return schedule


def enqueue_due(store, data_root):

    path = root(data_root) / "schedule.json"
    if not path.exists():
        return
    schedule = json.loads(path.read_text(encoding="utf-8"))
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if not schedule["enabled"] or now.hour < schedule["hour"]:
        return
    spec = json.loads(json.dumps(schedule["spec"]))
    if spec["market"]:
        spec["market"]["end"] = (pd.Timestamp(now.date()) - pd.Timedelta(days=int(schedule["hour"] < 18))).strftime(
            "%Y-%m-%d"
        )
    key = "scheduled-" + now.strftime("%Y%m%d") + "-" + sha256_text(json.dumps(schedule, sort_keys=True))[:16]
    try:
        store.create_download(
            "data_update",
            {"name": "自动统一数据更新", "spec": spec},
            {"spec": spec},
            key,
            total=sum(bool(spec[k]) for k in STAGES),
        )
    except TaskConflict:
        # A full queue must not prevent the worker from consuming existing tasks.
        return
