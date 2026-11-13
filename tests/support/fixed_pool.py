"""Test fixture: build fixed-pool market snapshots from fake providers.

This is the former fixed-pool download pipeline. Production acquires market data through the
shared incremental download (``application.market_download``); tests use this builder to create
``market-v2``/``market-v3`` snapshots that exercise the real fetch, repair, reconciliation and
coverage code paths in ``finance_quant.data``.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

import numpy as np
import pandas as pd
from filelock import FileLock, Timeout

from finance_quant.checksums import file_sha256, files_sha256, sha256_text
from finance_quant.configuration import DataConfig
from finance_quant.data import managed
from finance_quant.data.catalog import Catalog
from finance_quant.data.managed import (
    DatasetDownloadError,
    ManagedFetcher,
    atomic_json,
    coverage_report,
    instrument,
    month_ranges,
    repair_missing_dates,
    year_ranges,
)
from finance_quant.data.providers import TushareProvider, TushareSettings
from finance_quant.data.providers.baostock import BaoStockProvider, BaoStockProviderError
from finance_quant.data.reconciliation import normalize_baostock, reconcile_market
from finance_quant.data.suspensions import MemoizedSecondary
from finance_quant.data.validation_config import CrosscheckConfig

DATA_PACKAGE = Path(managed.__file__).parent


def abort_failed(fetch: ManagedFetcher, *, final: bool = False):
    recoverable = {"request_failed", "network_failed", "missing_sessions", "invalid_response"}
    failures = [
        error for error in fetch.errors if not (error["api"] in fetch.fallback_apis and error["error"] in recoverable)
    ]
    if not failures:
        return
    transient = {"request_failed", "network_failed", "missing_sessions"}
    if not final and all(error["error"] in transient for error in failures):
        return
    status = (
        "partial" if any(error["error"] in {"missing_sessions", "extra_sessions"} for error in failures) else "failed"
    )
    report = {"status": status, "reason": "request_partitions_failed", "failures": failures}
    fetch.catalog.finish(fetch.job_id, status, report)
    raise DatasetDownloadError(
        f"Download {fetch.job_id} incomplete; run status to inspect failed partitions and missing dates, then retry"
    )


def publish_snapshot(
    root: Path,
    config: DataConfig,
    market: pd.DataFrame,
    benchmark: pd.DataFrame,
    calendar: pd.DataFrame,
    coverage: dict,
    fetch: ManagedFetcher,
) -> dict:
    directory = root / "snapshots"
    directory.mkdir(exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=directory))
    try:
        for name, frame in [("market", market), ("benchmark", benchmark), ("calendar", calendar)]:
            frame.to_parquet(staging / f"{name}.parquet", index=False)
        content = {
            name: file_sha256(staging / name) for name in ["market.parquet", "benchmark.parquet", "calendar.parquet"]
        }
        requests = sorted(
            fetch.requests, key=lambda item: json.dumps({"api": item["api"], "params": item["params"]}, sort_keys=True)
        )
        manifest = {
            "schema_version": "market-v2",
            "source": "third_party_tushare_compatible",
            "provider_id": fetch.provider_id,
            "is_synthetic": False,
            "start": config.start,
            "end": config.end,
            "codes": list(config.codes),
            "cache_generation": fetch.generation,
            "benchmark": config.benchmark,
            "universe_type": "fixed_user_selection",
            "trading_days": coverage["expected_sessions"],
            "rows": len(market),
            "files": content,
            "requests": requests,
            "coverage": coverage,
            "units": {"volume": "shares", "amount": "CNY", "total_mv": "CNY", "change": "fraction"},
            "notes": [
                "用户固定股票池，不是历史指数成分；可能存在选股与幸存者偏差",
                "名称和行业来自当前资料，仅展示",
                "第三方历史数据与估值发布时间/修订未经独立核实；不使用财报因子",
            ],
        }
        if config.validation.mode != "off":
            manifest.update(
                schema_version="market-v3", source="tushare_baostock", cross_validation=coverage["cross_validation"]
            )
            sources = [
                DATA_PACKAGE / "managed.py",
                DATA_PACKAGE / "reconciliation.py",
                DATA_PACKAGE / "suspensions.py",
                DATA_PACKAGE / "quality.py",
                DATA_PACKAGE / "providers/baostock.py",
                DATA_PACKAGE / "providers/baostock_worker.py",
                DATA_PACKAGE / "providers/tushare.py",
            ]
            manifest["validation_code_sha256"] = files_sha256(sources)
            manifest["notes"] += ["联合核验未复权OHLC与复权收益，保留逐股来源；不是对估值或财报的独立认证"]
            if market.get("is_suspended", pd.Series(False, index=market.index)).any():
                manifest["notes"] += [
                    "已确认整日停牌采用此前已知收盘价估值，成交量为零且禁止买卖；缺失PE仅沿用此前可得值，valuation_asof记录该值的行情日期"
                ]
        version = sha256_text(json.dumps(manifest, sort_keys=True, allow_nan=False))[:16]
        prefix = "joint" if config.validation.mode != "off" else "tushare"
        manifest.update(id=f"{prefix}-{version}", snapshot_path=f"snapshots/{version}")
        atomic_json(staging / "manifest.json", manifest)
        target = directory / version
        if target.exists():
            if json.loads((target / "manifest.json").read_text(encoding="utf-8")) != manifest or any(
                file_sha256(target / name) != digest for name, digest in content.items()
            ):
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


def download_dataset(
    config: DataConfig,
    *,
    provider=None,
    refresh: bool = False,
    secondary_provider=None,
) -> dict:
    root = config.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(root / ".download.lock", timeout=0):
            return _download_locked(config, provider, refresh, secondary_provider)
    except Timeout:
        raise DatasetDownloadError("Another download is running in this data directory") from None


def _download_locked(config: DataConfig, provider, refresh: bool, secondary_provider=None) -> dict:
    root = config.root.resolve()
    provider = provider or TushareProvider(TushareSettings.from_env())
    catalog = Catalog(root)
    job_id = f"download-{uuid.uuid4().hex[:12]}"
    specification = {
        "start": config.start,
        "end": config.end,
        "codes": list(config.codes),
        "benchmark": config.benchmark,
        "refresh": refresh,
        "partition": "year",
    }
    if config.validation.mode != "off":
        specification["validation"] = config.validation.as_dict()
    calendar_end = (pd.Timestamp(config.end) + pd.Timedelta(days=20)).strftime("%Y%m%d")
    calendar_ranges = month_ranges(config.start, calendar_end)
    ranges = year_ranges(config.start, config.end)
    catalog.begin_job(
        job_id, specification, len(calendar_ranges) + len(config.codes) * (1 + 4 * len(ranges)) + len(ranges)
    )
    fetch = None
    try:
        fetch = ManagedFetcher(provider, root, catalog, job_id, refresh, specification)
        calendar_parts = fetch.get_many(
            [("trade_cal", {"exchange": "SSE", "start_date": start, "end_date": end}) for start, end in calendar_ranges]
        )
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
        ranges = [
            (start, end)
            for start, end in ranges
            if expected.to_series().between(pd.Timestamp(start), pd.Timestamp(end)).any()
        ]
        catalog.set_total(job_id, len(calendar_ranges) + len(config.codes) * (1 + 4 * len(ranges)) + len(ranges))
        frames, benchmark_parts = [], []
        if config.validation.mode != "off":
            secondary_provider = MemoizedSecondary(
                secondary_provider or BaoStockProvider(root / "secondary", namespace=fetch.generation)
            )
        for code in config.codes:
            halted_dates = pd.DatetimeIndex([])
            name = fetch.get("stock_basic", ts_code=code)
            abort_failed(fetch)
            if name is not None:
                metadata = name.iloc[0]
                for field, outside in [
                    ("list_date", lambda date: expected[expected < date]),
                    ("delist_date", lambda date: expected[expected > date]),
                ]:
                    value = metadata.get(field)
                    if pd.notna(value) and str(value).strip():
                        boundary = pd.to_datetime(str(value), format="%Y%m%d", errors="coerce")
                        affected = outside(boundary) if pd.notna(boundary) else pd.DatetimeIndex([])
                        if len(affected):
                            fetch.errors.append(
                                {
                                    "api": "stock_basic",
                                    "params": {"ts_code": code},
                                    "error": "outside_listing_period",
                                    "missing_dates": affected.strftime("%Y-%m-%d").tolist(),
                                }
                            )
                            abort_failed(fetch)
            parts = {api: [] for api in ["daily", "adj_factor", "daily_basic", "stk_limit"]}
            for start, end in ranges:
                monthly = fetch.get_many(
                    [(api, {"ts_code": code, "start_date": start, "end_date": end}) for api in parts]
                )
                abort_failed(fetch)
                period_sessions = expected[(expected >= pd.Timestamp(start)) & (expected <= pd.Timestamp(end))]
                missing_bars = any(
                    frame is not None and len(period_sessions.difference(pd.DatetimeIndex(frame.date)))
                    for api, frame in zip(parts, monthly, strict=True)
                )
                if config.validation.mode != "off" and missing_bars:
                    try:
                        bundle = secondary_provider.fetch(
                            code, expected[0].strftime("%Y%m%d"), expected[-1].strftime("%Y%m%d")
                        )
                        checked_secondary = normalize_baostock(bundle, code, expected)
                    except (ValueError, RuntimeError) as error:
                        category = (
                            "secondary_connection_failed"
                            if isinstance(error, BaoStockProviderError) and error.retryable
                            else "secondary_unavailable"
                            if isinstance(error, BaoStockProviderError)
                            else "secondary_invalid"
                        )
                        fetch.errors.append(
                            {"api": "secondary_history", "params": {"ts_code": code}, "error": category}
                        )
                        catalog.finish(job_id, "failed", {"status": "failed", "failures": fetch.errors})
                        raise DatasetDownloadError(f"Independent suspension evidence unavailable for {code}") from None
                    halted_dates = pd.DatetimeIndex(checked_secondary.loc[checked_secondary.is_suspended, "date"])
                for api, frame in zip(parts, monthly, strict=True):
                    if frame is not None:
                        checked = []
                        for month_start, month_end in month_ranges(start, end):
                            month_sessions = expected[
                                (expected >= pd.Timestamp(month_start)) & (expected <= pd.Timestamp(month_end))
                            ].difference(halted_dates)
                            section = frame.loc[
                                frame["date"].between(pd.Timestamp(month_start), pd.Timestamp(month_end))
                            ]
                            section = section.loc[~section.date.isin(halted_dates)]
                            checked.append(
                                repair_missing_dates(fetch, api, code, month_start, month_end, section, month_sessions)
                            )
                            abort_failed(fetch)
                        frame = pd.concat(checked, ignore_index=True)
                        parts[api].append(frame)
                abort_failed(fetch)
            if name is None or any(not rows for api, rows in parts.items() if api not in fetch.fallback_apis):
                continue
            if config.validation.mode != "off" or fetch.fallback_apis:
                joined = pd.DataFrame({"instrument": instrument(code), "date": expected})
                for api, rows in parts.items():
                    if rows:
                        joined = joined.merge(
                            pd.concat(rows, ignore_index=True),
                            on=["instrument", "date"],
                            how="left",
                            validate="one_to_one",
                        )
                for column in ["open", "high", "low", "close", "pre_close", "volume", "amount", "change", "adj_factor"]:
                    if column not in joined:
                        joined[column] = np.nan
            else:
                joined = pd.concat(parts["daily"], ignore_index=True)
                for api in ["adj_factor", "daily_basic", "stk_limit"]:
                    joined = joined.merge(
                        pd.concat(parts[api], ignore_index=True),
                        on=["instrument", "date"],
                        how="left",
                        validate="one_to_one",
                    )
            joined["name"], joined["industry"] = str(name.iloc[0]["name"]), str(name.iloc[0].get("industry", "未提供"))
            frames.append(joined)
        for start, end in ranges:
            frame = fetch.get("index_daily", ts_code=config.benchmark, start_date=start, end_date=end)
            if frame is not None:
                checked = []
                for month_start, month_end in month_ranges(start, end):
                    month_sessions = expected[
                        (expected >= pd.Timestamp(month_start)) & (expected <= pd.Timestamp(month_end))
                    ]
                    section = frame.loc[frame["date"].between(pd.Timestamp(month_start), pd.Timestamp(month_end))]
                    checked.append(
                        repair_missing_dates(
                            fetch, "index_daily", config.benchmark, month_start, month_end, section, month_sessions
                        )
                    )
                    abort_failed(fetch)
                frame = pd.concat(checked, ignore_index=True)
                benchmark_parts.append(frame)
            abort_failed(fetch)
        abort_failed(fetch, final=True)
        market = pd.concat(frames, ignore_index=True).sort_values(["instrument", "date"]).reset_index(drop=True)
        benchmark = pd.concat(benchmark_parts, ignore_index=True).sort_values("date").reset_index(drop=True)
        cross_validation = None
        if config.validation.mode != "off":
            secondary_provider = secondary_provider or BaoStockProvider(root / "secondary", namespace=fetch.generation)
            market, cross_validation = reconcile_market(
                market,
                expected,
                config.codes,
                config.validation.mode,
                secondary_provider,
                primary_failures=fetch.errors,
            )
        coverage = coverage_report(market, benchmark, expected, config.codes, config.benchmark)
        if cross_validation is not None:
            coverage["cross_validation"] = cross_validation
            if cross_validation["status"] == "conflict":
                coverage["status"] = "incomplete"
            coverage["note"] += (
                "；联合核验详情见cross_validation；回退时整只股票区间的价格与复权使用BaoStock，估值与限制价保留Tushare"
            )
        atomic_json(root / "reports" / f"{job_id}.json", coverage)
        if coverage["status"] != "complete":
            catalog.finish(job_id, "partial", coverage)
            raise DatasetDownloadError(
                f"Download {job_id} failed market quality checks (coverage, prices or adjustment); previous published snapshot retained. Run status for details"
            )
        manifest = publish_snapshot(root, config, market, benchmark, calendar, coverage, fetch)
        catalog.finish(job_id, "succeeded", coverage, manifest["id"])
        if refresh:
            (root / "pending-refresh.json").unlink(missing_ok=True)
        return {"job_id": job_id, "manifest": manifest}
    except BaseException as exc:
        current = catalog.job_status(job_id)
        if current == "running":
            interrupted = isinstance(exc, (KeyboardInterrupt, SystemExit))
            catalog.finish(
                job_id,
                "interrupted" if interrupted else "failed",
                {
                    "status": "interrupted" if interrupted else "failed",
                    "reason": "download_interrupted" if interrupted else "validation_or_publication_failed",
                    "failures": fetch.errors if fetch else [],
                },
            )
        raise
    finally:
        catalog.close()


def publish_fixture_snapshot(
    data_root,
    codes,
    start,
    end,
    *,
    provider,
    secondary_provider=None,
    validation="off",
    name="fixture",
    benchmark="000300.SH",
):
    """Publish a selectable snapshot where the shared download exports them."""

    config = DataConfig(
        Path(data_root) / "research" / "downloads" / name,
        start,
        end,
        tuple(codes),
        benchmark,
        CrosscheckConfig(validation),
    )
    return download_dataset(config, provider=provider, secondary_provider=secondary_provider)["manifest"]
