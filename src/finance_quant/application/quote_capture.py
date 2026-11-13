"""Resumable historical index acquisition, independent of fixed-pool research."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from finance_quant.application.quote_quality import _quality as _quality
from finance_quant.application.quote_quality import _secondary_evidence as _secondary_evidence
from finance_quant.application.quote_quality import primary_events, primary_valid
from finance_quant.checksums import file_sha256, files_sha256, sha256_text
from finance_quant.data.catalog import utc_now
from finance_quant.data.index_universe import membership_mask
from finance_quant.data.managed import (
    atomic_json,
    instrument,
    month_ranges,
    repair_missing_dates,
)
from finance_quant.data.market import SnapshotFetcher
from finance_quant.data.network_budget import DownloadPaused
from finance_quant.data.official_suspensions import AmbiguousSuspensionError, full_day_halts
from finance_quant.data.providers import TushareProvider
from finance_quant.data.providers.tushare import TushareProviderError
from finance_quant.data.quality import adjustment_discontinuities
from finance_quant.data.reconciliation import (
    reconcile_market,
)
from finance_quant.data.reviewed_events import REVIEWED_EVENTS
from finance_quant.data.suspensions import carry_halt_rows, confirmed_halts

WAREHOUSE_POLICY = "official-primary-sample-sha256-mod10-v1"


def sample_code(code):
    # Stable across ordering, batches and resumes. Approximately 10% of codes.
    return int(sha256_text(code), 16) % 10 == 0


def _save_frame(path, frame):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".pending.parquet")
    frame.to_parquet(temporary, index=False)
    temporary.replace(path)


def validation_signature():
    base = Path(__file__).resolve().parents[1]
    files = [
        Path(__file__),
        Path(__file__).with_name("quote_quality.py"),
        *[
            base / "data" / name
            for name in [
                "index_universe.py",
                "quality.py",
                "reconciliation.py",
                "suspensions.py",
                "reviewed_events.py",
                "official_suspensions.py",
                "providers/tushare.py",
                "providers/baostock.py",
                "providers/baostock_worker.py",
            ]
        ],
    ]
    return files_sha256(files)


def asset_key(task, provider_id, membership_sha, validation_sha):
    return sha256_text(
        json.dumps(
            {
                "code": task["code"],
                "start": task["start"],
                "end": task["end"],
                "provider_id": provider_id,
                "membership_sha256": membership_sha,
                "validation_sha256": validation_sha,
                "policy": "index-asset-v1",
            },
            sort_keys=True,
        )
    )[:20]


def _reuse_asset(root, task, fetch, key):
    code = task["code"]
    directory = root / "assets" / key
    receipt = directory / "receipt.json"
    if receipt.exists():
        record = json.loads(receipt.read_text(encoding="utf-8"))
        if file_sha256(directory / "market.parquet") != record["sha256"]:
            raise ValueError("股票缓存校验失败")
        capture_path = root / "captures" / f"{key}.json"
        if not capture_path.exists():
            captured = []
            for api in ["daily", "adj_factor", "daily_basic", "stk_limit"]:
                params = {"ts_code": code, "start_date": task["start"], "end_date": task["end"]}
                specification, partition_key = fetch.key(api, params)
                previous, cache_root = fetch.cached_partition(partition_key)
                if previous and previous["status"] == "succeeded":
                    path = (cache_root / previous["raw_path"]).resolve()
                    if not path.is_relative_to(cache_root.resolve()) or file_sha256(path) != previous["sha256"]:
                        raise ValueError("原始响应缓存校验失败")
                    captured.append(
                        {**specification, **{k: previous[k] for k in ["raw_path", "sha256", "rows", "fetched_at"]}}
                    )
            atomic_json(
                capture_path,
                {
                    "code": code,
                    "key": key,
                    "provider_id": fetch.provider_id,
                    "start": task["start"],
                    "end": task["end"],
                    "responses_saved": len(captured),
                    "requests": captured,
                    "status": "verified_asset_reused",
                },
            )
        return {**record, "reused": True}


def _capture_primary(root, task, fetch, key):
    code = task["code"]
    apis = ["daily", "adj_factor", "daily_basic", "stk_limit"]
    fetch.errors = []
    parts = fetch.get_many(
        [(api, {"ts_code": code, "start_date": task["start"], "end_date": task["end"]}) for api in apis]
    )
    captures = [
        r
        for r in fetch.requests
        if r["api"] in apis
        and r["params"].get("ts_code") == code
        and r["params"].get("start_date") == task["start"]
        and r["params"].get("end_date") == task["end"]
    ]
    atomic_json(
        root / "captures" / f"{key}.json",
        {
            "code": code,
            "key": key,
            "provider_id": fetch.provider_id,
            "start": task["start"],
            "end": task["end"],
            "responses_saved": len({r["api"] for r in captures}),
            "requests": captures,
            "status": "raw_capture_only; not independently validated",
        },
    )
    if any(e["error"] in {"access_denied", "rate_limit"} for e in fetch.errors):
        raise DownloadPaused("主源权限或频率限制，已保留断点，请检查后续传")
    if parts[2] is None or parts[3] is None:
        raise ValueError("估值或涨跌停请求失败，不能用第二源静默替换")
    return parts


def _assemble_primary_frame(task, expected, parts):
    code = task["code"]
    frame = pd.DataFrame({"instrument": instrument(code), "date": expected})
    for part in parts:
        if part is not None:
            frame = frame.merge(part, on=["instrument", "date"], how="left", validate="one_to_one")
    for col in ["open", "high", "low", "close", "pre_close", "volume", "amount", "change", "adj_factor"]:
        if col not in frame:
            frame[col] = np.nan
    frame["name"], frame["industry"] = task["name"], task["industry"]
    frame["is_suspended"] = False
    frame["valuation_carried"] = False
    return frame


def _validate_primary_sources(frame, root, task, parts, expected, eligible, fetch, secondary):
    code = task["code"]
    events = primary_events(frame, code)
    basic_complete = pd.DatetimeIndex(parts[2].date.sort_values()).equals(expected)
    valid = primary_valid(frame, expected, eligible, events) and basic_complete
    suspension_evidence = None
    official_status_reviews = []
    if not valid and isinstance(fetch.provider, TushareProvider):
        # Query only months containing missing/inactive prices, never infer
        # suspension merely from a quote hole or a subsequent resume event.
        candidates = frame.loc[
            frame[["open", "high", "low", "close"]].isna().any(axis=1) | frame.volume.eq(0) | frame.amount.eq(0), "date"
        ]
        official = SnapshotFetcher(fetch.provider, root / "suspension-raw")
        confirmed = pd.DatetimeIndex([])
        for month in sorted(set(candidates.dt.to_period("M"))):
            a = max(month.start_time, pd.Timestamp(task["start"]))
            b = min(month.end_time.normalize(), pd.Timestamp(task["end"]))

            try:
                raw = official.get(
                    "suspend_d", ts_code=code, start_date=a.strftime("%Y%m%d"), end_date=b.strftime("%Y%m%d")
                )
            except TushareProviderError:
                continue  # The secondary may still supply independently valid status.
            try:
                confirmed = confirmed.union(full_day_halts(raw, code, a, b, frame))
            except AmbiguousSuspensionError:
                official_status_reviews.append(
                    {
                        "start": a.strftime("%Y-%m-%d"),
                        "end": b.strftime("%Y-%m-%d"),
                        "reason": "same-day suspend/resume conflict; needs independent status evidence",
                        "request": official.requests[-1],
                    }
                )
        if len(confirmed):
            frame = carry_halt_rows(frame.set_index("date"), confirmed).rename_axis("date").reset_index()
            events = primary_events(frame, code)
            basic_complete = not len(expected.difference(confirmed).difference(pd.DatetimeIndex(parts[2].date)))
            valid = primary_valid(frame, expected, eligible, events) and basic_complete
            suspension_evidence = {
                "source": "tushare_suspend_d",
                "dates": confirmed.strftime("%Y-%m-%d").tolist(),
                "requests": official.requests,
                "scope": "official full-day status; no independent price certification",
            }
    if not valid:
        # A broken secondary adjusted-price series need not invalidate its
        # independently matching raw/adjusted trading-status fields. Confirm
        # halts separately; never turn an unexplained quote hole into a halt.
        status_bundle = secondary.fetch(code, task["start"], task["end"])
        halted = confirmed_halts(status_bundle, code, expected)
        if len(halted):
            frame = carry_halt_rows(frame.set_index("date"), halted).rename_axis("date").reset_index()
            events = primary_events(frame, code)
            basic_complete = not len(expected.difference(halted).difference(pd.DatetimeIndex(parts[2].date)))
            valid = primary_valid(frame, expected, eligible, events) and basic_complete
            suspension_evidence = {
                "source": status_bundle.metadata,
                "dates": halted.strftime("%Y-%m-%d").tolist(),
                "scope": "trading status only; does not certify adjusted prices",
            }
    return frame, events, valid, suspension_evidence, official_status_reviews


def _crosscheck_secondary(frame, root, key, task, expected, fetch, secondary, parts, valid, report, events):
    code = task["code"]
    trigger = report["trigger"]
    official_status_reviews = report["official_status_reviews"]
    level = "single_source_passed"
    try:
        bundle, evidence, rejected = _secondary_evidence(root, key, code, task, expected, secondary)
    except (ValueError, RuntimeError) as error:
        if not valid:
            raise
        # Optional sampling cannot make a healthy official series depend on
        # the secondary's availability, quota, or internally broken factors.
        report.update(
            status="unavailable",
            reason=type(error).__name__,
            validation_dates=getattr(error, "validation_dates", []),
        )
    else:
        halted = pd.DatetimeIndex(evidence.loc[evidence.is_suspended, "date"])
        checked = []
        for a, b in month_ranges(task["start"], task["end"]):
            dates = expected[(expected >= pd.Timestamp(a)) & (expected <= pd.Timestamp(b))].difference(halted)
            section = parts[2].loc[
                parts[2].date.between(pd.Timestamp(a), pd.Timestamp(b)) & ~parts[2].date.isin(halted)
            ]
            checked.append(repair_missing_dates(fetch, "daily_basic", code, a, b, section, dates))
        valuations = pd.concat(checked, ignore_index=True)
        if len(expected.difference(halted).difference(pd.DatetimeIndex(valuations.date))):
            raise ValueError("估值交易日记录仍缺失")
        columns = [c for c in valuations if c not in ["instrument", "date"]]
        frame = frame.drop(columns=columns).merge(
            valuations, on=["instrument", "date"], how="left", validate="one_to_one"
        )

        class Captured:
            def fetch(self, *args):
                return bundle

        frame, report = reconcile_market(
            frame,
            expected,
            (code,),
            "fallback",
            Captured(),
            primary_failures=fetch.errors,
            reviewed_events=REVIEWED_EVENTS,
        )
        report.update(
            trigger=trigger, rejected_secondary_bases=rejected, official_status_reviews=official_status_reviews
        )
        if report["status"] == "conflict":
            atomic_json(
                root / "quarantine" / f"{key}.json",
                {"code": code, "category": "source_conflict", "cross_validation": report},
            )
            raise ValueError("两源行情、复权或停牌校验存在冲突")
        events = report["stocks"][0].get("verified_event_moves", [])
        level = "secondary_fallback_passed" if report["status"] == "fallback" else "dual_source_verified"
    return frame, report, events, level


def _publish_asset(frame, root, key, task, intervals, fetch, expected, events, report, level):
    code = task["code"]
    directory = root / "assets" / key
    receipt = directory / "receipt.json"
    if level == "single_source_passed":
        frame["price_source"] = "tushare"
        frame["adjustment_source"] = "tushare"
        frame["valuation_source"] = "tushare"
        frame["limit_source"] = "tushare"
    frame["validation_level"] = level
    eligible = (
        membership_mask(intervals, code, frame.date)
        & frame.date.ge(pd.Timestamp(json.loads((root / "plan.json").read_text(encoding="utf-8"))["start"])).to_numpy()
    )
    frame["index_member"] = eligible
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    frame["history_sessions_before"] = np.arange(len(frame))
    frame["has_required_pre_history"] = frame.history_sessions_before.ge(plan["warmup_sessions"])
    frame["has_daily_price_limit"] = (
        np.isfinite(frame.up_limit)
        & np.isfinite(frame.down_limit)
        & frame.down_limit.gt(0)
        & frame.up_limit.gt(frame.down_limit)
    )
    event_dates = {event["date"] for event in events}
    unbounded = frame.date.dt.strftime("%Y-%m-%d").isin(event_dates)
    frame.loc[unbounded, "has_daily_price_limit"] = False
    frame["reviewed_unbounded_session"] = unbounded
    quality = _quality(frame, expected, eligible, events)
    quality["internal_consistency"] = primary_valid(frame, expected, eligible, events)
    quality["complete"] = quality["complete"] and quality["internal_consistency"]
    if not quality["complete"]:
        atomic_json(
            root / "quarantine" / f"{key}.json",
            {
                "code": code,
                "category": "market_quality_requires_review",
                "quality": quality,
                "adjustment_audits": adjustment_discontinuities(frame),
                "cross_validation": report,
                "created_at": utc_now(),
            },
        )
        raise ValueError("股票数据质量或成分期执行价格约束不完整，详情已隔离保存")
    if validation_signature() != fetch.validation_sha256:
        raise DownloadPaused("校验代码已更新，已保留断点；请续传以使用新版本")
    directory.mkdir(parents=True, exist_ok=True)
    _save_frame(directory / "market.parquet", frame)
    record = {
        "schema_version": "index-asset-v1",
        "key": key,
        "code": code,
        "start": task["start"],
        "end": task["end"],
        "rows": len(frame),
        "file": str((directory / "market.parquet").relative_to(root)).replace("\\", "/"),
        "sha256": file_sha256(directory / "market.parquet"),
        "provider_id": fetch.provider_id,
        "validation_sha256": fetch.validation_sha256,
        "validation_level": level,
        "validation_policy": WAREHOUSE_POLICY,
        "cross_validation": report,
        "quality": quality,
        "created_at": utc_now(),
    }
    atomic_json(receipt, record)
    return record


def download_asset(root, task, calendar, intervals, fetch, secondary):
    """Acquire one asset through capture, validation and immutable publication stages."""
    code = task["code"]
    plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
    key = asset_key(task, fetch.provider_id, plan["reference_files"]["memberships.parquet"], fetch.validation_sha256)
    cached = _reuse_asset(root, task, fetch, key)
    if cached is not None:
        return cached
    expected = pd.DatetimeIndex(
        calendar.loc[
            calendar.is_open & calendar.date.between(pd.Timestamp(task["start"]), pd.Timestamp(task["end"])), "date"
        ]
    )
    parts = _capture_primary(root, task, fetch, key)
    frame = _assemble_primary_frame(task, expected, parts)
    eligible = membership_mask(intervals, code, frame.date) & frame.date.ge(pd.Timestamp(plan["start"])).to_numpy()
    frame, events, valid, suspension_evidence, official_status_reviews = _validate_primary_sources(
        frame,
        root,
        task,
        parts,
        expected,
        eligible,
        fetch,
        secondary,
    )
    trigger = "anomaly" if not valid else "sample" if sample_code(code) else "not_selected"
    report = {
        "policy": WAREHOUSE_POLICY,
        "status": "not_checked",
        "trigger": trigger,
        "stocks": [],
        "conflict_codes": [],
        "fallback_codes": [],
        "primary_failures": list(fetch.errors),
        "suspension_evidence": suspension_evidence,
        "official_status_reviews": official_status_reviews,
    }
    level = "single_source_passed"
    if trigger != "not_selected":
        frame, report, events, level = _crosscheck_secondary(
            frame,
            root,
            key,
            task,
            expected,
            fetch,
            secondary,
            parts,
            valid,
            report,
            events,
        )
    return _publish_asset(frame, root, key, task, intervals, fetch, expected, events, report, level)
