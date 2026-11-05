import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from finance_quant.application.real_workflow import adjusted_quotes
from finance_quant.data.market import SnapshotFetcher
from finance_quant.integrations.qlib_runner import run_qlib
from finance_quant.research.market_factors import market_factors
from finance_quant.storage import ResultStore
from support.fakes import make_app
from support.synthetic import sample_factors, sample_prices


def sample_market():
    prices = sample_prices()
    prices["adj_factor"] = 1.0
    prices["up_limit"] = 1_000_000.0
    prices["down_limit"] = 0.01
    close = prices.pivot(index="date", columns="instrument", values="close")
    index_close = (close / close.iloc[0]).mean(axis=1) * 100
    benchmark = pd.DataFrame({field: index_close for field in ["open", "close", "high", "low"]})
    benchmark["date"], benchmark["instrument"] = benchmark.index, "SH000300"
    benchmark["volume"] = 1_000_000.0
    return prices, benchmark.reset_index(drop=True)


def constant_signal(prices):
    factors = sample_factors(prices)
    for row in factors["history"]:
        row["composite"] = 100 if row["instrument"] == "SH600000" else 0
    return factors


def execute(prices, benchmark, factors, destination):
    quotes = adjusted_quotes(prices, benchmark)
    return run_qlib(
        quotes,
        factors,
        destination,
        calendar=pd.bdate_range("2025-01-02", "2025-01-31"),
        start="2025-01-07",
        end="2025-01-17",
        initial_date="2025-01-06",
        names=prices.drop_duplicates("instrument").set_index("instrument")["name"].to_dict(),
        topk=1,
        limit_expressions=("$open >= $up_limit - 0.0001", "$open <= $down_limit + 0.0001"),
    )


def test_qlib_split_preserves_wealth_and_converts_equivalent_shares(tmp_path):
    prices, benchmark = sample_market()
    factors = constant_signal(prices)
    original = execute(prices, benchmark, factors, tmp_path / "base")
    split = prices.copy()
    after = (split["instrument"] == "SH600000") & (split["date"] >= "2025-01-10")
    split.loc[after, "adj_factor"] = 2
    split.loc[after, "volume"] *= 2
    for field in ["open", "high", "low", "close", "up_limit", "down_limit"]:
        split.loc[after, field] /= 2
    changed = execute(split, benchmark, factors, tmp_path / "split")
    assert changed["summary"]["metrics"] == pytest.approx(original["summary"]["metrics"])
    assert changed["equity"][-1]["account_value"] == pytest.approx(original["equity"][-1]["account_value"])
    assert changed["positions"][-1]["amount"] == 2 * original["positions"][-1]["amount"]
    assert changed["positions"][-1]["price"] == original["positions"][-1]["price"] / 2


def test_open_limit_uses_open_price_not_future_close(tmp_path):
    prices, benchmark = sample_market()
    factors = constant_signal(prices)
    base = execute(prices, benchmark, factors, tmp_path / "base")
    changed = prices.copy()
    day = (changed["instrument"] == "SH600000") & (changed["date"] == "2025-01-07")
    changed.loc[day, "close"] *= 3
    changed.loc[day, "high"] = changed.loc[day, "close"] * 1.01
    late_close = execute(changed, benchmark, factors, tmp_path / "close")
    opening_fills = lambda result: [trade for trade in result["trades"] if trade["date"] == "2025-01-07"]
    assert opening_fills(late_close) == opening_fills(base)
    blocked = prices.copy()
    blocked.loc[day, "up_limit"] = blocked.loc[day, "open"]
    limit_result = execute(blocked, benchmark, factors, tmp_path / "limit")
    assert not opening_fills(limit_result)


def test_explicit_late_signal_availability_is_rejected(tmp_path):
    prices, benchmark = sample_market()
    factors = constant_signal(prices)
    for row in factors["history"]:
        row["available_at"] = "2025-02-01T18:00:00+08:00"
    with pytest.raises(ValueError, match="not available"):
        execute(prices, benchmark, factors, tmp_path / "late")


def extended_market():
    records = []
    for index, code in enumerate(["SH600000", "SZ000001", "SH600519"]):
        for step, date in enumerate(pd.bdate_range("2025-01-02", periods=45)):
            records.append(
                {
                    "instrument": code,
                    "date": date,
                    "close": 10 + index + step * 0.03 + np.sin(step * 0.4 + index) * 0.2,
                    "adj_factor": 1.0,
                    "pe_ttm": 10 + index * 5,
                    "name": code,
                    "industry": "test",
                }
            )
    return pd.DataFrame(records)


def test_market_factors_do_not_read_future_prices_or_adjustment_factors():
    market = extended_market()
    baseline = market_factors(market, "fixture")
    changed = market.copy()
    future = changed["date"] > "2025-02-10"
    changed.loc[future, "close"] *= 2
    changed.loc[future, "adj_factor"] *= 3
    changed.loc[future, "pe_ttm"] *= 4
    altered = market_factors(changed, "fixture")
    before = lambda result: [row for row in result["history"] if row["date"] <= "2025-02-10"]
    assert before(baseline) == before(altered)
    assert baseline["metadata"]["is_synthetic"] is False
    assert "quality" not in baseline["metadata"]["weights"]


def test_missing_valuation_is_excluded_and_counted_in_coverage():
    market = extended_market()
    market.loc[market["instrument"] == "SH600000", "pe_ttm"] = np.nan
    factors = market_factors(market, "fixture")
    assert all(row["instrument"] != "SH600000" for row in factors["history"])
    assert all(point == {"eligible": 2, "universe": 3} for point in factors["metadata"]["coverage_by_date"].values())


def test_snapshot_requests_are_cached_and_corruption_is_rejected(tmp_path):
    class Provider:
        def __init__(self):
            self.calls = 0

        def query(self, api, **params):
            self.calls += 1
            return pd.DataFrame({"trade_date": ["20260930"], "close": [10.0]})

    provider = Provider()
    fetcher = SnapshotFetcher(provider, tmp_path)
    fetcher.get("daily", ts_code="000001.SZ")
    fetcher.get("daily", ts_code="000001.SZ")
    assert provider.calls == 1
    path = next(tmp_path.glob("*.parquet"))
    path.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        fetcher.get("daily", ts_code="000001.SZ")
    assert provider.calls == 1
    with pytest.raises(ValueError, match="Credentials"):
        fetcher.get("daily", token="never-persist-this")


def test_real_and_synthetic_results_are_distinct_and_real_is_default(tmp_path):
    sample = sample_factors(sample_prices())
    store = ResultStore(tmp_path)
    store.publish("factors", "sample", sample)
    real = market_factors(extended_market(), "fixture")
    real["metadata"]["id"] = "real"
    store.publish("factors", "real", real)
    client = TestClient(make_app(tmp_path))
    assert client.get("/api/v1/overview").json()["mode"] == "mixed"
    assert client.get("/api/v1/factor-runs").json()["items"][0]["id"] == "real"
    response = client.get("/api/v1/factor-runs/real/scores?sort_by=volatility")
    assert response.status_code == 200
    assert "volatility" in response.json()["items"][0]["scores"]
    history = client.get("/api/v1/instruments/SH600000/factor-history?run_id=real")
    assert history.status_code == 200
    assert history.json()["scheme"] == real["metadata"]["scheme"]
    assert history.json()["items"]
    assert client.get("/api/v1/instruments/SH600000/factor-history?run_id=real&scheme=wrong").status_code == 422


def test_full_account_interval_is_required(tmp_path):
    prices, benchmark = sample_market()
    factors = constant_signal(prices)
    quotes = adjusted_quotes(prices, benchmark)
    missing_end = quotes[~((quotes["instrument"] == "SH600000") & (quotes["date"] == "2025-01-17"))]
    with pytest.raises(ValueError, match="full requested"):
        run_qlib(
            missing_end,
            factors,
            tmp_path / "incomplete",
            calendar=pd.bdate_range("2025-01-02", "2025-01-31"),
            start="2025-01-07",
            end="2025-01-17",
            initial_date="2025-01-06",
            names=prices.drop_duplicates("instrument").set_index("instrument")["name"].to_dict(),
        )


def test_cache_is_scoped_to_provider_origin(tmp_path):
    from types import SimpleNamespace

    class Provider:
        def __init__(self, url):
            self.settings = SimpleNamespace(http_url=url)
            self.calls = 0

        def query(self, api, **params):
            self.calls += 1
            return pd.DataFrame({"close": [float(self.calls)]})

    first, second = Provider("https://first.invalid"), Provider("https://second.invalid")
    SnapshotFetcher(first, tmp_path).get("daily", ts_code="000001.SZ")
    SnapshotFetcher(second, tmp_path).get("daily", ts_code="000001.SZ")
    assert first.calls == second.calls == 1
    assert len(list(tmp_path.glob("*.parquet"))) == 2
