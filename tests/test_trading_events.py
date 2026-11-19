from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from test_joint_data import Secondary, bundle, market_frame

from finance_quant.application.quote_quality import _secondary_evidence, primary_events, primary_valid
from finance_quant.data.china_a_rules import (
    apply_execution_rules,
    board,
    ipo_no_band_mask,
    max_order_shares,
    round_shares,
)
from finance_quant.data.managed import coverage_report
from finance_quant.data.quality import adjustment_discontinuities, published_adjustment_discontinuities
from finance_quant.data.reviewed_events import REVIEWED_EVENTS
from finance_quant.data.security_history import normalize_names
from finance_quant.data.suspensions import LeadingHaltError, confirmed_halts
from finance_quant.data.trading_events import trading_events
from finance_quant.data.validation_errors import MarketDataError, failure_record


def history(code="600000.SH", day="20230615", announced="20230607", name="退市测试"):
    return normalize_names(
        pd.DataFrame(
            [
                {
                    "ts_code": code,
                    "name": "测试股份",
                    "start_date": "20100101",
                    "end_date": day,
                    "ann_date": "20100101",
                },
                {"ts_code": code, "name": name, "start_date": day, "end_date": None, "ann_date": announced},
            ]
        ),
        code,
    )


@pytest.mark.parametrize(
    "code,listed",
    [
        ("688159.SH", "2020-01-23"),
        ("300937.SZ", "2021-01-27"),
        ("603273.SH", "2023-10-23"),
        ("689009.SH", "2020-10-29"),
    ],
)
def test_ipo_candidates_share_execution_rule_and_do_not_include_sixth_session(code, listed):
    sessions = pd.bdate_range(listed, periods=7)
    # Exercise a holiday gap as well as ordinary weekdays.
    sessions = sessions[:2].append(sessions[2:] + pd.Timedelta(days=7))
    candidates = trading_events(code, listed, pd.DataFrame(), sessions)
    assert [event["date"] for event in candidates] == sessions[:5].strftime("%Y-%m-%d").tolist()
    assert ipo_no_band_mask(code[-2:] + code[:6], listed, sessions, sessions).tolist() == [True] * 5 + [False] * 2
    frame = market_frame(sessions, code).assign(is_suspended=False)
    frame.loc[2:, ["open", "high", "low", "close", "pre_close"]] *= 2
    frame.loc[2, "pre_close"] = 10
    frame.loc[2, "change"] = 1
    events = primary_events(frame, code, candidates)
    assert primary_valid(frame, sessions, np.ones(len(frame), bool), events)
    broken = frame.copy()
    broken.loc[2, "adj_factor"] *= 2
    assert not primary_valid(broken, sessions, np.ones(len(frame), bool), primary_events(broken, code, candidates))


@pytest.mark.parametrize("case", ["old_main_ipo", "old_chinext_ipo", "listing_outside_calendar"])
def test_ipo_rules_do_not_apply_to_older_boards_or_truncated_calendars(case):
    code, listed = ("600000.SH", "2020-01-02") if case == "old_main_ipo" else ("300001.SZ", "2020-01-02")
    sessions = pd.bdate_range("2020-01-02", periods=7)
    if case == "listing_outside_calendar":
        code, listed, sessions = "688159.SH", "2020-01-02", sessions[2:]
    assert trading_events(code, listed, pd.DataFrame(), sessions) == []


def test_delisting_event_uses_past_name_notice_and_price_identities_not_current_delist_date():
    sessions = pd.bdate_range(end="2023-06-15", periods=3)
    frame = market_frame(sessions).assign(is_suspended=False)
    frame.loc[2, ["open", "high", "low", "close"]] *= 0.2
    frame.loc[2, "change"] = -0.8
    candidates = trading_events("600000.SH", "2010-01-01", history(), sessions)
    events = primary_events(frame, "600000.SH", candidates)
    assert len(events) == 1 and events[0]["known_by"] == "2023-06-07"
    assert events[0]["point_in_time_certified"] is False
    assert primary_valid(frame, sessions, np.ones(3, bool), events)
    frame.loc[2, "change"] = -0.1
    assert primary_events(frame, "600000.SH", candidates) == []
    assert not primary_valid(frame, sessions, np.ones(3, bool), events)


@pytest.mark.parametrize(
    "case", ["same_day", "future", "missing", "ordinary_rename", "wrong_code", "conflict", "old_rule", "no_prior"]
)
def test_delisting_name_cannot_establish_an_undated_or_conflicting_exception(case):
    names = history()
    sessions = pd.bdate_range("2023-06-13", "2023-06-20")
    if case in {"same_day", "future", "missing"}:
        names.loc[1, "ann_date"] = {
            "same_day": pd.Timestamp("2023-06-15"),
            "future": pd.Timestamp("2023-06-16"),
            "missing": pd.NaT,
        }[case]
    elif case == "ordinary_rename":
        names.loc[1, "name"] = "测试科技"
    elif case == "wrong_code":
        names["instrument"] = "SZ000001"
    elif case == "conflict":
        names = pd.concat([names, names.iloc[[1]].assign(name="测试科技")])
    elif case == "old_rule":
        names = history(day="20190618", announced="20190611")
        sessions = pd.bdate_range("2019-06-17", "2019-06-21")
    else:
        names = names.iloc[1:]
    assert trading_events("600000.SH", "2010-01-01", names, sessions) == []


def test_unknown_extreme_move_still_fails_and_reviewed_resumption_is_date_specific():
    event = next(e for e in REVIEWED_EVENTS if e["code"] == "000995.SZ")
    sessions = pd.bdate_range(end=event["date"], periods=3)
    frame = market_frame(sessions, event["code"]).assign(is_suspended=False)
    frame.loc[2, ["open", "high", "low", "close"]] *= 4
    frame.loc[2, "change"] = 3
    assert primary_valid(frame, sessions, np.ones(3, bool), primary_events(frame, event["code"]))
    assert not primary_valid(frame, sessions, np.ones(3, bool), primary_events(frame, "600000.SH"))


@pytest.mark.parametrize("case", ["activity", "basis"])
def test_secondary_errors_keep_exact_dates_and_do_not_modify_captures(tmp_path, case):
    frame = market_frame()
    item = bundle(frame)
    raw, adjusted = item.raw.copy(), item.adjusted.copy()
    if case == "activity":
        raw.loc[2, "tradestatus"] = "0"
    else:
        adjusted.loc[2, "adjustflag"] = "3"
    before = raw.copy(deep=True), adjusted.copy(deep=True)
    item = replace(item, raw=raw, adjusted=adjusted)
    with pytest.raises(MarketDataError) as caught:
        confirmed_halts(item, "600000.SH", pd.DatetimeIndex(frame.date))
    record = failure_record("600000.SH", caught.value)
    assert record["dates"] == ["2025-01-06"] and record["date_count"] == 1 and record["action"]
    with pytest.raises(MarketDataError) as saved:
        _secondary_evidence(
            tmp_path,
            "asset",
            "600000.SH",
            {"start": "20250102", "end": "20250108"},
            pd.DatetimeIndex(frame.date),
            SecondaryBundle(item),
        )
    assert saved.value.evidence["quarantine_file"] == "quarantine/asset.json"
    assert (tmp_path / "quarantine/asset.json").exists()
    pd.testing.assert_frame_equal(raw, before[0])
    pd.testing.assert_frame_equal(adjusted, before[1])


class SecondaryBundle(Secondary):
    def __init__(self, item):
        super().__init__()
        self.item = item

    def fetch(self, *args):
        return self.item


def test_first_ipo_session_can_have_no_bands_without_inventing_an_adjusted_return():
    sessions = pd.bdate_range("2023-10-23", periods=6)
    frame = market_frame(sessions, "603273.SH").assign(is_suspended=False)
    frame.loc[:4, ["up_limit", "down_limit"]] = float("nan")
    candidates = trading_events("603273.SH", sessions[0], pd.DataFrame(), sessions)
    events = primary_events(frame, "603273.SH", candidates)
    assert len(events) == 5 and events[0]["adjusted_return"] is None
    assert events[0]["return_basis"] == "ipo_reference_price"
    assert primary_valid(frame, sessions, np.ones(6, bool), events)
    frame.loc[5, ["open", "high", "low", "close"]] *= 3
    frame.loc[5, "change"] = 2
    assert not primary_valid(frame, sessions, np.ones(6, bool), primary_events(frame, "603273.SH", candidates))


def test_future_name_corrections_cannot_change_an_earlier_event():
    sessions = pd.bdate_range("2023-06-01", "2023-06-20")
    names = history()
    before = trading_events("600000.SH", "2010-01-01", names, sessions)
    future = names.iloc[[1]].assign(start_date=pd.Timestamp("2023-06-05"), ann_date=pd.Timestamp("2024-01-02"))
    names = pd.concat([names.assign(end_date=pd.Timestamp("2024-12-31")), future])
    assert trading_events("600000.SH", "2010-01-01", names, sessions) == before


def test_published_event_projections_recheck_returns_and_legacy_inputs_stay_strict():
    frame = market_frame().assign(
        is_suspended=False, validation_level="single_source_passed", reviewed_unbounded_session=False
    )
    frame.loc[2:, ["open", "high", "low", "close", "pre_close"]] *= 0.2
    frame.loc[2, "pre_close"] = 10
    frame.loc[2, "change"] = -0.8
    frame.loc[2, "reviewed_unbounded_session"] = True
    frame.loc[2, ["up_limit", "down_limit"]] = float("nan")
    assert len(adjustment_discontinuities(frame)) == 1
    assert published_adjustment_discontinuities(frame) == []
    benchmark = market_frame().assign(instrument="SH000300")
    args = frame, benchmark, pd.DatetimeIndex(frame.date), ("600000.SH",), "000300.SH"
    assert coverage_report(*args)["status"] == "incomplete"
    assert coverage_report(*args, validated_shared=True)["status"] == "complete"
    frame.loc[2, "adj_factor"] *= 2
    assert published_adjustment_discontinuities(frame)


def test_secondary_mixed_adjustment_flags_can_use_a_clean_alternate_whole_interval(tmp_path):
    frame = market_frame()
    original = bundle(frame)
    bad = original.adjusted.copy()
    bad.loc[2, "adjustflag"] = "3"
    alternative = replace(
        original,
        adjusted=original.adjusted.assign(adjustflag="2"),
        metadata={**original.metadata, "adjusted_flag": "2"},
    )

    class Alternate(SecondaryBundle):
        def alternate_basis(self):
            return SecondaryBundle(alternative)

    selected, normalized, rejected = _secondary_evidence(
        tmp_path,
        "asset",
        "600000.SH",
        {"start": "20250102", "end": "20250108"},
        pd.DatetimeIndex(frame.date),
        Alternate(replace(original, adjusted=bad)),
    )
    assert selected is alternative and normalized.close.eq(10).all()
    assert rejected[0]["dates"] == ["2025-01-06"]
    assert (tmp_path / "quarantine/asset-secondary-flag1.json").exists()


def test_legacy_suspension_never_infers_unbounded_trading_from_a_delisting_name():
    sessions = pd.bdate_range("2020-12-29", "2021-01-06")
    frame = market_frame(sessions).assign(is_suspended=False)
    frame.loc[1 : len(frame) - 2, "is_suspended"] = True
    frame.loc[1 : len(frame) - 2, ["volume", "amount"]] = 0
    frame.loc[1 : len(frame) - 2, ["open", "high", "low", "close", "pre_close"]] = 10
    frame.loc[len(frame) - 1, ["open", "high", "low", "close"]] = 11
    frame.loc[len(frame) - 1, "change"] = 0.1
    candidates = trading_events("600000.SH", "2010-01-01", history(day="20210106", announced="20201229"), sessions)
    assert len(candidates) == 1
    assert primary_events(frame, "600000.SH", candidates) == []
    assert primary_valid(frame, sessions, np.ones(len(frame), bool), [])
    # A genuine large jump during a legacy halt remains unresolved without an explicit notice.
    frame.loc[len(frame) - 1, ["open", "high", "low", "close"]] = 30
    frame.loc[len(frame) - 1, "change"] = 2
    assert not primary_valid(frame, sessions, np.ones(len(frame), bool), primary_events(frame, "600000.SH", candidates))


def test_chinext_2020_reform_is_not_a_delisting_first_day_exception():
    sessions = pd.bdate_range("2020-09-21", "2020-09-25")
    names = history("300001.SZ", day="20200925", announced="20200921")
    assert trading_events("300001.SZ", "2010-01-01", names, sessions) == []


def test_dated_resumption_notice_can_verify_a_jump_after_a_legacy_suspension():
    sessions = pd.bdate_range("2020-03-19", "2022-08-22")
    frame = market_frame(sessions, "000670.SZ").assign(is_suspended=False)
    frame.loc[1 : len(frame) - 2, "is_suspended"] = True
    frame.loc[1 : len(frame) - 2, ["volume", "amount"]] = 0
    frame.loc[1 : len(frame) - 2, ["open", "high", "low", "close", "pre_close"]] = 10
    frame.loc[len(frame) - 1, ["open", "high", "low", "close"]] = 40
    frame.loc[len(frame) - 1, "change"] = 3
    events = primary_events(frame, "000670.SZ")
    assert len(events) == 1 and events[0]["known_by"] == "2022-08-15"
    assert "disc.static.szse.cn" in events[0]["source"]
    assert primary_valid(frame, sessions, np.ones(len(frame), bool), events)


def test_secondary_leading_halt_requests_history_expansion_instead_of_quarantining(tmp_path):
    frame = market_frame()
    item = bundle(frame)
    for part in (item.raw, item.adjusted):
        part.loc[:1, "tradestatus"] = "0"
        part.loc[:1, ["volume", "amount", "pctChg"]] = "0"
    with pytest.raises(LeadingHaltError):
        _secondary_evidence(
            tmp_path,
            "asset",
            "600000.SH",
            {"start": "20250102", "end": "20250108"},
            pd.DatetimeIndex(frame.date),
            SecondaryBundle(item),
        )
    assert not (tmp_path / "quarantine/asset.json").exists()


@pytest.mark.parametrize("instrument", ["SH689009", "SH689001", "SH689999"])
def test_cdr_quote_rules_do_not_enable_equity_execution(instrument):
    sessions = pd.bdate_range("2020-10-29", periods=6)
    assert ipo_no_band_mask(instrument, sessions[0], sessions, sessions).tolist() == [True] * 5 + [False]
    with pytest.raises(ValueError, match="CDR.*归档.*暂不支持回测"):
        board(instrument)
    with pytest.raises(ValueError, match="CDR"):
        max_order_shares(instrument)
    with pytest.raises(ValueError, match="CDR"):
        round_shares(200, 1, instrument)
    frame = pd.DataFrame(
        {
            "instrument": [instrument],
            "list_date": [sessions[0]],
            "date": [sessions[0]],
            "up_limit": [11.0],
            "down_limit": [9.0],
            "execution_available": [True],
        }
    )
    with pytest.raises(ValueError, match="CDR"):
        apply_execution_rules(frame, sessions)
