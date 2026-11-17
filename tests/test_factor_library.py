"""Independent scalar checks, causality and native execution for ten factors."""

import json
import math
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
import test_pool_workflow_matrix as matrix
from test_pool_workflow_matrix import codes_for, request_for, run_request

from finance_quant.application.factor_workflow import publish_factor_run
from finance_quant.application.research_requests import validate_research_data
from finance_quant.configuration import BacktestConfig, DataConfig, ResearchConfig
from finance_quant.data.financials import FinancialBinding
from finance_quant.research.factor_library import FACTOR_LIBRARY
from finance_quant.research.fundamentals import load_fundamentals
from finance_quant.research.market_factors import market_factors
from finance_quant.research.registry import FactorConfig, FactorSpec
from finance_quant.storage import ResultStore
from support.financial import income, publish


def settings():
    params = {
        "momentum": {"window": 12},
        "value": {},
        "volatility": {"window": 8},
        "book_to_price": {},
        "size": {},
        "reversal": {"window": 9, "skip": 1},
        "medium_momentum": {"window": 20, "skip": 5},
        "downside_volatility": {"window": 8},
        "max_drawdown": {"window": 15},
        "amount_anomaly": {"window": 5, "baseline_window": 10},
    }
    return FactorConfig(tuple(FactorSpec(name, 0.1, **params[name]) for name in params))


def frame(size=7, days=180):
    rows = []
    dates = pd.bdate_range("2024-01-02", periods=days)
    for i, code in enumerate(codes_for(size, 53)):
        for t, day in enumerate(dates):
            factor = 2.0 if t >= 90 else 1.0
            close = (20 + i * 0.4 + 0.01 * t + 0.35 * (-1) ** t + 0.07 * np.sin(t * 0.3 + i)) / factor
            rows.append(
                {
                    "date": day,
                    "instrument": code[-2:] + code[:6],
                    "name": code,
                    "industry": "fixture",
                    "close": close,
                    "adj_factor": factor,
                    "pe_ttm": 10.0 + i,
                    "pb": 1.0 + i * 0.2,
                    "total_mv": 1e9 * (i + 1) * (1 + 0.002 * t),
                    "amount": 1e7 * (i + 1) * (1 + 0.01 * t + 0.1 * np.sin(t + i)),
                }
            )
    return pd.DataFrame(rows)


def scalar(section, spec):
    p = (section.close * section.adj_factor).to_numpy()
    w = spec.window
    if spec.name in {"momentum", "reversal", "medium_momentum"}:
        return (p[-1 - spec.skip] / p[-1 - w] - 1) * (-1 if spec.name == "reversal" else 1)
    if spec.name == "value":
        return (section.total_mv.iloc[0] / section.pe_ttm.iloc[0]) / section.total_mv.iloc[-1]
    if spec.name == "book_to_price":
        return 1 / section.pb.iloc[-1]
    if spec.name == "size":
        return -math.log(section.total_mv.iloc[-1])
    if spec.name in {"volatility", "downside_volatility"}:
        r = p[-w:] / p[-w - 1 : -1] - 1
        if spec.name == "downside_volatility":
            r = r[r < 0]
        return float(np.std(r, ddof=1) * math.sqrt(252))
    if spec.name == "max_drawdown":
        values = p[-w - 1 :]
        return float(np.min(values / np.maximum.accumulate(values) - 1))
    a = section.amount.to_numpy()
    return float(a[-w:].mean() / a[-w - spec.baseline_window : -w].mean())


@pytest.mark.parametrize("size", [2, 7, 17, 50])
def test_ten_raw_values_scores_and_contributions_match_scalar_formulas(size, tmp_path):
    market, config = frame(size), settings()
    reports = [
        income(
            code[2:] + "." + code[:2],
            period="20231231",
            announce="20240102",
            n_income_attr_p=part.total_mv.iloc[0] / part.pe_ttm.iloc[0],
        )
        for code, part in market.groupby("instrument")
    ]
    root = tmp_path / "financials"
    manifest = publish(root, reports, seen_on="2024-01-02")
    financials = load_fundamentals(
        FinancialBinding(root, manifest["id"]), [code[2:] + "." + code[:2] for code in market.instrument.unique()]
    )
    output = market_factors(market, "generated", config, financials=financials)
    for date in sorted(market.date.unique())[40::45]:
        expected = {
            code: {s.key: scalar(section.loc[section.date <= date], s) for s in config.specs}
            for code, section in market.groupby("instrument")
        }
        records = [r for r in output["history"] if r["date"] == pd.Timestamp(date).strftime("%Y-%m-%d")]
        assert len(records) == size
        for row in records:
            contribution = 0.0
            for spec in config.specs:
                value = expected[row["instrument"]][spec.key]
                assert row["raw"][spec.key] == pytest.approx(value, abs=1e-10)
                values = [e[spec.key] for e in expected.values()]
                better = sum(v > value if FACTOR_LIBRARY[spec.name].direction == "lower" else v < value for v in values)
                ties = sum(v == value for v in values)
                percentile = (better + (ties + 1) / 2) / size * 100
                assert row["scores"][spec.key] == pytest.approx(percentile)
                assert row["contributions"][spec.key] == pytest.approx(percentile * spec.weight)
                contribution += percentile * spec.weight
            assert row["composite"] == pytest.approx(contribution)
    json.dumps(output, allow_nan=False)


def test_changes_to_all_future_inputs_leave_past_raw_scores_and_diagnostics_unchanged():
    market, config = frame(), settings()
    cutoff = sorted(market.date.unique())[100]
    first = market_factors(market, "same", config)
    changed = market.copy()
    for field in ["close", "adj_factor", "pe_ttm", "pb", "total_mv", "amount"]:
        changed.loc[changed.date > cutoff, field] *= 7.3
    second = market_factors(changed, "same", config)
    cutoff_day = pd.Timestamp(cutoff).strftime("%Y-%m-%d")
    for key in ["history", "diagnostics"]:
        before = [row for row in first[key] if row["date"] <= cutoff_day]
        after = [row for row in second[key] if row["date"] <= cutoff_day]
        assert before == after
    prefix = market_factors(market.loc[market.date <= cutoff], "prefix", config)
    assert prefix["history"] == [r for r in first["history"] if r["date"] <= pd.Timestamp(cutoff).strftime("%Y-%m-%d")]


def test_instances_weights_windows_and_nonoverlapping_baseline_are_distinct(tmp_path):
    market = frame()
    config = FactorConfig(
        (FactorSpec("momentum", 0.4, 12, id="fast"), FactorSpec("momentum", 0.6, 35, skip=5, id="slow"))
    )
    output = market_factors(market, "fixture", config)
    assert set(output["history"][-1]["raw"]) == {"fast", "slow"}
    weighted = market_factors(
        market, "fixture", FactorConfig((replace(config.specs[0], weight=0.8), replace(config.specs[1], weight=0.2)))
    )
    common = {(r["date"], r["instrument"]): r for r in weighted["history"]}
    assert all(row["raw"] == common[(row["date"], row["instrument"])]["raw"] for row in output["history"])
    assert any(row["composite"] != common[(row["date"], row["instrument"])]["composite"] for row in output["history"])
    manifest = {"id": "immutable-fixture"}
    one = publish_factor_run(market, manifest, tmp_path, config)
    two = publish_factor_run(
        market, manifest, tmp_path, FactorConfig((replace(config.specs[0], window=15), config.specs[1]))
    )
    assert one["metadata"]["id"] != two["metadata"]["id"]
    assert publish_factor_run(market, manifest, tmp_path, config)["metadata"]["id"] == one["metadata"]["id"]
    anomaly = FactorConfig((FactorSpec("amount_anomaly", 1, 5, baseline_window=10),))
    first = market_factors(market, "fixture", anomaly)
    other = market_factors(market, "fixture", FactorConfig((FactorSpec("amount_anomaly", 1, 5, baseline_window=30),)))
    assert first["history"][-1]["raw"] != other["history"][-1]["raw"]


def test_missing_new_fields_and_suspended_carried_values_are_not_filled_or_reweighted():
    market = frame()
    last = market.date.max()
    target = market.instrument.iloc[0]
    market.loc[market.instrument.eq(target) & market.date.eq(last), "pb"] = np.nan
    output = market_factors(market, "fixture", settings())
    excluded = next(r for r in output["diagnostics"] if r["date"] == str(last.date()) and r["instrument"] == target)
    assert excluded["reasons"]["book_to_price"] == "missing_or_nonpositive_pb"
    assert not any(r["date"] == str(last.date()) and r["instrument"] == target for r in output["history"])
    assert output["metadata"]["availability_by_date"][str(last.date())]["eligible"] == 6
    market["is_suspended"] = False
    market.loc[market.instrument.eq(target) & market.date.eq(last), "is_suspended"] = True
    market.loc[market.instrument.eq(target) & market.date.eq(last), "pb"] = 1.0
    output = market_factors(market, "fixture", settings())
    excluded = next(r for r in output["diagnostics"] if r["date"] == str(last.date()) and r["instrument"] == target)
    assert excluded["reasons"]["book_to_price"] == excluded["reasons"]["size"] == "suspended_auxiliary_value"
    with pytest.raises(ValueError, match="缺少数据字段"):
        market_factors(market.drop(columns="pb"), "fixture", settings())
    with pytest.raises(ValueError, match="交易日缺口"):
        market_factors(market.drop(market.index[35]), "fixture", settings())


@pytest.mark.parametrize(
    "params",
    [
        dict(name="value", skip=1),
        dict(name="size", window=20),
        dict(name="book_to_price", baseline_window=30),
        dict(name="reversal", window=20, skip=20),
        dict(name="medium_momentum", skip=-1),
        dict(name="amount_anomaly", baseline_window=0),
        dict(name="momentum", id="available_at"),
        dict(name="momentum", id="size"),
        dict(name="momentum", id="../bad"),
        dict(name="volatility", skip=1),
    ],
)
def test_bad_parameters_and_reserved_instance_ids_are_rejected(params):
    with pytest.raises(ValueError):
        FactorSpec(weight=1.0, **params)


@pytest.mark.parametrize("bad_amount", [-100.0, np.inf, np.nan])
def test_invalid_amount_inside_either_window_excludes_stock(bad_amount):
    config = FactorConfig((FactorSpec("amount_anomaly", 1.0, 5, baseline_window=10),))
    for offset in [2, 8]:
        market = frame()
        target = market.instrument.iloc[0]
        dates = sorted(market.date.unique())
        market.loc[market.instrument.eq(target) & market.date.eq(dates[-offset]), "amount"] = bad_amount
        output = market_factors(market, "fixture", config)
        last = pd.Timestamp(dates[-1]).strftime("%Y-%m-%d")
        assert not any(r["date"] == last and r["instrument"] == target for r in output["history"])
        excluded = next(r for r in output["diagnostics"] if r["date"] == last and r["instrument"] == target)
        assert excluded["raw"]["amount_anomaly"] is None


def test_two_stock_activity_rejection_explains_counts_and_later_start_recovers(tmp_path):
    market = frame(size=2)
    dates = sorted(market.date.unique())
    target = market.instrument.iloc[0]
    market.loc[market.instrument.eq(target) & market.date.between(dates[80], dates[99]), "amount"] = 0.0
    codes = tuple(code[2:] + "." + code[:2] for code in market.instrument.unique())
    data = DataConfig(tmp_path, str(pd.Timestamp(dates[0]).date()), str(pd.Timestamp(dates[-1]).date()), codes)
    config = ResearchConfig(
        data,
        BacktestConfig(str(pd.Timestamp(dates[100]).date()), str(pd.Timestamp(dates[170]).date()), topk=1),
        tmp_path / "runs",
        FactorConfig((FactorSpec("amount_anomaly", 1.0, 5, baseline_window=10),)),
    )
    calendar = pd.DataFrame({"date": dates, "is_open": True})
    with pytest.raises(ValueError, match=r"共同有效1/2只.*成交额异动\[amount_anomaly\] 1/2只"):
        validate_research_data(config, market, calendar)
    later = replace(config, backtest=replace(config.backtest, start=str(pd.Timestamp(dates[125]).date())))
    validate_research_data(later, market, calendar)


def alternating_bars(code, dates):
    dates = pd.DatetimeIndex(dates)
    n = np.busday_count("2024-10-01", dates.to_numpy().astype("datetime64[D]"))
    i = int(code[:6]) % 17

    def close(t):
        return 10 + i + 0.01 * t + 0.35 * (-1.0) ** t + 0.07 * np.sin(t * 0.3 + i)

    c, previous = close(n), close(n - 1)
    return pd.DataFrame(
        {
            "date": dates,
            "instrument": code[-2:] + code[:6],
            "name": code,
            "industry": "fixture",
            "open": previous + 0.02,
            "close": c,
            "high": np.maximum(previous + 0.02, c) + 0.1,
            "low": np.minimum(previous + 0.02, c) - 0.1,
            "pre_close": previous,
            "volume": 1e6 + n % 5 * 20000,
            "amount": c * 1e6,
            "change": c / previous - 1,
            "adj_factor": 1.0,
            "pe_ttm": 10.0 + i,
            "pb": 1.0 + i * 0.1,
            "total_mv": 1e9 * (i + 1),
            "up_limit": previous * 1.1,
            "down_limit": previous * 0.9,
        }
    )


@pytest.mark.parametrize("size", [2, 7, 17, 50])
def test_ten_factors_through_queued_task_native_qlib_and_evaluation(tmp_path, monkeypatch, size):
    monkeypatch.setattr(matrix, "bars", alternating_bars)
    request = request_for(codes_for(size, 63))
    request["factors"] = settings().as_list()
    task = run_request(request, tmp_path / "data", tmp_path / "runs")
    assert task["status"] == "succeeded", task["error"]
    result = task["result"]
    store = ResultStore(tmp_path / "runs")
    factors = store.get("factors", result["factor_run_id"])
    backtest = store.get("backtests", result["backtest_run_id"])
    evaluation = store.get("evaluations", result["evaluation_run_id"])
    assert len(factors["metadata"]["weights"]) == 10
    assert backtest["metadata"]["factor_run_id"] == evaluation["metadata"]["factor_run_id"] == result["factor_run_id"]
    assert evaluation["methodology"]["directions"]["downside_volatility"] == "lower"
    assert evaluation["methodology"]["directions"]["max_drawdown"] == "higher"
    assert set(factors["history"][-1]["contributions"]) == set(settings().weights)
