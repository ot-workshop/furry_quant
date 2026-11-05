import pandas as pd
import pytest

from finance_quant.data.index_universe import historical_codes, members_on, membership_mask, normalize_memberships


def calendar():
    days = pd.date_range("2019-12-01", "2021-01-31")
    return pd.DataFrame({"date": days, "is_open": days.weekday < 5})


def weights():
    return pd.DataFrame(
        [
            {"index_code": "399300.SZ", "trade_date": day, "con_code": code, "weight": 50.0}
            for day, codes in [("20191231", ["600000.SH", "000001.SZ"]), ("20200630", ["600000.SH", "600036.SH"])]
            for code in codes
        ]
    )


def test_snapshots_are_forward_only_and_do_not_fill_their_own_month():
    intervals = normalize_memberships(weights(), calendar(), expected_count=2)
    assert members_on(intervals, "2020-06-30") == ("000001.SZ", "600000.SH")
    assert members_on(intervals, "2020-07-01") == ("600000.SH", "600036.SH")
    assert members_on(intervals, "2019-12-31") == ()
    assert historical_codes(intervals, "2020-01-01", "2020-12-31") == ("000001.SZ", "600000.SH", "600036.SH")
    assert membership_mask(intervals, "600036.SH", pd.to_datetime(["2020-06-29", "2020-07-01"])).tolist() == [
        False,
        True,
    ]


def test_future_member_and_weight_perturbation_cannot_change_earlier_universe():
    before = normalize_memberships(weights(), calendar(), expected_count=2)
    future = weights()
    future.loc[future.trade_date.eq("20200630") & future.con_code.eq("600036.SH"), "con_code"] = "000858.SZ"
    after = normalize_memberships(future, calendar(), expected_count=2)
    for day in pd.bdate_range("2020-01-01", "2020-06-30"):
        assert members_on(before, day) == members_on(after, day)


@pytest.mark.parametrize("case", ["missing", "duplicate", "date", "weight"])
def test_incomplete_membership_is_never_silently_used(case):
    raw = weights()
    if case == "missing":
        raw = raw.iloc[1:]
    elif case == "duplicate":
        raw = pd.concat([raw, raw.iloc[:1]])
    elif case == "date":
        raw.loc[0, "trade_date"] = "20191229"
    else:
        raw.loc[0, "weight"] = 1
    with pytest.raises(ValueError):
        normalize_memberships(raw, calendar(), expected_count=2)
