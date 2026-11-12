"""Cross-stock, calendar and immutable-version contracts before refactoring."""

import pandas as pd
import pytest

from finance_quant.data.financials import FinancialStore, merge_observations, report_versions
from finance_quant.research.fundamentals import attach_fundamentals, fundamental_events
from support.financial import balance, cashflow, income, publish


@pytest.mark.parametrize("codes", [("600000.SH", "000001.SZ"), ("688001.SH", "300750.SZ", "600519.SH")])
def test_cross_stock_asof_never_borrows_another_stocks_report(codes):
    observations = None
    for i, code in enumerate(codes):
        for maker in (income, balance, cashflow):
            observations = merge_observations(
                observations, maker(ts_code=code, announce=f"202503{20 + i}"), seen_on="2025-03-25"
            )
    sessions = pd.bdate_range("2025-03-19", "2025-04-01")
    events = fundamental_events(report_versions(observations, sessions, "announcement_estimate"))
    market = pd.DataFrame({"instrument": [code[-2:] + code[:6] for code in codes], "date": pd.Timestamp("2025-03-21")})
    joined = attach_fundamentals(market, events)
    assert pd.notna(joined.iloc[0].roe_ttm)
    assert joined.iloc[1:].roe_ttm.isna().all()


def test_announcement_waits_for_actual_exchange_session():
    observations = merge_observations(None, income(announce="20250403"), seen_on="2025-04-03")
    sessions = pd.to_datetime(["2025-04-03", "2025-04-07", "2025-04-08"])
    versions = report_versions(observations, sessions)
    assert versions.iloc[0].available_at.tz_convert("Asia/Shanghai") == pd.Timestamp(
        "2025-04-07 09:30", tz="Asia/Shanghai"
    )


def test_pinned_version_survives_later_correction_and_new_stock(tmp_path):
    store = FinancialStore(tmp_path)
    original = publish(tmp_path, [income(), balance(), cashflow()])
    before, sessions = store.load(original, ["600000.SH"])
    changed = merge_observations(store.observations("600000.SH"), income(n_income_attr_p=999), "2025-07-02")
    store.save_stock("600000.SH", changed, fetched_at="2025-07-02T00:00:00Z")
    after_version = publish(tmp_path, [income(ts_code="688001.SH")])
    assert after_version["id"] != original["id"]
    after, _ = store.load(store.version(original["id"]), ["600000.SH"])
    pd.testing.assert_frame_equal(before, after)
    pd.testing.assert_frame_equal(report_versions(before, sessions), report_versions(after, sessions))
