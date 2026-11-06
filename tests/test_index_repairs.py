"""Regression checks for quarantined histories, without relaxing tolerances."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from test_joint_data import Secondary, bundle, market_frame

from finance_quant.application.quote_capture import _quality, _secondary_evidence
from finance_quant.data.providers.baostock import BaoStockProvider
from finance_quant.data.reconciliation import CrosscheckError, normalize_baostock, reconcile_market
from finance_quant.data.reviewed_events import REVIEWED_EVENTS


def front_bundle(frame, code="600000.SH", scale=1.0):
    item = bundle(frame, code)
    adjusted = item.adjusted.copy()
    for field in ["open", "high", "low", "close", "preclose"]:
        adjusted[field] = (pd.to_numeric(adjusted[field]) * scale).astype(str)
    adjusted["adjustflag"] = "2"
    return replace(item, adjusted=adjusted, metadata={**item.metadata, "adjusted_flag": "2"})


def broken(item):
    adjusted = item.adjusted.copy()
    for field in ["open", "high", "low", "close", "preclose"]:
        adjusted.loc[2, field] = str(float(adjusted.loc[2, field]) * 1.2)
    return replace(item, adjusted=adjusted)


def test_alternative_cache_request_is_distinct_and_replayed(tmp_path):
    calls = []

    def worker(request):
        calls.append(request)
        item = front_bundle(market_frame()) if request.get("adjusted_flag") == "2" else bundle(market_frame())
        return {"raw": item.raw.to_dict("records"), "adjusted": item.adjusted.to_dict("records")}

    provider = BaoStockProvider(tmp_path, worker=worker)
    first = provider.fetch("600000.SH", "20250102", "20250108")
    alternate = provider.alternate_basis()
    second = alternate.fetch("600000.SH", "20250102", "20250108")
    assert first.metadata["cache_key"] != second.metadata["cache_key"]
    assert second.metadata["request"]["adjusted_flag"] == "2"
    alternate.fetch("600000.SH", "20250102", "20250108")
    assert len(calls) == 2


def test_front_relative_factors_and_returns_invariant_to_future_global_rebase():
    frame = market_frame()
    frame.loc[2:, "adj_factor"] *= 2
    for field in ["open", "high", "low", "close", "pre_close"]:
        frame.loc[2:, field] /= 2
    full = normalize_baostock(front_bundle(frame, scale=0.002), "600000.SH", pd.DatetimeIndex(frame.date))
    prefix = frame.iloc[:3]
    early = normalize_baostock(front_bundle(prefix, scale=13.0), "600000.SH", pd.DatetimeIndex(prefix.date))
    np.testing.assert_allclose(full.adj_factor[:3], early.adj_factor)
    np.testing.assert_allclose(full.adj_factor, [1, 1, 2, 2, 2])


@pytest.mark.parametrize("both_bad", [False, True])
def test_whole_interval_retry_preserves_rejected_evidence(tmp_path, both_bad):
    frame = market_frame()
    bad = broken(bundle(frame))
    alternative = front_bundle(frame)
    if both_bad:
        alternative = broken(alternative)

    class Alternate:
        def fetch(self, *args):
            return alternative

    class Original:
        def fetch(self, *args):
            return bad

        def alternate_basis(self):
            return Alternate()

    args = (
        tmp_path,
        "asset",
        "600000.SH",
        {"start": "20250102", "end": "20250108"},
        pd.DatetimeIndex(frame.date),
        Original(),
    )
    if both_bad:
        with pytest.raises(CrosscheckError) as caught:
            _secondary_evidence(*args)
        assert caught.value.validation_dates == ["2025-01-06", "2025-01-07"]
        assert (tmp_path / "quarantine/asset-secondary-flag2.json").exists()
    else:
        selected, normalized, rejected = _secondary_evidence(*args)
        assert selected.metadata["adjusted_flag"] == "2" and normalized.adj_factor.eq(1).all()
        assert rejected[0]["dates"] == ["2025-01-06", "2025-01-07"]
    assert (tmp_path / "quarantine/asset-secondary-flag1.json").exists()


def event_market(event):
    day = pd.Timestamp(event["date"])
    dates = pd.bdate_range(end=day, periods=3)
    frame = market_frame(dates, event["code"])
    frame.loc[2, ["open", "high", "low", "close"]] *= 4
    frame.loc[2, "change"] = 3.0
    return frame


@pytest.mark.parametrize("event", REVIEWED_EVENTS)
def test_known_event_exact_date_verified_without_fallback_and_usable_quality(event):
    frame = event_market(event)
    code = event["code"]
    result, report = reconcile_market(
        frame, frame.date, (code,), "fallback", Secondary({code: frame}), reviewed_events=REVIEWED_EVENTS
    )
    assert report["status"] == "verified" and result.adjustment_source.eq("tushare").all()
    events = report["stocks"][0]["verified_event_moves"]
    assert len(events) == 1 and events[0]["date"] == event["date"]
    assert result.close.iloc[-1] == 40 and result.change.iloc[-1] == 3
    assert _quality(result, pd.DatetimeIndex(frame.date), np.ones(3, dtype=bool), events)["complete"]
    assert not _quality(result, pd.DatetimeIndex(frame.date), np.ones(3, dtype=bool))["complete"]


@pytest.mark.parametrize(
    "case", ["unknown", "future_notice", "wrong_date", "raw_disagreement", "factor_error", "secondary_bad"]
)
def test_event_cannot_hide_unverified_data_or_future_information(case):
    event = dict(REVIEWED_EVENTS[0])
    frame = event_market(event)
    code = event["code"]
    source = frame.copy()
    if case == "unknown":
        event["code"] = "600000.SH"
    elif case == "future_notice":
        event["known_by"] = "2021-08-11"
    elif case == "wrong_date":
        event["date"] = "2021-08-11"
    elif case == "raw_disagreement":
        frame.loc[2, "close"] += 0.02
    elif case == "factor_error":
        frame.loc[2, "adj_factor"] *= 2
    else:
        source.loc[2, "change"] = 0
    result, report = reconcile_market(
        frame, frame.date, (code,), "verify", Secondary({code: source}), reviewed_events=(event,)
    )
    assert report["status"] == "conflict"
    pd.testing.assert_frame_equal(result, frame)
