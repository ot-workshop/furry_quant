import pandas as pd
import pytest

from finance_quant.data.point_in_time import (
    availability_from_announcement_date,
    financial_history_as_of,
    latest_financials_as_of,
)


def reports():
    return pd.DataFrame(
        [
            {
                "instrument": "SH600000",
                "report_period": "2024-12-31",
                "available_at": "2025-03-20T20:00:00+08:00",
                "revision": 0,
                "roe": 0.1,
            },
            {
                "instrument": "SH600000",
                "report_period": "2024-12-31",
                "available_at": "2025-05-20T19:00:00+08:00",
                "revision": 1,
                "roe": 0.2,
            },
            {
                "instrument": "SH600000",
                "report_period": "2025-03-31",
                "available_at": "2025-04-25T18:00:00+08:00",
                "revision": 0,
                "roe": 0.15,
            },
        ]
    )


def test_report_period_is_not_publication_time():
    frame = reports()
    assert latest_financials_as_of(frame, "2025-01-31T15:00:00+08:00").empty
    assert latest_financials_as_of(frame, "2025-03-20T15:00:00+08:00").empty
    visible = latest_financials_as_of(frame, "2025-03-20T20:00:00+08:00")
    assert visible.iloc[0]["roe"] == 0.1


def test_revisions_do_not_rewrite_past_information():
    frame = reports()
    before = financial_history_as_of(frame, "2025-05-19T15:00:00+08:00")
    assert before.loc[before["report_period"] == "2024-12-31", "roe"].item() == 0.1
    after = financial_history_as_of(frame, "2025-05-21T15:00:00+08:00")
    assert after.loc[after["report_period"] == "2024-12-31", "roe"].item() == 0.2
    changed = frame.copy()
    changed.loc[changed["revision"] == 1, "roe"] = 99
    pd.testing.assert_frame_equal(before, financial_history_as_of(changed, "2025-05-19T15:00:00+08:00"))


def test_visibility_filter_precedes_history_window_and_period_selection():
    early = financial_history_as_of(reports(), "2025-03-21T15:00:00+08:00", periods=1)
    assert early.iloc[0]["report_period"] == pd.Timestamp("2024-12-31")
    # A recent revision of an older period must not replace a newer report period.
    latest = latest_financials_as_of(reports(), "2025-05-21T15:00:00+08:00")
    assert latest.iloc[0]["report_period"] == pd.Timestamp("2025-03-31")


def test_date_only_announcement_waits_until_next_exchange_session():
    available = availability_from_announcement_date("2025-01-24", ["2025-01-24", "2025-02-05"])
    assert available == pd.Timestamp("2025-02-05T09:30:00+08:00")
    frame = pd.DataFrame(
        [
            {
                "instrument": "SH600000",
                "report_period": "2024-12-31",
                "available_at": available,
                "revision": 0,
                "roe": 0.1,
            }
        ]
    )
    assert latest_financials_as_of(frame, "2025-01-24T15:00:00+08:00").empty
    assert latest_financials_as_of(frame, "2025-02-05T09:29:59+08:00").empty
    assert len(latest_financials_as_of(frame, available)) == 1
    with pytest.raises(ValueError, match="calendar"):
        availability_from_announcement_date("2025-01-24", ["2025-01-24"])


def test_visibility_is_separate_for_each_stock():
    frame = reports()
    other = frame.iloc[[0]].copy()
    other["instrument"] = "SZ000001"
    other["available_at"] = "2025-04-01T18:00:00+08:00"
    frame = pd.concat([frame, other], ignore_index=True)
    assert set(latest_financials_as_of(frame, "2025-03-21T15:00:00+08:00")["instrument"]) == {"SH600000"}


@pytest.mark.parametrize(
    "mutation", ["missing_available_at", "naive_available_at", "duplicate", "premature", "fractional_revision"]
)
def test_ambiguous_or_invalid_financial_inputs_are_rejected(mutation):
    frame = reports()
    if mutation == "missing_available_at":
        frame = frame.drop(columns="available_at")
    elif mutation == "naive_available_at":
        frame.loc[0, "available_at"] = "2025-03-20"
    elif mutation == "duplicate":
        frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    elif mutation == "premature":
        frame.loc[0, "available_at"] = "2024-12-30T15:00:00+08:00"
    elif mutation == "fractional_revision":
        frame["revision"] = frame["revision"].astype(float)
        frame.loc[0, "revision"] = 0.5
    with pytest.raises(ValueError):
        latest_financials_as_of(frame, "2025-05-21T15:00:00+08:00")


def test_as_of_requires_timezone():
    with pytest.raises(ValueError, match="timezone"):
        latest_financials_as_of(reports(), "2025-05-21")
