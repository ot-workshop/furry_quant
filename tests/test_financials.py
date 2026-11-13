"""Observation merging, version identity and point-in-time visibility of statements."""

import pandas as pd
import pytest

from finance_quant.data.financials import (
    FinancialBinding,
    FinancialStore,
    covered_codes,
    empty_observations,
    merge_observations,
    quarantined_rows,
    report_versions,
)
from finance_quant.research.fundamentals import (
    FEATURES,
    attach_fundamentals,
    fundamental_events,
    load_fundamentals,
)
from support.financial import balance, cashflow, income, publish


def observe(*frames, seen_on="2025-03-21"):
    result = empty_observations()
    for frame in frames:
        result = merge_observations(result, frame, seen_on=seen_on)
    return result


def versions(*frames, seen_on="2025-03-21"):
    return report_versions(observe(*frames, seen_on=seen_on), CALENDAR)


CALENDAR = pd.bdate_range("2023-01-02", "2027-06-30")


def second_session(announced: str) -> pd.Timestamp:
    day = pd.Timestamp(announced)
    later = CALENDAR[CALENDAR > day]
    assert len(later)
    return later[0].tz_localize("Asia/Shanghai") + pd.Timedelta(hours=9, minutes=30)


def test_report_period_alone_is_never_availability():
    frame = versions(income())
    assert len(frame) == 1
    available = frame.iloc[0]["available_at"]
    assert available.tz_convert("Asia/Shanghai") == second_session("2025-03-20")


def test_missing_announcement_is_quarantined_and_never_visible():
    frame = observe(income(announce="", report_type=1))
    frame.loc[0, "ann_date"] = pd.NaT
    frame.loc[0, "f_ann_date"] = pd.NaT
    assert quarantined_rows(frame).all()
    assert report_versions(frame, CALENDAR).empty


def test_in_place_correction_waits_for_our_first_observation():
    original = income()
    corrected = income(n_income_attr_p=12.0)
    # Same announcement date, different numbers, discovered later.
    frame = versions(original, corrected, seen_on="2025-05-10")
    same_period = frame.loc[frame.report_period == pd.Timestamp("2024-12-31")]
    assert len(same_period) == 2
    first, second = same_period.sort_values("revision").itertuples()
    assert first.available_at == second_session("2025-03-20")
    assert second.available_at.tz_convert("Asia/Shanghai") >= pd.Timestamp("2025-05-10").tz_localize("Asia/Shanghai")
    assert second.n_income_attr_p == 12.0


def test_adjustment_original_defers_the_overwriting_value():
    # The vendor replaced the annual figure in place; only the pre-adjustment copy (type 5)
    # and next year's comparative (type 4) preserve when the new number became public.
    overwritten = income(report_type=1, announce="20250320", n_income_attr_p=12.0)
    original = income(report_type=5, announce="20250320", n_income_attr_p=10.0)
    comparative = income(report_type=4, announce="20260320", n_income_attr_p=12.0)
    frame = versions(overwritten, original, comparative, seen_on="2025-05-10")
    rows = frame.loc[frame.report_period == pd.Timestamp("2024-12-31")].sort_values(["available_at", "priority"])
    assert list(rows.report_type) == [5, 1, 4]
    assert rows.iloc[0]["available_at"] == second_session("2025-03-20")
    # The overwriting value is only trusted once we actually observed it.
    assert rows.iloc[1]["available_at"].tz_convert("Asia/Shanghai") >= pandas_local("2025-05-10")
    # The next year's comparative publishes the same numbers earlier or later.
    assert rows.iloc[2]["available_at"] == second_session("2026-03-20")


def test_later_revision_of_the_same_period_replaces_the_previous_value():
    first = income(announce="20250320", n_income_attr_p=10.0)
    revised = income(announce="20250510", n_income_attr_p=12.0)
    frame = observe(first, revised, balance(), cashflow())
    events = fundamental_events(report_versions(frame, CALENDAR))
    before = events.loc[events.financial_available_at <= second_session("2025-03-20")]
    after = events.loc[events.financial_available_at <= second_session("2025-05-12")]
    assert math_close(before.iloc[-1]["roa_ttm"], 10.0 / 500.0)
    assert math_close(after.iloc[-1]["roa_ttm"], 12.0 / 500.0)


def pandas_local(day: str) -> pd.Timestamp:
    return pd.Timestamp(day).tz_localize("Asia/Shanghai")


def math_close(left, right):
    return left == pytest.approx(right)


def test_future_statements_do_not_change_past_features():
    parts = [income(), balance(), cashflow()]
    events = fundamental_events(versions(*parts))
    late = [income(period="20250930", announce="20251030", n_income_attr_p=99.0)]
    later = fundamental_events(versions(*parts, *late))
    cutoff = second_session("2025-04-01")
    before = events.loc[events.financial_available_at <= cutoff].reset_index(drop=True)
    after = later.loc[later.financial_available_at <= cutoff].reset_index(drop=True)
    pd.testing.assert_frame_equal(before, after)


def test_ttm_uses_year_to_date_cumulative_flow_and_average_balance():
    parts = [
        income(period="20241231", announce="20250320", total_revenue=400.0, oper_cost=240.0, n_income_attr_p=40.0),
        balance(period="20241231", announce="20250320", total_assets=1000.0, total_liab=400.0),
        balance(period="20231231", announce="20240320", total_assets=800.0, total_liab=320.0),
        cashflow(period="20241231", announce="20250320", n_cashflow_act=30.0),
        income(period="20250331", announce="20250425", total_revenue=120.0, oper_cost=70.0, n_income_attr_p=12.0),
        balance(period="20250331", announce="20250425", total_assets=1100.0, total_liab=440.0),
        cashflow(period="20250331", announce="20250425", n_cashflow_act=9.0),
    ]
    events = fundamental_events(versions(*parts)).sort_values("financial_available_at")
    latest = events.iloc[-1]
    # TTM profit = YTD(Q1) + annual - YTD(Q1 last year); Q1 last year is absent, so NaN-safe check below.
    assert latest["financial_report_period"] == pd.Timestamp("2025-03-31")
    # Without the prior-year Q1 the TTM flow cannot be built: the factor stays missing rather than wrong.
    assert pd.isna(latest["roe_ttm"]) or latest["roe_ttm"] == pytest.approx(12 / 900)
    assert latest["debt_to_assets"] == pytest.approx(0.4)
    annual = events.iloc[0]
    assert annual["roe_ttm"] == pytest.approx(40 / 300)
    assert pd.isna(latest["roe_ttm"])
    assert annual["gross_margin_ttm"] == pytest.approx(0.4)
    assert annual["asset_turnover_ttm"] == pytest.approx(400 / 900)
    assert annual["accruals_ttm"] == pytest.approx(10 / 900)


def test_stale_reports_are_dropped_from_the_cross_section():
    parts = [
        income(period="20231231", announce="20240320"),
        balance(period="20231231", announce="20240320"),
        cashflow(period="20231231", announce="20240320"),
    ]
    events = fundamental_events(versions(*parts))
    market = pd.DataFrame(
        {
            "instrument": ["SH600000", "SH600000"],
            "date": [pd.Timestamp("2024-04-01"), pd.Timestamp("2025-06-02")],
        }
    )
    joined = attach_fundamentals(market, events)
    assert pd.notna(joined.loc[0, "roe_ttm"])
    assert pd.isna(joined.loc[1, "roe_ttm"])


def test_published_version_is_content_addressed_and_reproducible(tmp_path):
    root = tmp_path / "financials"
    first = publish(root, [income(), balance(), cashflow()])
    assert first["id"] == FinancialStore(root).publish()["id"]
    # Re-fetching identical data must not invalidate research that pinned the version.
    again = publish(root, [income(), balance(), cashflow()])
    assert again["id"] == first["id"]
    assert covered_codes(again, ["600000.SH", "000001.SZ"]) == ["600000.SH"]


def test_store_round_trips_observations_and_rejects_tampering(tmp_path):
    root = tmp_path / "financials"
    manifest = publish(root, [income(), balance(), cashflow()])
    store = FinancialStore(root)
    observations, sessions = store.load(manifest, ["600000.SH"])
    assert len(observations) == 3
    assert len(sessions) > 0
    path = store._object_path(manifest["stocks"]["600000.SH"]["object"])
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="校验失败"):
        store.load(manifest, ["600000.SH"])


def test_superseded_vendor_row_of_the_same_announcement_is_dropped():
    """The vendor keeps both update_flag rows for one announcement; only the current one is a version."""
    current = income(update_flag="1", n_income_attr_p=12.0)
    stale = income(update_flag="0", n_income_attr_p=99.0)
    frame = versions(current, stale)
    assert len(frame) == 1
    assert frame.iloc[0]["n_income_attr_p"] == 12.0


def test_unflagged_duplicates_are_ordered_by_observation():
    first = income(n_income_attr_p=10.0)
    second = income(n_income_attr_p=12.0)
    frame = versions(first, second, seen_on="2025-05-10")
    same = frame.loc[frame.report_period == pd.Timestamp("2024-12-31")].sort_values("revision")
    assert [*same.n_income_attr_p] == [10.0, 12.0]
    assert same.iloc[1].available_at.tz_convert("Asia/Shanghai") >= pandas_local("2025-05-10")


def test_duplicate_rows_from_the_vendor_are_collapsed():
    frame = observe(income(), income())
    assert len(frame) == 1


def test_fundamentals_cover_only_stocks_of_the_version(tmp_path):
    root = tmp_path / "financials"
    manifest = publish(root, [income(), balance(), cashflow()])

    data = load_fundamentals(FinancialBinding(root, manifest["id"]), ["600000.SH", "000001.SZ"])
    assert data.covered == ("SH600000",)
    assert data.metadata(["SH600000", "SZ000001"])["covered"] == 1
    assert set(FEATURES).issubset(data.events.columns)
