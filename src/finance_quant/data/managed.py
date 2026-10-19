"""Partitioned downloads, resumable work and atomic validated snapshots."""

from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
from filelock import FileLock, Timeout

from finance_quant.configuration import DataConfig
from finance_quant.data.catalog import Catalog, utc_now
from finance_quant.data.quality import adjustment_discontinuities
from finance_quant.data.providers import (
    TushareProvider, TushareSettings, TushareProviderError, normalize_adj_factor, normalize_calendar,
    normalize_daily, normalize_daily_basic, normalize_index_daily,
)


class DatasetDownloadError(RuntimeError):
    pass


def month_ranges(start: str, end: str) -> list[tuple[str, str]]:
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    result = []
    while first <= last:
        boundary = min(first + pd.offsets.MonthEnd(0), last)
        result.append((first.strftime("%Y%m%d"), boundary.strftime("%Y%m%d")))
        first = boundary + pd.Timedelta(days=1)
    return result


def year_ranges(start: str, end: str) -> list[tuple[str, str]]:
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    result = []
    while first <= last:
        boundary = min(pd.Timestamp(year=first.year, month=12, day=31), last)
        result.append((first.strftime("%Y%m%d"), boundary.strftime("%Y%m%d")))
        first = boundary + pd.Timedelta(days=1)
    return result


def atomic_json(path: Path, document: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(document, file, ensure_ascii=False, allow_nan=False, indent=2)
        except BaseException:
            file.close()
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def instrument(code: str) -> str:
    return code[-2:] + code[:6]


def normalize_response(api: str, raw: pd.DataFrame, params: dict) -> pd.DataFrame:
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        raise ValueError("Required response is empty")
    normalizers = {"daily": normalize_daily, "adj_factor": normalize_adj_factor,
                   "daily_basic": normalize_daily_basic, "trade_cal": normalize_calendar,
                   "index_daily": normalize_index_daily}
    if api in normalizers:
        frame = normalizers[api](raw)
    elif api == "stk_limit":
        frame = raw.copy()
        frame["instrument"] = frame["ts_code"].map(instrument)
        frame["date"] = pd.to_datetime(frame["trade_date"], format="%Y%m%d", errors="raise")
        for column in ["up_limit", "down_limit"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame[["instrument", "date", "up_limit", "down_limit"]]
    elif api == "stock_basic":
        if set(raw["ts_code"]) != {params["ts_code"]} or len(raw) != 1 or pd.isna(raw.iloc[0]["name"]):
            raise ValueError("Invalid current stock metadata")
        return raw.copy()
    else:
        raise ValueError("Unsupported managed dataset")
    if "instrument" in frame and set(frame["instrument"]) != {instrument(params["ts_code"])}:
        raise ValueError("Response instrument mismatch")
    if frame["date"].duplicated().any():
        raise ValueError("Duplicate dates in response")
    start = params.get("trade_date", params.get("start_date"))
    end = params.get("trade_date", params.get("end_date"))
    if not frame["date"].between(pd.Timestamp(start), pd.Timestamp(end)).all():
        raise ValueError("Response dates outside request bounds")
    return frame.sort_values("date").reset_index(drop=True)


class ManagedFetcher:
    def __init__(self, provider, root: Path, catalog: Catalog, job_id: str, refresh: bool, scope: dict):
        self.provider, self.root, self.catalog, self.job_id, self.refresh = provider, root, catalog, job_id, refresh
        settings = getattr(provider, "settings", None)
        origin = settings.http_url if settings else f"offline:{type(provider).__module__}.{type(provider).__qualname__}"
        self.provider_id = hashlib.sha256(origin.encode()).hexdigest()
        self.generation = "base"
        manifest_path = root / 'manifest.json'
        if manifest_path.exists():
            current = json.loads(manifest_path.read_text(encoding='utf-8'))
            if current.get('provider_id') == self.provider_id:
                self.generation = current.get('cache_generation', 'base')
        if refresh:
            scope_hash = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
            pending_path = root / 'pending-refresh.json'
            pending = json.loads(pending_path.read_text(encoding='utf-8')) if pending_path.exists() else {}
            if pending.get('provider_id') == self.provider_id and pending.get('scope') == scope_hash:
                self.generation = pending['generation']
            else:
                self.generation = uuid.uuid4().hex
                atomic_json(pending_path, {'provider_id': self.provider_id, 'scope': scope_hash, 'generation': self.generation})
        self.requests, self.errors = [], []
        (root / "raw").mkdir(exist_ok=True)

    def key(self, api: str, params: dict) -> tuple[dict, str]:
        if api not in {"trade_cal", "daily", "adj_factor", "daily_basic", "stk_limit", "stock_basic", "index_daily"}:
            raise ValueError("Unsupported managed API")
        if set(params) - {"ts_code", "exchange", "start_date", "end_date", "trade_date"}:
            raise ValueError("Unsupported request parameters; credentials belong only in provider settings")
        specification = {"api": api, "params": params, "provider_id": self.provider_id, "normalizer": "managed-v1"}
        if self.generation != 'base':
            specification['cache_generation'] = self.generation
        key = hashlib.sha256(json.dumps(specification, sort_keys=True).encode()).hexdigest()[:24]
        return specification, key

    def get_many(self, requests: list[tuple[str, dict]]) -> list[pd.DataFrame | None]:
        # Only HTTP runs in workers. SQLite, validation and publication stay on the calling thread.
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = []
            for api, params in requests:
                previous = self.catalog.partition(self.key(api, params)[1])
                cached = previous and previous["status"] == "succeeded"
                futures.append(None if cached else executor.submit(self.request, api, params))
            return [self.get(api, _future=future, **params) for (api, params), future in zip(requests, futures)]

    def request(self, api: str, params: dict) -> pd.DataFrame:
        # This compatible service also returns intermittent unclassified provider errors.
        # Retry those once; permission/unsupported errors and invalid content are never retried here.
        try:
            return self.provider.query(api, **params)
        except TushareProviderError as exc:
            if exc.category != "provider":
                raise
            time.sleep(1)
            return self.provider.query(api, **params)

    def get(self, api: str, *, _future=None, **params) -> pd.DataFrame | None:
        specification, key = self.key(api, params)
        previous = self.catalog.partition(key)
        attempts = previous["attempts"] if previous else 0
        cached = previous and previous["status"] == "succeeded"
        metadata = None
        stage = "request_failed"
        try:
            if cached:
                stage = "cache_corrupt"
                path = (self.root / previous["raw_path"]).resolve()
                if not path.is_relative_to(self.root.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != previous["sha256"]:
                    raise ValueError("Raw cache checksum mismatch")
                raw = pd.read_parquet(path)
                metadata = {field: previous[field] for field in ["rows", "fetched_at", "sha256", "raw_path"]}
            else:
                attempts += 1
                raw = _future.result() if _future is not None else self.request(api, params)
                stage = "invalid_response"
                if not isinstance(raw, pd.DataFrame):
                    raise ValueError("Response is not a table")
                relative = f"raw/{api}-{key}-{uuid.uuid4().hex[:10]}.parquet"
                path = self.root / relative
                temporary = path.with_suffix(".tmp")
                raw.to_parquet(temporary, index=False)
                temporary.replace(path)
                metadata = {"rows": len(raw), "fetched_at": utc_now(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "raw_path": relative}
                atomic_json(path.with_suffix(".json"), {**specification, **metadata})
            stage = "invalid_response" if not cached else "cache_corrupt"
            frame = normalize_response(api, raw, params)
            self.catalog.save_partition(key, api, params, self.provider_id, status="succeeded", attempts=attempts, metadata=metadata)
            self.catalog.record(self.job_id, key, status="succeeded", cached=bool(cached))
            self.requests.append({**specification, **metadata})
            print(f"{api} {params.get('ts_code', 'calendar')} {params.get('start_date', params.get('trade_date', ''))}: {'cache' if cached else 'fetched'} {len(frame)}", flush=True)
            return frame
        except Exception as exc:
            if getattr(exc, "category", None) == "transport":
                stage = "network_failed"
            elif getattr(exc, "category", None) in {"rate_limit", "access_denied", "unsupported"}:
                stage = exc.category
            # Never persist exception text or provider bodies: either can contain credentials.
            self.catalog.save_partition(key, api, params, self.provider_id, status="failed", attempts=attempts, metadata=metadata, error=stage)
            self.catalog.record(self.job_id, key, status="failed", error=stage)
            self.errors.append({"api": api, "params": params, "error": stage})
            print(f"{api} {params.get('ts_code', 'calendar')}: {stage}", flush=True)
            return None


def repair_missing_dates(fetch: ManagedFetcher, api: str, code: str, start: str, end: str,
                         frame: pd.DataFrame, expected: pd.DatetimeIndex) -> pd.DataFrame:
    """Re-query actual missing records; never infer adjustment factors by filling."""
    extra = pd.DatetimeIndex(frame["date"]).difference(expected)
    if len(extra):
        fetch.errors.append({"api": api, "params": {"ts_code": code, "start_date": start, "end_date": end},
                             "error": "extra_sessions", "extra_dates": extra.strftime("%Y-%m-%d").tolist()})
        return frame
    missing = expected.difference(pd.DatetimeIndex(frame["date"]))
    if not len(missing):
        return frame
    # Monthly responses have at most 31 dates. Large gaps need human/source diagnosis.
    if len(missing) <= 31:
        fetch.catalog.add_total(fetch.job_id, len(missing))
        repairs = fetch.get_many([(api, {"ts_code": code, "trade_date": date.strftime("%Y%m%d")}) for date in missing])
        frame = pd.concat([frame, *[part for part in repairs if part is not None]], ignore_index=True).sort_values("date")
    remaining = expected.difference(pd.DatetimeIndex(frame["date"]))
    if len(remaining):
        fetch.errors.append({"api": api, "params": {"ts_code": code, "start_date": start, "end_date": end},
                             "error": "missing_sessions", "missing_dates": remaining.strftime("%Y-%m-%d").tolist()})
    return frame


def abort_failed(fetch: ManagedFetcher, *, final: bool = False):
    if not fetch.errors:
        return
    transient = {'request_failed', 'network_failed', 'missing_sessions'}
    if not final and all(error['error'] in transient for error in fetch.errors):
        return
    status = "partial" if any(error["error"] in {"missing_sessions", "extra_sessions"} for error in fetch.errors) else "failed"
    report = {"status": status, "reason": "request_partitions_failed", "failures": fetch.errors}
    fetch.catalog.finish(fetch.job_id, status, report)
    raise DatasetDownloadError(f"Download {fetch.job_id} incomplete; run status to inspect failed partitions and missing dates, then retry")


def coverage_report(market: pd.DataFrame, benchmark: pd.DataFrame, sessions: pd.DatetimeIndex,
                    codes: tuple[str, ...], benchmark_code: str) -> dict:
    def dates(values):
        return sorted(pd.DatetimeIndex(values).strftime("%Y-%m-%d").tolist())

    def assess(frame, code, stock):
        actual = pd.DatetimeIndex(frame["date"])
        missing, extra = sessions.difference(actual), actual.difference(sessions)
        invalid = pd.Series(False, index=frame.index)
        required = ["open", "high", "low", "close", "volume"] + (["adj_factor", "up_limit", "down_limit"] if stock else [])
        values = frame[required].to_numpy(dtype=float)
        invalid |= ~np.isfinite(values).all(axis=1) | (values <= 0).any(axis=1)
        invalid |= frame["high"] < frame[["open", "close", "low"]].max(axis=1)
        invalid |= frame["low"] > frame[["open", "close", "high"]].min(axis=1)
        if stock:
            invalid |= frame["up_limit"] <= frame["down_limit"]
        duplicate = frame.loc[frame["date"].duplicated(keep=False), "date"]
        result = {"code": code, "expected": len(sessions), "actual": len(actual.unique()),
                  "missing_dates": dates(missing), "extra_dates": dates(extra),
                  "duplicate_dates": dates(duplicate.unique()), "invalid_dates": dates(frame.loc[invalid, "date"])}
        if stock:
            valid_pe = np.isfinite(frame["pe_ttm"]) & (frame["pe_ttm"] > 0)
            result["missing_valuation_dates"] = dates(frame.loc[~valid_pe, "date"])
        result["complete"] = not any(result[key] for key in ["missing_dates", "extra_dates", "duplicate_dates", "invalid_dates"])
        return result

    anomalies = adjustment_discontinuities(market)
    stocks = [assess(market.loc[market["instrument"] == instrument(code)], code, True) for code in codes]
    for row in stocks:
        rejected = [item['date'] for item in anomalies if item['instrument'] == instrument(row['code'])]
        row['invalid_dates'] = sorted(set(row['invalid_dates'] + rejected))
        row['complete'] = row['complete'] and not rejected
    index = assess(benchmark, benchmark_code, False)
    return {"status": "complete" if all(row["complete"] for row in [*stocks, index]) and len(sessions) > 0 else "incomplete",
            "expected_sessions": len(sessions), "stock_count": len(codes), "stocks": stocks, "benchmark": index, 'adjustment_anomalies': anomalies,
            "note": "缺失交易日不直接推断为停牌；复权连续性异常需核查公司行动或数据口径，不自动缩放、补值或剔除股票"}


def publish_snapshot(root: Path, config: DataConfig, market: pd.DataFrame, benchmark: pd.DataFrame,
                     calendar: pd.DataFrame, coverage: dict, fetch: ManagedFetcher) -> dict:
    directory = root / "snapshots"
    directory.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=directory))
    try:
        for name, frame in [("market", market), ("benchmark", benchmark), ("calendar", calendar)]:
            frame.to_parquet(staging / f"{name}.parquet", index=False)
        content = {name: hashlib.sha256((staging / name).read_bytes()).hexdigest()
                   for name in ["market.parquet", "benchmark.parquet", "calendar.parquet"]}
        requests = sorted(fetch.requests, key=lambda item: json.dumps({"api": item["api"], "params": item["params"]}, sort_keys=True))
        manifest = {"schema_version": "market-v2", "source": "third_party_tushare_compatible", "provider_id": fetch.provider_id,
                    "is_synthetic": False, "start": config.start, "end": config.end, "codes": list(config.codes), "cache_generation": fetch.generation,
                    "benchmark": config.benchmark, "universe_type": "fixed_user_selection",
                    "trading_days": coverage["expected_sessions"], "rows": len(market), "files": content, "requests": requests,
                    "coverage": coverage, "units": {"volume": "shares", "amount": "CNY", "total_mv": "CNY", "change": "fraction"},
                    "notes": ["用户固定股票池，不是历史指数成分；可能存在选股与幸存者偏差", "名称和行业来自当前资料，仅展示",
                              "第三方历史数据与估值发布时间/修订未经独立核实；不使用财报因子"]}
        version = hashlib.sha256(json.dumps(manifest, sort_keys=True, allow_nan=False).encode()).hexdigest()[:16]
        manifest.update(id=f"tushare-{version}", snapshot_path=f"snapshots/{version}")
        atomic_json(staging / "manifest.json", manifest)
        target = directory / version
        if target.exists():
            if json.loads((target / "manifest.json").read_text(encoding="utf-8")) != manifest or any(hashlib.sha256((target / name).read_bytes()).hexdigest() != digest for name, digest in content.items()):
                raise ValueError("Immutable snapshot conflicts with prior content")
        else:
            staging.rename(target)
        atomic_json(root / "manifest.json", manifest)
        return manifest
    finally:
        # Delete only our own known staging folder, never any published snapshot.
        if staging.exists():
            for path in staging.iterdir():
                path.unlink()
            staging.rmdir()


def download_dataset(config: DataConfig, *, provider=None, refresh: bool = False) -> dict:
    root = config.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(root / ".download.lock", timeout=0):
            return _download_locked(config, provider, refresh)
    except Timeout:
        raise DatasetDownloadError("Another download is running in this data directory") from None


def _download_locked(config: DataConfig, provider, refresh: bool) -> dict:
    root = config.root.resolve()
    provider = provider or TushareProvider(TushareSettings.from_env())
    catalog = Catalog(root)
    job_id = f"download-{uuid.uuid4().hex[:12]}"
    specification = {"start": config.start, "end": config.end, "codes": list(config.codes), "benchmark": config.benchmark, "refresh": refresh, "partition": "year"}
    calendar_end = (pd.Timestamp(config.end) + pd.Timedelta(days=20)).strftime("%Y%m%d")
    calendar_ranges = month_ranges(config.start, calendar_end)
    ranges = year_ranges(config.start, config.end)
    catalog.begin_job(job_id, specification, len(calendar_ranges) + len(config.codes) * (1 + 4 * len(ranges)) + len(ranges))
    fetch = None
    try:
        fetch = ManagedFetcher(provider, root, catalog, job_id, refresh, specification)
        calendar_parts = fetch.get_many([("trade_cal", {"exchange": "SSE", "start_date": start, "end_date": end}) for start, end in calendar_ranges])
        if any(part is None for part in calendar_parts):
            raise DatasetDownloadError("Calendar download incomplete")
        calendar = pd.concat(calendar_parts, ignore_index=True).sort_values("date")
        expected_calendar = pd.date_range(config.start, calendar_end)
        if not pd.DatetimeIndex(calendar["date"]).equals(expected_calendar):
            raise DatasetDownloadError("Official calendar dates are incomplete or duplicated")
        sessions = pd.DatetimeIndex(calendar.loc[calendar["is_open"], "date"])
        expected = sessions[(sessions >= pd.Timestamp(config.start)) & (sessions <= pd.Timestamp(config.end))]
        if len(expected) < 25 or not (sessions > pd.Timestamp(config.end)).any():
            raise DatasetDownloadError("Not enough official sessions for factor warm-up and backtest boundary")
        ranges = [(start, end) for start, end in ranges if expected.to_series().between(pd.Timestamp(start), pd.Timestamp(end)).any()]
        catalog.set_total(job_id, len(calendar_ranges) + len(config.codes) * (1 + 4 * len(ranges)) + len(ranges))
        frames, benchmark_parts = [], []
        for code in config.codes:
            name = fetch.get("stock_basic", ts_code=code)
            abort_failed(fetch)
            parts = {api: [] for api in ["daily", "adj_factor", "daily_basic", "stk_limit"]}
            for start, end in ranges:
                monthly = fetch.get_many([(api, {"ts_code": code, "start_date": start, "end_date": end}) for api in parts])
                abort_failed(fetch)
                for api, frame in zip(parts, monthly):
                    if frame is not None:
                        checked = []
                        for month_start, month_end in month_ranges(start, end):
                            month_sessions = expected[(expected >= pd.Timestamp(month_start)) & (expected <= pd.Timestamp(month_end))]
                            section = frame.loc[frame["date"].between(pd.Timestamp(month_start), pd.Timestamp(month_end))]
                            checked.append(repair_missing_dates(fetch, api, code, month_start, month_end, section, month_sessions))
                            abort_failed(fetch)
                        frame = pd.concat(checked, ignore_index=True)
                        parts[api].append(frame)
                abort_failed(fetch)
            if name is None or any(not rows for rows in parts.values()):
                continue
            joined = pd.concat(parts["daily"], ignore_index=True)
            for api in ["adj_factor", "daily_basic", "stk_limit"]:
                joined = joined.merge(pd.concat(parts[api], ignore_index=True), on=["instrument", "date"], how="left", validate="one_to_one")
            joined["name"], joined["industry"] = str(name.iloc[0]["name"]), str(name.iloc[0].get("industry", "未提供"))
            frames.append(joined)
        for start, end in ranges:
            frame = fetch.get("index_daily", ts_code=config.benchmark, start_date=start, end_date=end)
            if frame is not None:
                checked = []
                for month_start, month_end in month_ranges(start, end):
                    month_sessions = expected[(expected >= pd.Timestamp(month_start)) & (expected <= pd.Timestamp(month_end))]
                    section = frame.loc[frame["date"].between(pd.Timestamp(month_start), pd.Timestamp(month_end))]
                    checked.append(repair_missing_dates(fetch, "index_daily", config.benchmark, month_start, month_end, section, month_sessions))
                    abort_failed(fetch)
                frame = pd.concat(checked, ignore_index=True)
                benchmark_parts.append(frame)
            abort_failed(fetch)
        abort_failed(fetch, final=True)
        market = pd.concat(frames, ignore_index=True).sort_values(["instrument", "date"]).reset_index(drop=True)
        benchmark = pd.concat(benchmark_parts, ignore_index=True).sort_values("date").reset_index(drop=True)
        coverage = coverage_report(market, benchmark, expected, config.codes, config.benchmark)
        atomic_json(root / "reports" / f"{job_id}.json", coverage)
        if coverage["status"] != "complete":
            catalog.finish(job_id, "partial", coverage)
            raise DatasetDownloadError(f"Download {job_id} failed market quality checks (coverage, prices or adjustment); previous published snapshot retained. Run status for details")
        manifest = publish_snapshot(root, config, market, benchmark, calendar, coverage, fetch)
        catalog.add_snapshot(manifest)
        catalog.finish(job_id, "succeeded", coverage, manifest["id"])
        if refresh:
            (root / 'pending-refresh.json').unlink(missing_ok=True)
        return {"job_id": job_id, "manifest": manifest}
    except BaseException as exc:
        current = catalog.job_status(job_id)
        if current == "running":
            interrupted = isinstance(exc, (KeyboardInterrupt, SystemExit))
            catalog.finish(job_id, "interrupted" if interrupted else "failed",
                           {"status": "interrupted" if interrupted else "failed", "reason": "download_interrupted" if interrupted else "validation_or_publication_failed",
                            "failures": fetch.errors if fetch else []})
        raise
    finally:
        catalog.close()
