import math

import pytest

from support.synthetic import sample_factors, sample_prices
from support.synthetic_qlib import INITIAL_CASH, execute_qlib


def test_qlib_fills_next_open_and_account_reconciles(tmp_path):
    prices = sample_prices()
    factors = sample_factors(prices)
    result = execute_qlib(prices, factors, tmp_path / "qlib")
    assert result["metadata"]["engine"] == "qlib"
    assert result["trades"]
    first_day = min(fill["date"] for fill in result["trades"])
    assert first_day == "2025-01-07"
    initial_scores = sorted(
        [row for row in factors["history"] if row["date"] == "2025-01-06"],
        key=lambda row: row["composite"],
        reverse=True,
    )
    assert {fill["instrument"] for fill in result["trades"] if fill["date"] == first_day} == {
        row["instrument"] for row in initial_scores[:2]
    }
    cash = INITIAL_CASH
    amounts = {}
    for point in result["equity"][1:]:
        day = point["date"]
        for fill in [trade for trade in result["trades"] if trade["date"] == day]:
            opening = prices.loc[(prices["date"] == day) & (prices["instrument"] == fill["instrument"]), "open"].iloc[0]
            assert fill["price"] == pytest.approx(opening, rel=1e-6)
            sign = 1 if fill["side"] == "buy" else -1
            amounts[fill["instrument"]] = amounts.get(fill["instrument"], 0) + sign * fill["amount"]
            cash -= sign * fill["value"] + fill["cost"]
            assert fill["amount"] % 100 == pytest.approx(0)
        assert cash >= -1e-6
        assert point["cash"] == pytest.approx(cash)
        market_value = sum(
            amount * prices.loc[(prices["date"] == day) & (prices["instrument"] == symbol), "close"].iloc[0]
            for symbol, amount in amounts.items()
        )
        assert point["account_value"] == pytest.approx(cash + market_value, rel=1e-6)
        assert point["equity"] == pytest.approx(point["account_value"] / INITIAL_CASH)
    assert result["summary"]["metrics"]["total_return"] == pytest.approx(result["equity"][-1]["equity"] - 1)


def test_buy_and_hold_matches_hand_calculation_and_signal_lag(tmp_path):
    prices = sample_prices()
    factors = sample_factors(prices)
    for row in factors["history"]:
        row["composite"] = 100 if row["instrument"] == "SH600000" else 0
    result = execute_qlib(prices, factors, tmp_path / "hold", topk=1, open_cost=0, close_cost=0, min_cost=5)
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    quantity = math.floor(INITIAL_CASH * 0.9 / trade["price"] / 100) * 100
    assert trade["amount"] == quantity
    assert trade["cost"] == 5
    last_close = prices.loc[(prices["date"] == "2025-01-17") & (prices["instrument"] == "SH600000"), "close"].iloc[0]
    expected = INITIAL_CASH - quantity * trade["price"] - 5 + quantity * last_close
    assert result["equity"][-1]["account_value"] == pytest.approx(expected, rel=1e-6)
    # Scores changed at Jan 10 close must not alter trades at Jan 10 open.
    for row in factors["history"]:
        if row["date"] >= "2025-01-10":
            row["composite"] = 100 if row["instrument"] == "SZ000001" else 0
    altered = execute_qlib(prices, factors, tmp_path / "lag", topk=1, open_cost=0, close_cost=0, min_cost=5)
    assert [trade for trade in altered["trades"] if trade["date"] <= "2025-01-10"] == result["trades"]
    assert any(
        trade["date"] == "2025-01-13" and trade["instrument"] == "SZ000001" and trade["side"] == "buy"
        for trade in altered["trades"]
    )


def test_current_day_close_cannot_change_opening_fills(tmp_path):
    prices = sample_prices()
    factors = sample_factors(prices)
    for row in factors["history"]:
        row["composite"] = 100 if row["instrument"] == "SH600000" else 0
    original = execute_qlib(prices, factors, tmp_path / "original", topk=1)
    changed = prices.copy()
    selection = (changed["date"] == "2025-01-07") & (changed["instrument"] == "SH600000")
    changed.loc[selection, "close"] *= 3
    changed.loc[selection, "high"] = changed.loc[selection, "close"] * 1.01
    modified = execute_qlib(changed, factors, tmp_path / "changed", topk=1)
    fills = lambda result: [trade for trade in result["trades"] if trade["date"] == "2025-01-07"]
    assert fills(original)
    assert fills(modified) == fills(original)
