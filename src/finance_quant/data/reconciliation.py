"""Independent BaoStock checks and conservative whole-series fallback.

Absolute adjustment factors from different providers are never compared or
spliced. BaoStock's official back-adjusted bars supply a coherent relative
factor series, after raw quotes and internal adjustment identities are checked.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from finance_quant.data.providers.baostock import BaoStockBundle, BaoStockProviderError
from finance_quant.data.quality import adjustment_discontinuities
from finance_quant.data.reviewed_events import verified_event_moves
from finance_quant.data.suspensions import LeadingHaltError, carry_halt_rows, confirmed_halts, invalid_halt_rows
from finance_quant.data.symbols import to_baostock, to_qlib

POLICY = "baostock-crosscheck-v1"
PRICE_TOLERANCE = 0.011
RETURN_TOLERANCE = 0.002
BAO_IDENTITY_TOLERANCE = 0.0002
PRICE_FIELDS = ("open", "high", "low", "close")
RAW_FIELDS = (*PRICE_FIELDS, "pre_close", "volume", "amount", "change")


class CrosscheckError(ValueError):
    pass


def normalize_baostock(bundle: BaoStockBundle, code: str, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    halted_dates = confirmed_halts(bundle, code, sessions)
    required = {
        "date",
        "code",
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "adjustflag",
        "pctChg",
        "tradestatus",
        "isST",
    }
    expected_code = to_baostock(code)
    normalized = []
    for original, flag in [(bundle.raw, "3"), (bundle.adjusted, bundle.metadata.get("adjusted_flag", "1"))]:
        if original.empty or not required.issubset(original.columns):
            raise CrosscheckError("BaoStock response is incomplete")
        frame = original.copy()
        if not frame["code"].eq(expected_code).all() or not frame["adjustflag"].astype(str).eq(flag).all():
            raise CrosscheckError("BaoStock instrument or adjustment flag mismatch")
        frame["date"] = pd.to_datetime(frame["date"], format="%Y-%m-%d", errors="raise")
        frame = frame.sort_values("date").set_index("date")
        if frame.index.has_duplicates or not frame.index.equals(sessions):
            raise CrosscheckError("BaoStock trading dates differ from required exchange sessions")
        if not frame["isST"].astype(str).isin(["0", "1"]).all():
            raise CrosscheckError("BaoStock includes unsupported status records")
        frame = frame.loc[~frame.index.isin(halted_dates)]
        if frame.empty:
            raise CrosscheckError("No observed prices in requested interval")
        frame = frame.rename(columns={"preclose": "pre_close", "pctChg": "change"})
        for field in RAW_FIELDS:
            frame[field] = pd.to_numeric(frame[field], errors="coerce")
        positive = [*PRICE_FIELDS, "pre_close", "volume", "amount"]
        if not np.isfinite(frame[list(RAW_FIELDS)].to_numpy(dtype=float)).all() or (frame[positive] <= 0).any().any():
            raise CrosscheckError("BaoStock has nonfinite or nonpositive quote values")
        if (
            (frame.high < frame[["open", "close", "low"]].max(axis=1))
            | (frame.low > frame[["open", "close", "high"]].min(axis=1))
        ).any():
            raise CrosscheckError("BaoStock OHLC geometry is inconsistent")
        frame["change"] /= 100
        normalized.append(frame)
    raw, adjusted = normalized
    factor = adjusted.close / raw.close
    # Every OHLC field must imply the same factor, rather than trusting close alone.
    for field in PRICE_FIELDS:
        implied = adjusted[field] / raw[field]
        if not np.isclose(implied, factor, rtol=2e-6, atol=1e-8).all():
            raise CrosscheckError("BaoStock adjusted OHLC do not share one factor")
    if not np.isfinite(factor).all() or (factor <= 0).any():
        raise CrosscheckError("BaoStock adjustment factors are invalid")
    if not np.allclose(adjusted["change"], raw["change"], rtol=0, atol=1e-8):
        raise CrosscheckError("BaoStock raw and adjusted percentage changes disagree")
    # These identities also detect a broken adjusted series in the secondary source.
    computed = adjusted.close.pct_change(fill_method=None)
    if (computed.sub(raw.change).abs().iloc[1:] > BAO_IDENTITY_TOLERANCE).any():
        raise CrosscheckError("BaoStock adjusted returns disagree with its reported changes")
    if ((raw.close / raw.pre_close - 1).sub(raw.change).abs() > BAO_IDENTITY_TOLERANCE).any():
        raise CrosscheckError("BaoStock raw prices disagree with its reported changes")
    raw = raw[list(RAW_FIELDS)].copy()
    raw["adj_factor"] = factor / factor.iloc[0]
    raw = raw.reindex(sessions)
    raw = carry_halt_rows(raw, halted_dates)
    raw["instrument"] = to_qlib(code)
    raw.index.name = "date"
    return raw.reset_index()


def _primary_issues(frame: pd.DataFrame, sessions: pd.DatetimeIndex, reviewed_dates=()) -> list[str]:
    issues = []
    if frame["date"].duplicated().any() or not pd.DatetimeIndex(frame.date).equals(sessions):
        issues.append("missing_or_duplicate_sessions")
    required = [*RAW_FIELDS, "adj_factor"]
    values = frame[required].to_numpy(dtype=float)
    halted = frame.get("is_suspended", pd.Series(False, index=frame.index))
    bad_activity = (frame[["volume", "amount"]] < 0).any(axis=1) | (
        (frame[["volume", "amount"]] == 0).any(axis=1) & ~halted
    )
    if (
        not np.isfinite(values).all()
        or (frame[[*PRICE_FIELDS, "pre_close", "adj_factor"]] <= 0).any().any()
        or bad_activity.any()
        or invalid_halt_rows(frame).any()
    ):
        issues.append("missing_or_invalid_prices_or_adjustment")
    if (
        (frame.high < frame[["open", "close", "low"]].max(axis=1))
        | (frame.low > frame[["open", "close", "high"]].min(axis=1))
    ).any():
        issues.append("invalid_ohlc_geometry")
    if any(audit["date"] not in reviewed_dates for audit in adjustment_discontinuities(frame)):
        issues.append("adjustment_discontinuity")
    return issues


def reconcile_market(
    market: pd.DataFrame, sessions, codes, mode: str, provider, *, primary_failures=(), reviewed_events=()
) -> tuple[pd.DataFrame, dict]:
    if mode not in {"verify", "fallback"}:
        raise ValueError("Reconciliation mode must be verify or fallback")
    sessions = pd.DatetimeIndex(sessions)
    if sessions.empty or sessions.has_duplicates or not sessions.is_monotonic_increasing:
        raise ValueError("Reconciliation needs an ordered unique exchange calendar")
    output, stocks = [], []
    for code in codes:
        instrument = to_qlib(code)
        primary = market.loc[market.instrument == instrument].sort_values("date").copy()
        record = {
            "code": code,
            "status": "conflict",
            "primary_issues": [],
            "raw_price_differences": [],
            "return_differences": [],
            "corporate_action_differences": [],
            "secondary": None,
            "price_source": "tushare",
            "adjustment_source": "tushare",
        }
        try:
            if primary.empty or primary.date.duplicated().any():
                raise CrosscheckError("Primary merge must provide one row per instrument/session")
            bundle = provider.fetch(code, sessions[0].strftime("%Y%m%d"), sessions[-1].strftime("%Y%m%d"))
            record["secondary"] = bundle.metadata
            secondary = normalize_baostock(bundle, code, sessions)
            halted_dates = pd.DatetimeIndex(secondary.loc[secondary.is_suspended, "date"])
            record["suspended_dates"] = halted_dates.strftime("%Y-%m-%d").tolist()
            one = carry_halt_rows(primary.set_index("date").reindex(sessions), halted_dates)
            one.index.name = "date"
            two = secondary.set_index("date")
            raw_bad = pd.Series(False, index=sessions)
            for field in PRICE_FIELDS:
                present = np.isfinite(one[field]) & (one[field] > 0) & ~one.is_suspended
                # Invalid primary OHLC may be replaced; two plausible disagreeing quotes cannot.
                raw_bad |= present & ((one[field] - two[field]).abs() > PRICE_TOLERANCE)
            record["raw_price_differences"] = [d.strftime("%Y-%m-%d") for d in sessions[raw_bad]]
            if raw_bad.any():
                record["reason"] = "raw_price_conflict"
                raise CrosscheckError("Independent raw prices disagree; no source can be selected automatically")
            events = verified_event_moves(one, two, code, reviewed_events)
            record["verified_event_moves"] = events
            issues = _primary_issues(one.reset_index(), sessions, {event["date"] for event in events})
            record["primary_issues"] = issues
            primary_return = (one.close * one.adj_factor).pct_change(fill_method=None)
            secondary_return = (two.close * two.adj_factor).pct_change(fill_method=None)
            delta = (primary_return - secondary_return).abs()
            mismatch = delta > RETURN_TOLERANCE
            # Both algorithms may differ on a genuine action date. Report it
            # separately only when BOTH coherent factor series change that day.
            action = (one.adj_factor.pct_change(fill_method=None).abs() > 1e-5) & (
                two.adj_factor.pct_change(fill_method=None).abs() > 1e-5
            )
            for date in sessions[mismatch]:
                detail = {
                    "date": date.strftime("%Y-%m-%d"),
                    "primary_return": float(primary_return.loc[date])
                    if np.isfinite(primary_return.loc[date])
                    else None,
                    "secondary_return": float(secondary_return.loc[date]),
                    "absolute_difference": float(delta.loc[date]) if np.isfinite(delta.loc[date]) else None,
                }
                record[
                    "corporate_action_differences" if action.loc[date] and not issues else "return_differences"
                ].append(detail)
            if issues:
                if mode == "verify":
                    record["reason"] = "primary_failed_verify_only"
                    raise CrosscheckError("Primary validation failed; verify mode never replaces data")
                # Replace a full instrument/window, never a few absolute factor values.
                for field in [*RAW_FIELDS, "adj_factor"]:
                    one[field] = two[field]
                one["price_source"] = "baostock"
                one["adjustment_source"] = "baostock"
                record.update(
                    status="fallback",
                    price_source="baostock",
                    adjustment_source="baostock",
                    replacement="entire instrument interval: OHLC, volume, amount, previous close, change and relative adjustment",
                )
            elif record["return_differences"]:
                record["reason"] = "plausible_adjusted_return_conflict"
                raise CrosscheckError("Plausible primary and secondary returns disagree; manual review required")
            else:
                one["price_source"], one["adjustment_source"] = "tushare", "tushare"
                record["status"] = "verified"
            one["instrument"] = instrument
            one["valuation_source"], one["limit_source"] = "tushare", "tushare"
            one.index.name = "date"
            output.append(one.reset_index())
        except LeadingHaltError:
            # Not a source conflict: the caller widens the range to include the pre-halt close.
            raise
        except (BaoStockProviderError, CrosscheckError, ValueError, KeyError, TypeError) as error:
            # Keep structured evidence; never persist arbitrary provider exception text.
            record["status"] = "conflict"
            record.setdefault(
                "reason",
                "secondary_connection_failed"
                if isinstance(error, BaoStockProviderError) and error.retryable
                else "secondary_unavailable_invalid_or_conflicting",
            )
        stocks.append(record)
    conflicts = [stock["code"] for stock in stocks if stock["status"] == "conflict"]
    fallback = [stock["code"] for stock in stocks if stock["status"] == "fallback"]
    report = {
        "policy": POLICY,
        "mode": mode,
        "status": "conflict" if conflicts else "fallback" if fallback else "verified",
        "scope": "unadjusted OHLC and adjusted close returns; valuations and volume are not independently certified",
        "price_absolute_tolerance_cny": PRICE_TOLERANCE,
        "return_absolute_tolerance": RETURN_TOLERANCE,
        "secondary_identity_tolerance": BAO_IDENTITY_TOLERANCE,
        "stocks": stocks,
        "fallback_codes": fallback,
        "conflict_codes": conflicts,
        "primary_failures": list(primary_failures),
        "notes": [
            "比较未复权OHLC和逐日复权收益，不比较两家复权因子的绝对值",
            "自动回退整只股票的区间行情与复权序列；估值和涨跌停仍来自Tushare",
            "BaoStock使用涨跌幅复权法，不等同于实际现金分红账本；两源核验不代表交易所认证",
            "两源原始价格冲突、第二源缺失或异常时拒绝发布，不按来源投票填充",
        ],
    }
    # On conflict return the original untouched candidate, plus a failed report.
    return (
        market.copy()
        if conflicts
        else pd.concat(output, ignore_index=True).sort_values(["instrument", "date"]).reset_index(drop=True)
    ), report


def validate_published_sources(market: pd.DataFrame, manifest: dict) -> None:
    """A checksum-valid joint snapshot must also carry consistent source evidence."""
    report = manifest.get("cross_validation", {})
    if (
        manifest.get("source") != "tushare_baostock"
        or report.get("policy") != POLICY
        or report.get("mode") not in {"verify", "fallback"}
        or report.get("status") not in {"verified", "fallback"}
        or report.get("conflict_codes")
        or manifest.get("coverage", {}).get("status") != "complete"
    ):
        raise ValueError("Joint snapshot has no successful source validation")
    required = {"price_source", "adjustment_source", "valuation_source", "limit_source"}
    stocks = report.get("stocks", [])
    if (
        not required.issubset(market.columns)
        or len(stocks) != len(manifest["codes"])
        or {s.get("code") for s in stocks} != set(manifest["codes"])
    ):
        raise ValueError("Joint snapshot source coverage is incomplete")
    fallback = []
    for stock in stocks:
        code = stock["code"]
        section = market.loc[market.instrument == to_qlib(code)]
        if stock.get("status") not in {"verified", "fallback"} or section.empty:
            raise ValueError("Joint snapshot has unresolved source conflicts")
        halted = section.get("is_suspended", pd.Series(False, index=section.index))
        if invalid_halt_rows(section).any() or set(section.loc[halted, "date"].dt.strftime("%Y-%m-%d")) != set(
            stock.get("suspended_dates", [])
        ):
            raise ValueError("Suspension rows differ from independent trading-status evidence")
        source = "baostock" if stock["status"] == "fallback" else "tushare"
        if (
            stock.get("price_source") != source
            or stock.get("adjustment_source") != source
            or not section.price_source.eq(source).all()
            or not section.adjustment_source.eq(source).all()
            or not section.valuation_source.eq("tushare").all()
            or not section.limit_source.eq("tushare").all()
        ):
            raise ValueError("Joint snapshot row sources differ from its validation report")
        evidence = stock.get("secondary") or {}
        files = evidence.get("files", {})
        if evidence.get("source") != "baostock" or set(files) != {"raw.parquet", "adjusted.parquet"}:
            raise ValueError("Joint snapshot has no independent source evidence")
        if stock["status"] == "fallback":
            fallback.append(code)
    if set(fallback) != set(report.get("fallback_codes", [])) or report["status"] != (
        "fallback" if fallback else "verified"
    ):
        raise ValueError("Joint snapshot fallback summary is inconsistent")
