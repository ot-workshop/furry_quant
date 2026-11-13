"""Resumable per-stock financial statement download into immutable versions.

Every stock costs nine requests (three statements times three report types) plus the
shared calendar, so the flow is planned once, previewed, then executed stock by stock
with durable progress. A failed stock never replaces the last published version.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from filelock import FileLock, Timeout

from finance_quant.checksums import sha256_text
from finance_quant.data.financials import (
    REPORT_TYPES,
    FinancialStockMismatch,
    FinancialStore,
    content_id,
    empty_observations,
    financial_root,
    merge_observations,
    utc_now,
)
from finance_quant.data.managed import atomic_json
from finance_quant.data.network_budget import DownloadPaused
from finance_quant.data.providers import (
    TushareProvider,
    TushareSettings,
    financial_request_fields,
    normalize_calendar,
    normalize_financial_statement,
)

PLAN_POLICY = "financial-download-v2"
DEFAULT_REFRESH_DAYS = 7
STATEMENTS = ("income", "balance", "cashflow")
REQUESTS_PER_STOCK = len(STATEMENTS) * len(REPORT_TYPES)
REVISION_OVERLAP_DAYS = 400
FULL_SCAN_DAYS = 90
_CODE = re.compile(r"^\d{6}\.(?:SH|SZ)$")
_IDENTIFIER = re.compile(r"[a-f0-9]{24}")


def default_provider():
    return TushareProvider(TushareSettings(TushareSettings.from_env().token))


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_spec(document) -> dict:
    """``{"market_version": <.id>, "refresh_days": 0..3650}``; the stock list comes from the version."""
    required = {"market_version", "refresh_days"}
    if not isinstance(document, dict) or not required <= set(document) or set(document) - required - {"full_refresh"}:
        raise ValueError("下载参数须包含行情版本和更新间隔")
    identifier = document["market_version"]
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", identifier):
        raise ValueError("行情版本编号无效")
    days = document["refresh_days"]
    if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 3650:
        raise ValueError("更新间隔须为 0 至 3650 的整数天")
    if not isinstance(document.get("full_refresh", False), bool):
        raise ValueError("全量校订须为布尔值")
    return {"market_version": identifier, "refresh_days": days, "full_refresh": document.get("full_refresh", False)}


def _market_version(data_root: Path, version: str) -> dict:
    """The manifest of one published shared market version (its asset list and plan)."""
    from finance_quant.application.market_download import shared_root

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", version):
        raise ValueError("行情版本编号无效")
    root = shared_root(data_root) / "versions" / version
    if not (root / "version.json").exists() or not (root / "manifest.json").exists():
        raise ValueError("行情版本不存在，请刷新数据列表")
    return _json(root / "manifest.json")


def _market_codes(data_root: Path, version: str) -> list[str]:
    manifest = _market_version(data_root, version)
    return [code for code in sorted(manifest["assets"]) if _CODE.fullmatch(code)]


def preview(data_root: Path, document: dict) -> dict:
    """Plan without downloading: which stocks need fetching and what it costs."""
    spec = validate_spec(document)
    data_root = Path(data_root).resolve()
    codes = _market_codes(data_root, spec["market_version"])
    if not codes:
        raise ValueError("该行情版本没有可用的沪深A股")
    store = FinancialStore(financial_root(data_root))
    cutoff = pd.Timestamp(datetime.now(ZoneInfo("Asia/Shanghai")) - pd.Timedelta(days=spec["refresh_days"]))
    pending, skipped = [], 0
    for code in codes:
        pointer = store.pointer(code)
        if pointer and not spec["full_refresh"] and pd.Timestamp(pointer["fetched_at"]) > cutoff:
            skipped += 1
        else:
            state = pointer.get("fetch_state", {}) if pointer else {}
            full = (
                spec["full_refresh"]
                or not state.get("full_scan_at")
                or (pd.Timestamp.now(tz="UTC") - pd.Timestamp(state["full_scan_at"])).days >= FULL_SCAN_DAYS
            )
            start = (
                "19900101"
                if full
                else (pd.Timestamp(pointer["fetched_at"]) - pd.Timedelta(days=REVISION_OVERLAP_DAYS)).strftime("%Y%m%d")
            )
            pending.append(
                {
                    "code": code,
                    "rows": pointer["rows"] if pointer else 0,
                    "base_object": pointer["object"] if pointer else None,
                    "base_fetched_at": pointer["fetched_at"] if pointer else None,
                    "start_date": start,
                    "full_scan": full,
                }
            )
    plan = {
        "schema_version": PLAN_POLICY,
        "spec": spec,
        "codes": pending,
        "skipped": skipped,
        "estimated_requests": len(pending) * REQUESTS_PER_STOCK,
        "requests_per_stock": REQUESTS_PER_STOCK,
        "note": (
            "财务接口按单只股票查询（2000 积分）；每只股票 "
            f"{REQUESTS_PER_STOCK} 次请求，共用每分钟 200 次、每接口每日约 10 万次的限额。"
            "下载完成后发布不可变版本，旧版本与已有结果不受影响。"
            "首次及每90天全量校订；其余更新回看最近400天公告。每张报表独立续传，旧公告的就地修订由全量校订发现。"
        ),
        "point_in_time_certified": False,
    }
    plan["id"] = sha256_text(json.dumps(plan, sort_keys=True, ensure_ascii=False))[:24]
    directory = _plan_dir(data_root, plan["id"])
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / "plan.json").exists():
        atomic_json(directory / "plan.json", plan)
    return plan


def _plan_dir(data_root: Path, identifier: str) -> Path:
    if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
        raise ValueError("下载计划编号无效")
    return financial_root(data_root) / "jobs" / identifier


def load_plan(data_root: Path, identifier: str) -> dict:
    plan = _json(_plan_dir(data_root, identifier) / "plan.json")
    check = {key: value for key, value in plan.items() if key != "id"}
    if sha256_text(json.dumps(check, sort_keys=True, ensure_ascii=False))[:24] != identifier:
        raise ValueError("下载计划校验失败")
    return plan


def active(data_root: Path) -> bool:
    root = financial_root(data_root)
    try:
        with FileLock(root / "worker.lock", timeout=0):
            return False
    except Timeout:
        return True


def status(data_root: Path) -> dict:
    from finance_quant.application.market_download import shared_root

    root = financial_root(data_root)
    store = FinancialStore(root)
    current = store.current()
    versions = []
    for path in sorted((root / "versions").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        manifest = _json(path)
        versions.append(
            {
                "id": manifest["id"],
                "created_at": manifest.get("created_at"),
                "codes": len(manifest["stocks"]),
                "rows": sum(entry["rows"] for entry in manifest["stocks"].values()),
                "quarantined": sum(entry["quarantined"] for entry in manifest["stocks"].values()),
                "latest_report_period": max(
                    (entry["latest_period"] for entry in manifest["stocks"].values() if entry["latest_period"]),
                    default=None,
                ),
                "current": current is not None and current["id"] == manifest["id"],
            }
        )
    jobs = []
    for path in sorted((root / "jobs").glob("*/progress.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
        row = _json(path)
        if row["status"] == "running" and not active(data_root):
            row = {**row, "status": "interrupted", "message": "进程已停止，可续传；旧版本仍可用"}
        jobs.append(row)
    market_versions = []
    for path in sorted(
        (shared_root(data_root) / "versions").glob("*/version.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )[:20]:
        version = _json(path)
        market_versions.append(
            {
                "id": version["id"],
                "scope": version["scope"],
                "start": version["start"],
                "end": version["end"],
                "codes_count": version["codes_count"],
                "restricted": version.get("restricted", False),
            }
        )
    from finance_quant.data.network_budget import NetworkBudget

    return {
        "worker_running": active(data_root),
        "version": (
            {
                "id": current["id"],
                "created_at": current.get("created_at"),
                "codes": len(current["stocks"]),
                "rows": sum(entry["rows"] for entry in current["stocks"].values()),
                "quarantined": sum(entry["quarantined"] for entry in current["stocks"].values()),
                "latest_report_period": max(
                    (entry["latest_period"] for entry in current["stocks"].values() if entry["latest_period"]),
                    default=None,
                ),
            }
            if current is not None
            else None
        ),
        "versions": versions,
        "jobs": jobs,
        "market_versions": market_versions,
        "budget": NetworkBudget().status(),
    }


def run(data_root: Path, identifier: str, *, provider=None) -> dict:
    root = financial_root(data_root)
    directory = _plan_dir(data_root, identifier)
    plan = load_plan(data_root, identifier)
    store = FinancialStore(root)
    provider = provider or default_provider()
    progress = {
        "id": identifier,
        "status": "running",
        "total": len(plan["codes"]),
        "completed": 0,
        "skipped": plan["skipped"],
        "failures": [],
        "downloaded_rows": 0,
        "updated_at": utc_now(),
    }

    def save():
        progress["updated_at"] = utc_now()
        atomic_json(directory / "progress.json", progress)

    with FileLock(root / "worker.lock", timeout=0):
        try:
            progress_path = directory / "progress.json"
            if progress_path.exists() and (previous := _json(progress_path)).get("status") == "complete":
                return previous
            checkpoint_path = directory / "checkpoint.json"
            checkpoint = _json(checkpoint_path) if checkpoint_path.exists() else {}
            (root / "PAUSE").unlink(missing_ok=True)
            _refresh_calendar(store, provider)
            save()
            for item in plan["codes"]:
                if (root / "PAUSE").exists():
                    raise DownloadPaused("用户暂停；已完成的股票保留，可续传")
                code = item["code"]
                if checkpoint.get(code, {}).get("complete"):
                    progress["completed"] += 1
                    continue
                progress["current_code"] = code
                save()
                try:
                    saved = checkpoint.setdefault(code, {"streams": []})
                    try:
                        observations = store.observations(code)
                    except FinancialStockMismatch:
                        observations = empty_observations()
                        item = {**item, "full_scan": True, "start_date": "19900101"}
                        saved.clear()
                        saved["streams"] = []
                    if "object" in saved:
                        # Another plan may have refreshed this stock while this one was paused.
                        observations = pd.concat(
                            [store._get(saved["object"], saved["sha256"]), observations], ignore_index=True
                        )
                        observations = observations.sort_values("first_seen").drop_duplicates(
                            "row_sha256", keep="first"
                        )
                    for statement in STATEMENTS:
                        for report_type in REPORT_TYPES:
                            stream = f"{statement}:{report_type}"
                            if stream in saved["streams"]:
                                continue
                            if (root / "PAUSE").exists():
                                raise DownloadPaused("用户暂停；已完成的报表保留，可续传")
                            fresh = _statement(
                                provider, code, statement, report_type, start_date=item.get("start_date", "19900101")
                            )
                            observations = merge_observations(observations, fresh, seen_on=utc_now())
                            saved["object"] = content_id(observations)
                            saved["sha256"] = store._put(saved["object"], observations)
                            saved["streams"].append(stream)
                            atomic_json(checkpoint_path, checkpoint)
                    before = item["rows"]
                    previous_state = (store.pointer(code) or {}).get("fetch_state", {})
                    pointer = store.save_stock(
                        code,
                        observations,
                        fetched_at=utc_now(),
                        fetch_state={
                            "full_scan_at": utc_now()
                            if item.get("full_scan", True)
                            else previous_state.get("full_scan_at"),
                        },
                    )
                    saved["complete"] = True
                    atomic_json(checkpoint_path, checkpoint)
                    progress["downloaded_rows"] += max(0, pointer["rows"] - before)
                except DownloadPaused:
                    raise
                except Exception as error:
                    from finance_quant.data.providers import TushareProviderError

                    if isinstance(error, TushareProviderError) and error.category in {"access_denied", "rate_limit"}:
                        raise
                    progress["failures"].append(
                        {
                            "code": code,
                            "type": type(error).__name__,
                            "reason": str(error)[:240],
                        }
                    )
                progress["completed"] += 1
                save()
            if progress["failures"]:
                progress.update(status="partial", message="有股票下载失败，未发布新版本；可续传")
            else:
                version = store.publish()
                progress.update(status="complete", version_id=version["id"], message="新版本已发布，旧版本保留")
            save()
            return progress
        except DownloadPaused as error:
            progress.update(status="paused", message=str(error))
            save()
            return progress
        except BaseException:
            progress.update(status="interrupted", message="发布或运行中断；上一成功版本保留，可续传")
            save()
            raise


def _api(statement: str) -> str:
    from finance_quant.data.providers import FINANCIAL_STATEMENTS

    return FINANCIAL_STATEMENTS[statement][0]


# ``balancesheet`` silently returns at most 100 rows: a whole-history request stops around
# 2010 for a long-listed stock. A response that reaches this many rows is always re-fetched
# in smaller windows, and a single window that still reaches it is rejected.
MAX_STATEMENT_ROWS = 100
STATEMENT_WINDOW_YEARS = 10


def _statement(provider, code: str, statement: str, report_type: int, start_date="19900101") -> pd.DataFrame:
    """One statement type for one stock, paging by window whenever the row cap may truncate."""
    api = _api(statement)
    fields = financial_request_fields(statement)

    def query(start: str, end: str) -> pd.DataFrame:
        return normalize_financial_statement(
            provider.query(api, fields=fields, ts_code=code, report_type=report_type, start_date=start, end_date=end),
            statement,
            report_type,
            code,
        )

    today = datetime.now(ZoneInfo("Asia/Shanghai")).year
    whole = query(start_date, f"{today}1231")
    if len(whole) < MAX_STATEMENT_ROWS:
        return whole
    parts = []
    first = int(start_date[:4])
    while first <= today:
        parts.append(_window(query, first, min(first + STATEMENT_WINDOW_YEARS - 1, today), statement, code))
        first += STATEMENT_WINDOW_YEARS
    merged = pd.concat(parts, ignore_index=True)
    return merged.drop_duplicates()


def _window(query, first: int, last: int, statement: str, code: str) -> pd.DataFrame:
    """One window, halved while it still reaches the row cap."""
    frame = query(f"{first}0101", f"{last}1231")
    if len(frame) < MAX_STATEMENT_ROWS:
        return frame
    if first == last:
        raise ValueError(
            f"{code} {statement} 在 {first} 年的返回达到接口行数上限，无法确认完整性；请稍后重试或联系数据源"
        )
    middle = (first + last) // 2
    return pd.concat(
        [
            _window(query, first, middle, statement, code),
            _window(query, middle + 1, last, statement, code),
        ],
        ignore_index=True,
    )


def _refresh_calendar(store: FinancialStore, provider) -> pd.DataFrame:
    """Keep an official calendar spanning every stored report period plus the current year."""
    pointer = store.calendar_pointer()
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    parts = []
    if pointer is not None:
        parts.append(store._get(pointer["object"], pointer["sha256"]))
        if pointer["end"] >= f"{today.year}-12-31":
            return parts[0]
        years = range(int(pointer["end"][:4]) + 1, today.year + 1)
    else:
        years = range(1990, today.year + 1)
    for year in years:
        raw = provider.query("trade_cal", exchange="SSE", start_date=f"{year}0101", end_date=f"{year}1231")
        parts.append(normalize_calendar(raw))
    calendar = pd.concat(parts, ignore_index=True).drop_duplicates("date").sort_values("date").reset_index(drop=True)
    store.save_calendar(calendar)
    return calendar


def start_task(data_root: Path, task_store, plan_id: str) -> dict:
    """Queue one ``financial_download`` task for a previewed plan."""
    plan = load_plan(data_root, plan_id)
    if active(data_root):
        raise ValueError("已有财务数据下载任务运行，请先暂停或等待完成")
    root = financial_root(data_root)
    return task_store.create_download(
        "financial_download",
        {"name": "财务数据下载", "plan_id": plan_id},
        {"root": str(root.resolve()), "plan_id": plan_id},
        "financial-" + uuid.uuid4().hex,
        total=max(1, len(plan["codes"])),
    )
