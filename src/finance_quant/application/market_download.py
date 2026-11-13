"""Shared quote planning, resumable incremental acquisition and immutable views."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from filelock import FileLock, Timeout

from finance_quant.application.quote_capture import download_asset, validation_signature
from finance_quant.application.update_service import publish_references, refresh_dates
from finance_quant.checksums import sha256_text
from finance_quant.data import postgres_market
from finance_quant.data import shared_market as shared
from finance_quant.data.catalog import Catalog, utc_now
from finance_quant.data.execution_universe import validate_execution_universe
from finance_quant.data.index_universe import historical_codes, members_on, normalize_memberships
from finance_quant.data.managed import ManagedFetcher, atomic_json, coverage_report, month_ranges, year_ranges
from finance_quant.data.market import SnapshotFetcher, load_market
from finance_quant.data.network_budget import DownloadPaused, NetworkBudget
from finance_quant.data.providers import TushareProvider, TushareSettings, normalize_calendar, normalize_index_daily
from finance_quant.data.providers.baostock import BaoStockProvider
from finance_quant.data.suspensions import LeadingHaltError

POLICY = "shared-incremental-v1"
INDEX_SIZE = 300
CODE = re.compile(r"(?:(?:60|68)\d{4}\.SH|(?:00|30)\d{4}\.SZ)")


class IncrementalFetcher(ManagedFetcher):
    """Persist each response before the next request; pause cannot discard prefetched replies."""

    def get_many(self, requests):
        frames = []
        for api, params in requests:
            if self.pause_file.exists():
                raise DownloadPaused("用户暂停；已完成接口响应保留，可续传")
            frames.append(self.get(api, **params))
        return frames


def shared_root(data_root):
    return Path(data_root) / "shared-market"


def official_provider():
    return TushareProvider(TushareSettings(TushareSettings.from_env().token))


def _save(path, frame):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".pending.parquet")
    frame.to_parquet(temporary, index=False)
    temporary.replace(path)


def _json(path):
    for attempt in range(21):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 20:
                raise
            time.sleep(0.05)


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{24}", value):
        raise ValueError("下载计划编号无效")
    return value


def validate_spec(document):
    required = {"scope", "start", "end", "warmup", "codes"}
    if (
        not isinstance(document, dict)
        or not required.issubset(document)
        or set(document) - required - {"refresh_sessions", "deep_refresh"}
    ):
        raise ValueError("下载范围参数不完整")
    if "refresh_sessions" in document and (
        isinstance(document["refresh_sessions"], bool)
        or not isinstance(document["refresh_sessions"], int)
        or not 0 <= document["refresh_sessions"] <= 504
    ):
        raise ValueError("修订巡检须为0至504个交易日")
    if "deep_refresh" in document and not isinstance(document["deep_refresh"], bool):
        raise ValueError("深度复核必须为布尔值")
    if document["scope"] not in {"csi300", "all_market", "custom"}:
        raise ValueError("下载范围无效")
    for field in ["start", "end"]:
        if not isinstance(document[field], str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", document[field]):
            raise ValueError("日期须为YYYY-MM-DD")
    start, end = pd.Timestamp(document["start"]), pd.Timestamp(document["end"])
    today = datetime.now(ZoneInfo("Asia/Shanghai"))
    cutoff = pd.Timestamp(today.date()) - pd.Timedelta(days=int(today.hour < 18))
    if not pd.Timestamp("1990-01-01") <= start <= end <= cutoff:
        raise ValueError("日期须在1990年至最近已完成的数据日之间")
    warmup = document["warmup"]
    if isinstance(warmup, bool) or not isinstance(warmup, int) or not 0 <= warmup <= 504:
        raise ValueError("预热长度须为0至504个交易日")
    codes = document["codes"]
    if (
        not isinstance(codes, list)
        or len(codes) > 10000
        or any(not isinstance(c, str) or not CODE.fullmatch(c) for c in codes)
        or len(set(codes)) != len(codes)
    ):
        raise ValueError("股票代码须为不重复的沪深A股代码")
    if document["scope"] == "custom" and not codes:
        raise ValueError("自选范围不能为空")
    return {**document, "codes": sorted(codes)}


DEFAULT_REFRESH_SESSIONS = 20


def prepare(root, document, *, provider=None):
    validate_spec(document)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(root / "preview.lock", timeout=0):
        return _prepare(root, document, provider=provider)


def _prepare(root, document, *, provider=None):
    """Only reference queries during preview. Never downloads stock quotes."""

    spec = validate_spec(document)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    refresh_sessions = spec.get("refresh_sessions", DEFAULT_REFRESH_SESSIONS)
    refresh_id = uuid.uuid4().hex if refresh_sessions or spec.get("deep_refresh") else None
    provider = provider or official_provider()
    if getattr(provider, "settings", None) and provider.settings.http_url != "https://api.tushare.pro":
        raise ValueError("共享下载仅使用官方Tushare")
    fetch = SnapshotFetcher(provider, root / "reference-raw")
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    current = SnapshotFetcher(provider, root / "reference-raw" / (refresh_id or today))
    start, end = pd.Timestamp(spec["start"]), pd.Timestamp(spec["end"])
    left = start - pd.Timedelta(days=spec["warmup"] * 2 + 90)
    calendars = []
    for year in range(left.year, (end + pd.Timedelta(days=20)).year + 1):
        a = f"{year}0101"
        b = f"{year}1231"
        cal = normalize_calendar(fetch.get("trade_cal", exchange="SSE", start_date=a, end_date=b))
        if not pd.DatetimeIndex(cal.date).equals(pd.date_range(a, b)):
            raise ValueError("官方交易日历不完整")
        calendars.append(cal)
    calendar = pd.concat(calendars, ignore_index=True).sort_values("date")
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
    active = sessions[(sessions >= start) & (sessions <= end)]
    if active.empty:
        raise ValueError("范围内没有已完成交易日")
    start, end = active[0], active[-1]
    history = sessions[sessions < start]
    if len(history) < spec["warmup"]:
        raise ValueError("日历不足以满足预热")
    data_start = history[-spec["warmup"]] if spec["warmup"] else start
    stocks = []
    for status in ["L", "D", "P"]:
        part = current.get(
            "stock_basic",
            exchange="",
            list_status=status,
            fields="ts_code,name,industry,list_date,delist_date,list_status",
        )
        if len(part) >= 6000:
            raise ValueError("股票列表触及接口上限，不能视为完整范围")
        if not {"ts_code", "name", "industry", "list_date", "delist_date", "list_status"}.issubset(part):
            raise ValueError("证券主数据字段不完整")
        stocks.append(part)
    stocks = pd.concat(stocks, ignore_index=True)
    if stocks.ts_code.duplicated().any():
        raise ValueError("证券主数据重复，需复核代码变更")
    excluded = int((~stocks.ts_code.map(lambda c: isinstance(c, str) and bool(CODE.fullmatch(c)))).sum())
    stocks = stocks.loc[stocks.ts_code.map(lambda c: isinstance(c, str) and bool(CODE.fullmatch(c)))].copy()
    stocks["listed"] = pd.to_datetime(stocks.list_date, format="%Y%m%d", errors="raise")
    stocks["delisted"] = pd.to_datetime(stocks.delist_date.replace("", None), format="%Y%m%d", errors="coerce")
    if (stocks.delist_date.notna() & stocks.delist_date.ne("") & stocks.delisted.isna()).any():
        raise ValueError("退市日期无效")
    stocks = stocks.loc[stocks.listed.le(end) & (stocks.delisted.isna() | stocks.delisted.gt(start))]
    intervals = pd.DataFrame(columns=["code", "snapshot_date", "effective_from", "effective_until", "weight"])
    if spec["scope"] == "csi300":
        parts = []
        preceding = start.replace(day=1) - pd.Timedelta(days=1)
        for a, b in month_ranges(preceding.replace(day=1).strftime("%Y%m%d"), end.strftime("%Y%m%d")):
            recent_month = (pd.Timestamp(today).replace(day=1) - pd.DateOffset(months=2)).strftime("%Y%m")
            reference = current if a[:6] >= recent_month or spec.get("deep_refresh") else fetch
            month_end = pd.Timestamp(a) + pd.offsets.MonthEnd(0)
            query_end = min(month_end, pd.Timestamp(today)).strftime("%Y%m%d")
            raw = reference.get("index_weight", index_code="399300.SZ", start_date=a, end_date=query_end)
            if len(raw) >= 5000:
                raise ValueError(f"{a[:6]}成分响应触顶，不能视为完整")
            if not raw.empty:
                if not raw.trade_date.astype(str).between(a, query_end).all():
                    raise ValueError("成分日期超出请求范围")
                raw = raw.loc[raw.trade_date.astype(str).le(b)]
            month_sessions = sessions[(sessions >= pd.Timestamp(a)) & (sessions <= month_end)]
            incomplete_month = len(month_sessions) and end < month_sessions[-1]
            if raw.empty:
                if incomplete_month:
                    continue  # Prior snapshot remains the explicitly assumed known pool.
                raise ValueError(f"{a[:6]}完整月份的成分快照缺失，不能用未来名单替代")
            parts.append(raw)
        if not parts:
            raise ValueError("缺少历史成分快照")
        intervals = normalize_memberships(pd.concat(parts, ignore_index=True), calendar)
        if len(members_on(intervals, start)) != INDEX_SIZE:
            raise ValueError("缺少覆盖范围起点的完整历史成分")
        codes = set(historical_codes(intervals, start, end))
        if not codes.issubset(set(stocks.ts_code)):
            raise ValueError("历史成分缺少生命周期资料")
    else:
        codes = set(stocks.ts_code)
    if spec["codes"]:
        if not set(spec["codes"]).issubset(codes):
            raise ValueError("指定股票不在所选范围或日期的生命周期中")
        codes &= set(spec["codes"])
    if not codes:
        raise ValueError("下载范围为空")
    stocks = stocks.loc[stocks.ts_code.isin(codes)].sort_values("ts_code")
    document = shared.catalog(root)
    tasks = []
    reused = missing = 0
    for row in stocks.itertuples():
        expected = sessions[
            (sessions >= max(data_start, row.listed))
            & (sessions <= end)
            & (True if pd.isna(row.delisted) else sessions < row.delisted)
        ]
        if expected.empty:
            continue
        covered = shared.covered_dates(document, row.ts_code, expected, fetch.provider_id)
        revisions = covered if spec.get("deep_refresh") else refresh_dates(expected, covered, refresh_sessions)
        gaps = shared.gap_ranges(expected, covered.difference(revisions))
        reused += len(covered)
        missing += len(expected) - len(covered)
        tasks.append(
            {
                "code": row.ts_code,
                "name": str(row.name),
                "industry": str(row.industry) if pd.notna(row.industry) else "未知",
                "start": expected[0].strftime("%Y%m%d"),
                "end": expected[-1].strftime("%Y%m%d"),
                "gaps": [[a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d")] for a, b in gaps],
                "expected_rows": len(expected),
                "reused_rows": len(covered),
                "refresh_dates": revisions.strftime("%Y-%m-%d").tolist(),
            }
        )
    references = {
        "calendar": calendar,
        "memberships": intervals,
        "instruments": stocks.drop(columns=["listed", "delisted"]),
    }
    benchmark_parts = []
    cache = root / "benchmark" / fetch.provider_id
    for path in cache.glob("*.parquet"):
        if shared.digest(path) != path.stem:
            raise ValueError("共享基准缓存校验失败")
        benchmark_parts.append(pd.read_parquet(path))
    required = sessions[(sessions >= data_start) & (sessions <= end)]
    covered = (
        pd.DatetimeIndex(pd.concat(benchmark_parts, ignore_index=True).date.unique())
        if benchmark_parts
        else pd.DatetimeIndex([])
    )
    benchmark_refresh = covered if spec.get("deep_refresh") else refresh_dates(required, covered, refresh_sessions)
    for left, right in shared.gap_ranges(required, covered.difference(benchmark_refresh)):
        for a, b in year_ranges(left.strftime("%Y%m%d"), right.strftime("%Y%m%d")):
            reference = current if refresh_id else fetch
            frame = normalize_index_daily(reference.get("index_daily", ts_code="000300.SH", start_date=a, end_date=b))
            expected = required[(required >= pd.Timestamp(a)) & (required <= pd.Timestamp(b))]
            if not pd.DatetimeIndex(frame.date).equals(expected):
                raise ValueError("基准增量覆盖不完整")
            temporary = root / "benchmark-pending.parquet"
            _save(temporary, frame)
            path = cache / (shared.digest(temporary) + ".parquet")
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                temporary.replace(path)
            benchmark_parts.append(frame)
    benchmark = (
        pd.concat(benchmark_parts, ignore_index=True)
        .drop_duplicates("date", keep="last" if refresh_id else "first")
        .sort_values("date")
    )
    benchmark = benchmark.loc[benchmark.date.isin(required)].reset_index(drop=True)
    if not pd.DatetimeIndex(benchmark.date).equals(required):
        raise ValueError("基准覆盖不完整")
    references["benchmark"] = benchmark
    temporary = root / "reference-staging"
    with FileLock(root / "prepare.lock"):
        hashes = {}
        for name, frame in references.items():
            _save(temporary / f"{name}.parquet", frame)
            hashes[f"{name}.parquet"] = shared.digest(temporary / f"{name}.parquet")
        plan = {
            "schema_version": POLICY,
            "spec": spec,
            "start": start.strftime("%Y-%m-%d"),
            "end": end.strftime("%Y-%m-%d"),
            "data_start": data_start.strftime("%Y-%m-%d"),
            "provider_id": fetch.provider_id,
            "tasks": tasks,
            "refresh_id": refresh_id,
            "refresh_sessions": refresh_sessions,
            "revision_rows": sum(len(t["refresh_dates"]) for t in tasks),
            "reference_files": hashes,
            "reused_rows": reused,
            "missing_rows": missing,
            "codes_count": len(tasks),
            "estimated_quote_requests": sum(
                len(year_ranges(a.replace("-", ""), b.replace("-", ""))) * 4 for t in tasks for a, b in t["gaps"]
            ),
            "excluded_unsupported": excluded,
            "restricted": bool(spec["codes"]),
            "point_in_time_certified": False,
            "note": "全市场指沪深A股，含范围内已退市证券；北交所及代码变更映射尚未适配。估算不含重试、停牌补查与BaoStock核验；边界可能额外请求一个已知交易日作连续性核验。",
        }
        identifier = sha256_text(json.dumps(plan, sort_keys=True))[:24]
        directory = root / "plans" / identifier
        directory.mkdir(parents=True, exist_ok=True)
        for name in hashes:
            target = directory / "reference" / name
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((temporary / name).read_bytes())
        plan["id"] = identifier
        if not (directory / "plan.json").exists():
            atomic_json(directory / "plan.json", plan)
    return plan


def load_plan(root, identifier):
    path = Path(root) / "plans" / _identifier(identifier)
    plan = _json(path / "plan.json")
    check = {k: v for k, v in plan.items() if k != "id"}
    if sha256_text(json.dumps(check, sort_keys=True))[:24] != identifier:
        raise ValueError("下载计划校验失败")
    for name, sha in plan["reference_files"].items():
        if shared.digest(path / "reference" / name) != sha:
            raise ValueError("下载参考数据校验失败")
    return path, plan


def active(root):
    """The worker holds ``worker.lock`` for the whole run."""
    if not Path(root).exists():
        return False
    try:
        with FileLock(Path(root) / "worker.lock", timeout=0):
            return False
    except Timeout:
        return True


def status(root):
    root = Path(root)
    versions = []
    for path in sorted((root / "versions").glob("*/version.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        versions.append(_json(path))
    jobs = []
    for path in sorted((root / "plans").glob("*/progress.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
        row = _json(path)
        if row["status"] == "running" and not active(root):
            row = {**row, "status": "interrupted", "message": "进程已停止，可续传；旧版本仍可用"}
        jobs.append(row)
    return {
        "worker_running": active(root),
        "versions": versions,
        "jobs": jobs,
        "registered_codes": len(shared.catalog(root)["segments"]),
        "budget": NetworkBudget().status(),
    }


# Extra sessions requested before a segment that starts inside a suspension.
LEADING_HALT_LOOKBACK = (20, 60, 250)


class SegmentFetch:
    """Download and validate one contiguous quote segment into its own capture directory."""

    def __init__(self, root, plan, provider, secondary, calendar, sessions):
        self.root, self.plan, self.provider, self.secondary = root, plan, provider, secondary
        self.calendar, self.sessions = calendar, sessions

    def fetch(self, task, start, end, *, suffix="", job_prefix="shared"):
        """Return ``(record, key, capture_root)``.

        A segment whose first session is a confirmed full-day halt has no prior traded close
        to value the halt with. The start is then moved back over the suspension, by
        progressively larger steps, until the pre-halt close is inside the request.
        """
        first = self.sessions.get_loc(pd.Timestamp(start))
        for extra in (0, *LEADING_HALT_LOOKBACK):
            left = self.sessions[max(0, first - extra)]
            try:
                return self._fetch(task, left, pd.Timestamp(end), suffix, job_prefix)
            except LeadingHaltError:
                if first - extra <= 0:
                    break
        raise ValueError(
            f"{task['code']}在 {pd.Timestamp(start):%Y-%m-%d} 处于停牌，"
            f"向前补查 {LEADING_HALT_LOOKBACK[-1]} 个交易日仍未找到停牌前报价；请把下载开始日期提前后重新预览"
        )

    def _fetch(self, task, left, right, suffix, job_prefix):
        plan = self.plan
        segment = {**task, "start": left.strftime("%Y%m%d"), "end": right.strftime("%Y%m%d")}
        key = (
            sha256_text(
                f"{task['code']}:{segment['start']}:{segment['end']}:{plan['provider_id']}:{plan.get('refresh_id') or ''}"
            )[:24]
            + suffix
        )
        capture = self.root / "captures" / key
        capture.mkdir(parents=True, exist_ok=True)
        # Quote validation has no index membership. Price-limit gaps remain explicit flags.
        intervals = pd.DataFrame(
            {
                "code": pd.Series(dtype=str),
                "effective_from": pd.Series(dtype="datetime64[ns]"),
                "effective_until": pd.Series(dtype="datetime64[ns]"),
            }
        )
        atomic_json(
            capture / "plan.json",
            {
                "start": left.strftime("%Y-%m-%d"),
                "warmup_sessions": 0,
                "reference_files": {"memberships.parquet": POLICY},
            },
        )
        catalog = Catalog(capture)
        job = f"{job_prefix}-{uuid.uuid4().hex}"
        catalog.begin_job(job, segment, 4)
        try:
            fetch = IncrementalFetcher(
                self.provider, capture, catalog, job, False, {"validation": {"mode": "fallback"}}
            )
            fetch.pause_file = self.root / "PAUSE"
            fetch.validation_sha256 = validation_signature()
            if fetch.provider_id != plan["provider_id"]:
                raise ValueError("下载来源与计划不一致")
            record = download_asset(capture, segment, self.calendar, intervals, fetch, self.secondary)
            catalog.finish(job, "complete", {"code": task["code"]})
            return record, key, capture
        finally:
            catalog.close()


def run(root, identifier, *, provider=None, secondary=None):

    root = Path(root).resolve()
    directory, plan = load_plan(root, identifier)
    provider = provider or official_provider()
    secondary = secondary or BaoStockProvider(root / "secondary", timeout=90)
    calendar = pd.read_parquet(directory / "reference/calendar.parquet")
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
    progress = {
        "id": identifier,
        "spec": plan["spec"],
        "status": "running",
        "total": len(plan["tasks"]),
        "completed": 0,
        "failures": [],
        "reused_rows": 0,
        "downloaded_rows": 0,
        "updated_at": utc_now(),
    }

    def save():
        progress["updated_at"] = utc_now()
        atomic_json(directory / "progress.json", progress)

    with FileLock(root / "worker.lock", timeout=0):
        try:
            publish_references(directory, plan)
            save()
            for task in plan["tasks"]:
                if (root / "PAUSE").exists():
                    raise DownloadPaused("用户暂停；已核验分区保留，可续传")
                progress["current_code"] = task["code"]
                save()
                try:
                    expected = sessions[
                        (sessions >= pd.Timestamp(task["start"])) & (sessions <= pd.Timestamp(task["end"]))
                    ]
                    covered = shared.covered_dates(shared.catalog(root), task["code"], expected, plan["provider_id"])
                    progress["reused_rows"] += len(covered)
                    revisions = pd.DatetimeIndex(task.get("refresh_dates", []))
                    ranges = [
                        (pd.Timestamp(a), pd.Timestamp(b))
                        for left, right in shared.gap_ranges(expected, covered.difference(revisions))
                        for a, b in year_ranges(left.strftime("%Y%m%d"), right.strftime("%Y%m%d"))
                    ]
                    expanded = False
                    for a, b in ranges:
                        span = expected[(expected >= a) & (expected <= b)]
                        if span.empty:
                            continue
                        a, b = span[0], span[-1]
                        if (root / "PAUSE").exists():
                            raise DownloadPaused("用户暂停；已核验分区保留，可续传")
                        # Adjacent overlap verifies continuity, without re-fetching the old interval.
                        i = expected.get_loc(a)
                        j = expected.get_loc(b)
                        left = expected[max(0, i - 1)]
                        right = expected[min(len(expected) - 1, j + 1)]
                        segment = SegmentFetch(root, plan, provider, secondary, calendar, sessions)
                        record, key, capture = segment.fetch(task, left, right)
                        if record["start"] != left.strftime("%Y%m%d"):
                            progress.setdefault("repairs", []).append(
                                {
                                    "code": task["code"],
                                    "action": "extended_before_suspension",
                                    "start": record["start"],
                                    "end": record["end"],
                                    "reason": "分段起点处于停牌期，已自动向前补取停牌前的最后报价",
                                }
                            )
                        try:
                            shared.register_segment(
                                root, record, capture / record["file"], batch_id=identifier + "-" + key
                            )
                        except postgres_market.FullHistoryRequired as change:
                            # Validation above established a coherent candidate, but a
                            # scale/source change needs the full known history in one unit.
                            if expanded:
                                raise
                            expanded = True
                            full_start = min(change.start, expected.min())
                            full_end = max(change.end, expected.max())
                            full_sessions = sessions[(sessions >= full_start) & (sessions <= full_end)]
                            if full_sessions.empty or full_sessions[0] != full_start or full_sessions[-1] != full_end:
                                raise ValueError(
                                    f"{task['code']}复权修订超出当前日历，需按已存历史完整日期范围深度复核"
                                ) from None
                            replacement, full_key, full_capture = segment.fetch(
                                task, full_start, full_end, suffix="-full", job_prefix="revision"
                            )
                            shared.register_segment(
                                root,
                                replacement,
                                full_capture / replacement["file"],
                                batch_id=identifier + "-" + full_key,
                            )
                            progress.setdefault("repairs", []).append(
                                {
                                    "code": task["code"],
                                    "action": "full_history_recheck",
                                    "start": replacement["start"],
                                    "end": replacement["end"],
                                    "reason": "复权口径或价格来源修订，已重新校验完整已存历史",
                                }
                            )
                    # Verifies all slices and overlap before declaring a stock covered.
                    shared.read_asset(root, task["code"], expected, plan["provider_id"])
                    progress["downloaded_rows"] += len(expected) - len(covered)
                    progress["completed"] += 1
                except DownloadPaused:
                    raise
                except Exception as error:
                    progress["failures"].append(
                        {
                            "code": task["code"],
                            "type": type(error).__name__,
                            "reason": str(error)[:240]
                            if isinstance(error, ValueError)
                            else "数据源或质量检查失败，请检查分区日志；未替换旧版本",
                        }
                    )
                save()
            if progress["failures"]:
                progress.update(status="partial", message="有待复核数据，未切换当前版本；可续传")
            else:
                version = publish(root, identifier)
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


def publish(root, identifier):
    with shared.market_store().consistent_read():
        return _publish(root, identifier)


def _publish(root, identifier):
    """A manifest of pinned segments. No duplicate market files per stock pool."""
    root = Path(root)
    directory, plan = load_plan(root, identifier)
    assets = {}
    calendar = pd.read_parquet(directory / "reference/calendar.parquet")
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
    for task in plan["tasks"]:
        expected = sessions[(sessions >= pd.Timestamp(task["start"])) & (sessions <= pd.Timestamp(task["end"]))]
        _, sources = shared.read_asset(root, task["code"], expected, plan["provider_id"])
        assets[task["code"]] = {"start": task["start"], "end": task["end"], "sources": sources}
    content = {
        "schema_version": POLICY,
        "spec": plan["spec"],
        "assets": assets,
        "provider_id": plan["provider_id"],
        "reference_files": plan["reference_files"],
    }
    version_id = sha256_text(json.dumps(content, sort_keys=True))[:24]
    counts = {level: 0 for level in ["single_source_passed", "dual_source_verified", "secondary_fallback_passed"]}
    for asset in assets.values():
        levels = {s["validation_level"] for s in asset["sources"]}
        level = (
            "single_source_passed"
            if "single_source_passed" in levels
            else "secondary_fallback_passed"
            if "secondary_fallback_passed" in levels
            else "dual_source_verified"
        )
        counts[level] += 1
    version = {
        **{k: plan[k] for k in ["start", "end", "data_start", "codes_count", "restricted"]},
        "id": version_id,
        "scope": plan["spec"]["scope"],
        "created_at": utc_now(),
        "snapshot_id": None,
    }
    version["validation_counts"] = counts
    target = root / "versions" / version_id
    target.mkdir(parents=True, exist_ok=True)
    if not (target / "manifest.json").exists():
        atomic_json(target / "manifest.json", {**content, "plan_id": identifier})
    else:
        if {k: v for k, v in _json(target / "manifest.json").items() if k != "plan_id"} != content:
            raise ValueError("共享版本冲突")
        version = _json(target / "version.json") if (target / "version.json").exists() else version
    # Existing fixed-pool research supports <=50 securities. Larger archives remain downloadable.
    if len(assets) <= 50:
        version["snapshot_id"] = export_snapshot(root, identifier, version_id)
    if plan["spec"]["scope"] == "csi300" and not plan["restricted"]:
        publish_index_view(root, identifier, version_id)
    if not (target / "version.json").exists():
        atomic_json(target / "version.json", version)
    scope_key = sha256_text(json.dumps({k: plan["spec"][k] for k in ["scope", "codes"]}, sort_keys=True))[:16]
    atomic_json(root / "current" / f"{scope_key}.json", {"id": version_id})
    return version


def publish_index_view(root, identifier, version_id):
    """Full CSI300 versions keep memberships separate; hardlinks reuse canonical composites."""

    root = Path(root)
    directory, plan = load_plan(root, identifier)
    target = root / "versions" / version_id / "index-view"
    if (target / "manifest.json").exists():
        return
    calendar = pd.read_parquet(directory / "reference/calendar.parquet")
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
    assets = {}
    for task in plan["tasks"]:
        expected = sessions[(sessions >= pd.Timestamp(task["start"])) & (sessions <= pd.Timestamp(task["end"]))]
        frame, sources = shared.read_asset(root, task["code"], expected, plan["provider_id"])
        key = sha256_text(json.dumps({"sources": sources, "start": task["start"], "end": task["end"]}, sort_keys=True))[
            :24
        ]
        canonical = root / "composites" / f"{key}.parquet"
        if not canonical.exists():
            _save(canonical, frame)
        path = target / "assets" / f"{task['code']}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            os.link(canonical, path)
        levels = set(frame.validation_level)
        level = (
            "single_source_passed"
            if "single_source_passed" in levels
            else "secondary_fallback_passed"
            if "secondary_fallback_passed" in levels
            else "dual_source_verified"
        )
        assets[task["code"]] = {
            "file": str(path.relative_to(target)).replace("\\", "/"),
            "sha256": shared.digest(path),
            "start": task["start"],
            "end": task["end"],
            "rows": len(frame),
            "validation_level": level,
        }
    for name in plan["reference_files"]:
        path = target / "reference" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            os.link(directory / "reference" / name, path)
    manifest = {
        "schema_version": "historical-index-warehouse-v1",
        "index": "399300.SZ",
        "start": plan["start"],
        "end": plan["end"],
        "data_start": plan["data_start"],
        "warmup_sessions": plan["spec"]["warmup"],
        "provider_id": plan["provider_id"],
        "membership_policy": "monthly_snapshot_next_session; NOT exact announcement effective dates",
        "point_in_time_certified": False,
        "assets": assets,
        "reference_files": plan["reference_files"],
        "rows": sum(a["rows"] for a in assets.values()),
        "shared_version": version_id,
        "created_at": utc_now(),
    }
    atomic_json(target / "manifest.json", manifest)


def warehouse_roots(data_root):
    """Complete CSI300 versions published by the shared download, newest first."""
    published = [
        path.parent
        for path in (shared_root(data_root) / "versions").glob("*/index-view/manifest.json")
        if (path.parent.parent / "version.json").exists()
    ]
    published.sort(key=lambda view: (view.parent / "version.json").stat().st_mtime, reverse=True)
    return [view.resolve() for view in published]


def export_snapshot(root, identifier, version_id):
    """Small selectable research projections; official quotes stay shared and immutable."""

    root = Path(root)
    directory, plan = load_plan(root, identifier)
    try:
        validate_execution_universe([t["code"] for t in plan["tasks"]])
    except ValueError:
        return None  # STAR/CDR quotes are archived; fixed-pool execution does not support them.
    calendar = pd.read_parquet(directory / "reference/calendar.parquet")
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
    parts = []
    for task in plan["tasks"]:
        expected = sessions[(sessions >= pd.Timestamp(task["start"])) & (sessions <= pd.Timestamp(task["end"]))]
        frame, _ = shared.read_asset(root, task["code"], expected, plan["provider_id"])
        parts.append(frame)
    market = pd.concat(parts, ignore_index=True)
    benchmark = pd.read_parquet(directory / "reference/benchmark.parquet")
    # This projection is explicitly a fixed user-selected pool, not a dynamic all-market backtest.
    selected = sessions[(sessions >= pd.Timestamp(plan["data_start"])) & (sessions <= pd.Timestamp(plan["end"]))]
    coverage = coverage_report(market, benchmark, selected, tuple(t["code"] for t in plan["tasks"]), "000300.SH")
    if coverage["status"] != "complete":
        return None
    destination = root.parent / "research/downloads" / ("shared-" + version_id)
    destination.mkdir(parents=True, exist_ok=True)
    snapshot = destination / "snapshots" / version_id
    snapshot.mkdir(parents=True, exist_ok=True)
    manifest_path = snapshot / "manifest.json"
    if manifest_path.exists():
        return load_market(snapshot)[3]["id"]
    for name, frame in [("market", market), ("benchmark", benchmark), ("calendar", calendar)]:
        _save(snapshot / f"{name}.parquet", frame)
    manifest = {
        "schema_version": "market-v4-shared",
        "source": "official_tushare_shared",
        "id": "shared-" + version_id,
        "provider_id": plan["provider_id"],
        "codes": sorted(t["code"] for t in plan["tasks"]),
        "start": plan["data_start"].replace("-", ""),
        "end": plan["end"].replace("-", ""),
        "benchmark": "000300.SH",
        "trading_days": len(selected),
        "rows": len(market),
        "is_synthetic": False,
        "coverage": coverage,
        "files": {
            f"{name}.parquet": shared.digest(snapshot / f"{name}.parquet")
            for name in ["market", "benchmark", "calendar"]
        },
        "shared_version": version_id,
        "universe_type": "fixed_user_selection",
        "point_in_time_certified": False,
        "validation_counts": shared.validation_counts(market),
        "snapshot_path": f"snapshots/{version_id}",
        "notes": [
            "共享行情的固定股票池研究投影，不能当成历史全市场或沪深300动态池",
            "逐行保留单源／双源／补缺等级，历史估值修订和公开时刻未认证",
        ],
    }
    shared.validate_snapshot(market, manifest)
    atomic_json(manifest_path, manifest)
    atomic_json(destination / "manifest.json", manifest)
    return manifest["id"]
