"""Price consistency checks and independently validated secondary-source evidence."""

import numpy as np
import pandas as pd

from finance_quant.data.catalog import utc_now
from finance_quant.data.managed import atomic_json
from finance_quant.data.quality import adjustment_discontinuities
from finance_quant.data.reconciliation import (
    BAO_IDENTITY_TOLERANCE,
    CrosscheckError,
    _primary_issues,
    normalize_baostock,
)
from finance_quant.data.reviewed_events import REVIEWED_EVENTS, verified_event_moves
from finance_quant.data.suspensions import LeadingHaltError, invalid_halt_rows
from finance_quant.data.validation_errors import MarketDataError


def primary_events(frame, code, candidates=REVIEWED_EVENTS):
    indexed = frame.set_index("date")
    return [
        {**event, "evidence_level": "single_source"}
        for event in verified_event_moves(indexed, indexed, code, candidates)
    ]


def primary_valid(frame, expected, eligible, events):
    if _primary_issues(frame, expected, {e["date"] for e in events}):
        return False
    # A plausible OHLC series also needs its own reference close/return identity.
    if ((frame.close / frame.pre_close - 1 - frame.change).abs() > 0.0002).any():
        return False
    adjusted = (frame.close * frame.adj_factor).pct_change(fill_method=None)
    if (adjusted.sub(frame.change).abs().iloc[1:] > 0.002).any():
        return False
    return _quality(frame, expected, eligible, events)["complete"]


def _quality(frame, expected, eligibility, reviewed_events=()):
    values = frame[["open", "high", "low", "close", "pre_close", "adj_factor"]].to_numpy(float)
    bad = ~np.isfinite(values).all(axis=1) | (values <= 0).any(axis=1)
    bad |= (frame.high < frame[["open", "close", "low"]].max(axis=1)) | (
        frame.low > frame[["open", "close", "high"]].min(axis=1)
    )
    bad |= ~np.isfinite(frame.volume) | frame.volume.lt(0) | (frame.volume.eq(0) & ~frame.is_suspended)
    bad |= ~np.isfinite(frame.amount) | frame.amount.lt(0) | (frame.amount.eq(0) & ~frame.is_suspended)
    bad |= ~np.isfinite(frame.change)
    bad |= invalid_halt_rows(frame)
    limit_ok = (
        np.isfinite(frame.up_limit)
        & np.isfinite(frame.down_limit)
        & frame.down_limit.gt(0)
        & frame.up_limit.gt(frame.down_limit)
    )
    # Before joining the index, IPO sessions may have no price bands. Preserve raw
    # missing bands, flag them, and never invent executable limits for research.
    dates = frame.date.dt.strftime("%Y-%m-%d")
    reviewed_dates = {event["date"] for event in reviewed_events}
    missing_live = (~limit_ok) & eligibility & ~dates.isin(reviewed_dates)
    audits = [a for a in adjustment_discontinuities(frame) if a["date"] not in reviewed_dates]
    raw_identity = (frame.close / frame.pre_close - 1 - frame.change).abs().gt(0.0002)
    adjusted_identity = (frame.close * frame.adj_factor).pct_change(fill_method=None).sub(frame.change).abs().gt(0.002)
    identity_dates = dates[raw_identity | adjusted_identity].tolist()
    return {
        "complete": pd.DatetimeIndex(frame.date).equals(expected)
        and not bad.any()
        and not missing_live.any()
        and not audits
        and not identity_dates,
        "invalid_price_dates": dates[bad].tolist(),
        "missing_limits_in_membership": dates[missing_live].tolist(),
        "unavailable_limit_dates": dates[~limit_ok].tolist(),
        "verified_event_moves": list(reviewed_events),
        "adjustment_audits": audits,
        "return_identity_dates": identity_dates,
        "missing_positive_pe": int((~(np.isfinite(frame.pe_ttm) & frame.pe_ttm.gt(0))).sum()),
    }


def _secondary_evidence(root, key, code, task, expected, secondary):
    """Try one alternate whole-interval basis after an identity or adjustment-flag failure.

    Both captures remain immutable. No values are spliced or repaired in place.
    Every alternative must pass the same OHLC, status and return identities.
    """
    rejected: list[dict] = []
    providers = [secondary]
    for provider in providers:
        bundle = provider.fetch(code, task["start"], task["end"])
        try:
            normalized = normalize_baostock(bundle, code, expected)
            return bundle, normalized, rejected
        except LeadingHaltError:
            raise  # The caller must widen history, not reject or switch the adjustment basis.
        except ValueError as error:
            detail = {
                "code": code,
                "category": "secondary_internal_validation_failed",
                "reason": str(error),
                "source_evidence": bundle.metadata,
                "dates": getattr(error, "validation_dates", []),
                "created_at": utc_now(),
            }
            identity_failure = str(error) == "BaoStock adjusted returns disagree with its reported changes"
            if identity_failure:
                raw = bundle.raw.loc[bundle.raw.tradestatus.eq("1")].sort_values("date").set_index("date")
                adjusted = (
                    bundle.adjusted.loc[bundle.adjusted.tradestatus.eq("1")].sort_values("date").set_index("date")
                )
                delta = (
                    pd.to_numeric(adjusted.close).pct_change(fill_method=None) - pd.to_numeric(raw.pctChg) / 100
                ).abs()
                detail["dates"] = delta.loc[delta > BAO_IDENTITY_TOLERANCE].index.tolist()
            rejected.append(detail)
            flag = bundle.metadata.get("adjusted_flag", "1")
            atomic_json(root / "quarantine" / f"{key}-secondary-flag{flag}.json", detail)
            basis_failure = getattr(error, "category", None) == "secondary_adjustment_basis"
            if len(providers) == 1 and (identity_failure or basis_failure) and hasattr(secondary, "alternate_basis"):
                providers.append(secondary.alternate_basis())
                continue
            atomic_json(root / "quarantine" / f"{key}.json", {**detail, "rejected_bases": rejected})
            dates = sorted({day for record in rejected for day in record["dates"]})
            if isinstance(error, CrosscheckError):
                error.validation_dates = dates
                raise
            raise MarketDataError(
                str(error),
                category=getattr(error, "category", "secondary_internal_validation_failed"),
                dates=dates,
                action="BaoStock响应未通过自身一致性检查，未用于补缺；核对官方行情后续传。",
                evidence={"quarantine_file": f"quarantine/{key}.json"},
            ) from error
