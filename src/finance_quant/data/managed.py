"""Resumable, checksummed provider captures shared by the market and index downloads."""

from __future__ import annotations

import json
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from finance_quant.checksums import file_sha256, sha256_text
from finance_quant.data.catalog import Catalog, utc_now
from finance_quant.data.network_budget import DownloadPaused
from finance_quant.data.providers import (
    TushareProviderError,
    normalize_adj_factor,
    normalize_calendar,
    normalize_daily,
    normalize_daily_basic,
    normalize_index_daily,
)
from finance_quant.data.quality import (
    adjustment_discontinuities,
    published_adjustment_discontinuities,
    published_event_mask,
)
from finance_quant.data.suspensions import invalid_halt_rows
from finance_quant.data.symbols import to_qlib


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
    try:
        # Windows readers and virus scanners can briefly hold a handle without
        # FILE_SHARE_DELETE. Keep the previous complete document until replace succeeds.
        for attempt in range(21):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 20:
                    raise
                time.sleep(0.05)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # Do not hide a persistent replace error with cleanup failure.


def instrument(code: str) -> str:
    return to_qlib(code)


def normalize_response(api: str, raw: pd.DataFrame, params: dict, *, allow_empty=False) -> pd.DataFrame:
    if not isinstance(raw, pd.DataFrame) or (raw.empty and not allow_empty):
        raise ValueError("Required response is empty")
    normalizers = {
        "daily": normalize_daily,
        "adj_factor": normalize_adj_factor,
        "daily_basic": normalize_daily_basic,
        "trade_cal": normalize_calendar,
        "index_daily": normalize_index_daily,
    }
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
    if not frame.empty and "instrument" in frame and set(frame["instrument"]) != {instrument(params["ts_code"])}:
        raise ValueError("Response instrument mismatch")
    if frame["date"].duplicated().any():
        raise ValueError("Duplicate dates in response")
    start = params.get("trade_date", params.get("start_date"))
    end = params.get("trade_date", params.get("end_date"))
    if not frame["date"].between(pd.Timestamp(start), pd.Timestamp(end)).all():
        raise ValueError("Response dates outside request bounds")
    return frame.sort_values("date").reset_index(drop=True)


def provider_identifier(provider) -> str:
    settings = getattr(provider, "settings", None)
    origin = settings.http_url if settings else f"offline:{type(provider).__module__}.{type(provider).__qualname__}"
    return sha256_text(origin)


class ManagedFetcher:
    def __init__(
        self,
        provider,
        root: Path,
        catalog: Catalog,
        job_id: str,
        refresh: bool,
        scope: dict,
    ):
        self.provider, self.root, self.catalog, self.job_id, self.refresh = provider, root, catalog, job_id, refresh
        self.provider_id = provider_identifier(provider)
        self.generation = "base"
        manifest_path = root / "manifest.json"
        if manifest_path.exists():
            current = json.loads(manifest_path.read_text(encoding="utf-8"))
            if current.get("provider_id") == self.provider_id:
                self.generation = current.get("cache_generation", "base")
        if refresh:
            scope_hash = sha256_text(json.dumps(scope, sort_keys=True))
            pending_path = root / "pending-refresh.json"
            pending = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else {}
            if pending.get("provider_id") == self.provider_id and pending.get("scope") == scope_hash:
                self.generation = pending["generation"]
            else:
                self.generation = uuid.uuid4().hex
                atomic_json(
                    pending_path, {"provider_id": self.provider_id, "scope": scope_hash, "generation": self.generation}
                )
        self.requests, self.errors = [], []
        self.fallback_apis = {"daily", "adj_factor"} if scope.get("validation", {}).get("mode") == "fallback" else set()
        self.allow_empty_stock = scope.get("validation", {}).get("mode") in {"verify", "fallback"}
        (root / "raw").mkdir(exist_ok=True)

    def cached_partition(self, key: str):
        """Return the ledger row for ``key`` and the capture root holding its raw file."""
        return self.catalog.partition(key), self.root

    def key(self, api: str, params: dict) -> tuple[dict, str]:
        if api not in {"trade_cal", "daily", "adj_factor", "daily_basic", "stk_limit", "stock_basic", "index_daily"}:
            raise ValueError("Unsupported managed API")
        if set(params) - {"ts_code", "exchange", "start_date", "end_date", "trade_date"}:
            raise ValueError("Unsupported request parameters; credentials belong only in provider settings")
        specification = {"api": api, "params": params, "provider_id": self.provider_id, "normalizer": "managed-v1"}
        if self.generation != "base":
            specification["cache_generation"] = self.generation
        key = sha256_text(json.dumps(specification, sort_keys=True))[:24]
        return specification, key

    def get_many(self, requests: list[tuple[str, dict]]) -> list[pd.DataFrame | None]:
        # Only HTTP runs in workers. SQLite, validation and publication stay on the calling thread.
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = []
            for api, params in requests:
                previous, _ = self.cached_partition(self.key(api, params)[1])
                cached = previous and previous["status"] == "succeeded"
                futures.append(None if cached else executor.submit(self.request, api, params))
            return [
                self.get(api, _future=future, **params) for (api, params), future in zip(requests, futures, strict=True)
            ]

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
        previous, cache_root = self.cached_partition(key)
        attempts = previous["attempts"] if previous else 0
        cached = previous and previous["status"] == "succeeded"
        metadata = None
        stage = "request_failed"
        try:
            if cached:
                stage = "cache_corrupt"
                path = (cache_root / previous["raw_path"]).resolve()
                if not path.is_relative_to(cache_root.resolve()) or file_sha256(path) != previous["sha256"]:
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
                metadata = {
                    "rows": len(raw),
                    "fetched_at": utc_now(),
                    "sha256": file_sha256(path),
                    "raw_path": relative,
                }
                atomic_json(path.with_suffix(".json"), {**specification, **metadata})
            stage = "invalid_response" if not cached else "cache_corrupt"
            # An empty capture is not usable market data. It may, however, cover
            # a wholly suspended partition; the later independent status check
            # and full-calendar coverage gate must still prove every date.
            frame = normalize_response(
                api,
                raw,
                params,
                allow_empty=self.allow_empty_stock and api in {"daily", "daily_basic", "adj_factor", "stk_limit"},
            )
            self.catalog.save_partition(
                key, api, params, self.provider_id, status="succeeded", attempts=attempts, metadata=metadata
            )
            self.catalog.record(self.job_id, key, status="succeeded", cached=bool(cached))
            self.requests.append({**specification, **metadata})
            print(
                f"{api} {params.get('ts_code', 'calendar')} {params.get('start_date', params.get('trade_date', ''))}: {'cache' if cached else 'fetched'} {len(frame)}",
                flush=True,
            )
            return frame
        except Exception as exc:
            if isinstance(exc, DownloadPaused):
                raise
            if api == "stock_basic" and stage == "invalid_response":
                stage = "stock_metadata_unavailable"
            if getattr(exc, "category", None) == "transport":
                stage = "network_failed"
            elif getattr(exc, "category", None) in {"rate_limit", "access_denied", "unsupported"}:
                stage = exc.category
            # Never persist exception text or provider bodies: either can contain credentials.
            self.catalog.save_partition(
                key, api, params, self.provider_id, status="failed", attempts=attempts, metadata=metadata, error=stage
            )
            self.catalog.record(self.job_id, key, status="failed", error=stage)
            self.errors.append({"api": api, "params": params, "error": stage})
            print(f"{api} {params.get('ts_code', 'calendar')}: {stage}", flush=True)
            return None


def repair_missing_dates(
    fetch: ManagedFetcher, api: str, code: str, start: str, end: str, frame: pd.DataFrame, expected: pd.DatetimeIndex
) -> pd.DataFrame:
    """Re-query actual missing records; never infer adjustment factors by filling."""
    extra = pd.DatetimeIndex(frame["date"]).difference(expected)
    if len(extra):
        fetch.errors.append(
            {
                "api": api,
                "params": {"ts_code": code, "start_date": start, "end_date": end},
                "error": "extra_sessions",
                "extra_dates": extra.strftime("%Y-%m-%d").tolist(),
            }
        )
        return frame
    missing = expected.difference(pd.DatetimeIndex(frame["date"]))
    if not len(missing):
        return frame
    # Monthly responses have at most 31 dates. Large gaps need human/source diagnosis.
    if len(missing) <= 31:
        requests = [(api, {"ts_code": code, "trade_date": date.strftime("%Y%m%d")}) for date in missing]
        if api in fetch.fallback_apis:
            # Reuse prior independently captured daily records, but do not issue
            # more primary network calls before the secondary fallback.
            requests = [
                (name, params)
                for name, params in requests
                if (cached := fetch.cached_partition(fetch.key(name, params)[1])[0]) and cached["status"] == "succeeded"
            ]
        fetch.catalog.add_total(fetch.job_id, len(requests))
        repairs = fetch.get_many(requests)
        frame = pd.concat([frame, *[part for part in repairs if part is not None]], ignore_index=True).sort_values(
            "date"
        )
    remaining = expected.difference(pd.DatetimeIndex(frame["date"]))
    if len(remaining):
        fetch.errors.append(
            {
                "api": api,
                "params": {"ts_code": code, "start_date": start, "end_date": end},
                "error": "missing_sessions",
                "missing_dates": remaining.strftime("%Y-%m-%d").tolist(),
            }
        )
    return frame


def coverage_report(
    market: pd.DataFrame,
    benchmark: pd.DataFrame,
    sessions: pd.DatetimeIndex,
    codes: tuple[str, ...],
    benchmark_code: str,
    *,
    validated_shared: bool = False,
) -> dict:
    def dates(values):
        return sorted(pd.DatetimeIndex(values).strftime("%Y-%m-%d").tolist())

    def assess(frame, code, stock):
        actual = pd.DatetimeIndex(frame["date"])
        missing, extra = sessions.difference(actual), actual.difference(sessions)
        invalid = pd.Series(False, index=frame.index)
        required = ["open", "high", "low", "close"] + (["adj_factor"] if stock else [])
        values = frame[required].to_numpy(dtype=float)
        invalid |= ~np.isfinite(values).all(axis=1) | (values <= 0).any(axis=1)
        halted = (
            frame.get("is_suspended", pd.Series(False, index=frame.index))
            if stock
            else pd.Series(False, index=frame.index)
        )
        invalid |= ~np.isfinite(frame.volume) | frame.volume.lt(0) | (frame.volume.eq(0) & ~halted)
        if stock:
            invalid |= invalid_halt_rows(frame)
        invalid |= frame["high"] < frame[["open", "close", "low"]].max(axis=1)
        invalid |= frame["low"] > frame[["open", "close", "high"]].min(axis=1)
        if stock:
            unbounded = published_event_mask(frame) if validated_shared else pd.Series(False, index=frame.index)
            valid_limits = (
                np.isfinite(frame.up_limit)
                & np.isfinite(frame.down_limit)
                & frame.down_limit.gt(0)
                & frame.up_limit.gt(frame.down_limit)
            )
            invalid |= ~valid_limits & ~unbounded
        duplicate = frame.loc[frame["date"].duplicated(keep=False), "date"]
        result = {
            "code": code,
            "expected": len(sessions),
            "actual": len(actual.unique()),
            "missing_dates": dates(missing),
            "extra_dates": dates(extra),
            "duplicate_dates": dates(duplicate.unique()),
            "invalid_dates": dates(frame.loc[invalid, "date"]),
        }
        if stock:
            valid_pe = np.isfinite(frame["pe_ttm"]) & (frame["pe_ttm"] > 0)
            result["missing_valuation_dates"] = dates(frame.loc[~valid_pe, "date"])
            result["suspended_dates"] = dates(frame.loc[halted, "date"])
        result["complete"] = not any(
            result[key] for key in ["missing_dates", "extra_dates", "duplicate_dates", "invalid_dates"]
        )
        return result

    anomalies = published_adjustment_discontinuities(market) if validated_shared else adjustment_discontinuities(market)
    stocks = [assess(market.loc[market["instrument"] == instrument(code)], code, True) for code in codes]
    for row in stocks:
        rejected = [item["date"] for item in anomalies if item["instrument"] == instrument(row["code"])]
        row["invalid_dates"] = sorted(set(row["invalid_dates"] + rejected))
        row["complete"] = row["complete"] and not rejected
    index = assess(benchmark, benchmark_code, False)
    return {
        "status": "complete"
        if all(row["complete"] for row in [*stocks, index]) and len(sessions) > 0
        else "incomplete",
        "expected_sessions": len(sessions),
        "stock_count": len(codes),
        "stocks": stocks,
        "benchmark": index,
        "adjustment_anomalies": anomalies,
        "note": "缺失交易日不直接推断为停牌；仅独立两种复权口径均确认的整日停牌可按此前已知收盘价估值并禁止买卖，缺失估值仅向前沿用且记录时点；其他缺失或复权异常不自动补值、缩放或剔除股票",
    }
