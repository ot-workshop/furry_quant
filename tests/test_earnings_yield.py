"""Signed point-in-time earnings yield and explicit missing-score policies."""

import json
from dataclasses import replace

import pandas as pd
import pytest
from test_factor_library import frame
from test_industry_neutralization import history

from finance_quant.application.selection_context import factor_configs
from finance_quant.data.financials import FinancialBinding
from finance_quant.data.symbols import from_qlib
from finance_quant.research.evaluation import evaluate_factors
from finance_quant.research.fundamentals import load_fundamentals
from finance_quant.research.market_factors import market_factors
from finance_quant.research.neutralization import NeutralizationConfig, residualize
from finance_quant.research.registry import EvaluationConfig, FactorConfig, FactorSpec
from finance_quant.storage import ResultStore
from finance_quant.storage_tables import StreamingTables, read_rows
from support.financial import income, publish


def inputs(tmp_path):
    market = frame(4, 20)
    market["open"] = market.close
    codes = [from_qlib(code) for code in market.instrument.unique()]
    reports = [
        income(code, period="20231231", announce="20240103", n_income_attr_p=profit)
        for code, profit in zip(codes, (20.0, 0.0, -10.0, None), strict=True)
    ]
    root = tmp_path / "financials"
    version = publish(root, reports, seen_on="2024-01-03")
    financials = load_fundamentals(FinancialBinding(root, version["id"]), codes)
    return market, financials, reports, codes


def config(policy="zero"):
    return FactorConfig((FactorSpec("value", 0.5, missing_policy=policy), FactorSpec("momentum", 0.5, 2)))


@pytest.mark.parametrize("policy,filled", [("zero", 0.0), ("neutral", 50.0), ("exclude", None)])
def test_signed_profits_and_real_missing_are_distinct(tmp_path, policy, filled):
    market, financials, _, codes = inputs(tmp_path)
    result = market_factors(market, "fixture", config(policy), financials=financials)
    day = "2024-01-08"
    rows = {row["instrument"]: row for row in result["history"] if row["date"] == day}
    assert len(rows) == (3 if filled is None else 4)
    for index, profit in enumerate((20, 0, -10)):
        symbol = codes[index][-2:] + codes[index][:6]
        cap = market.loc[market.instrument.eq(symbol) & market.date.eq(day), "total_mv"].iloc[0]
        assert rows[symbol]["raw"]["value"] == pytest.approx(profit / cap)
        assert rows[symbol]["factor_details"]["value"]["ttm_net_profit"] == profit
        assert rows[symbol]["factor_details"]["value"]["imputed"] is False
    missing = codes[3][-2:] + codes[3][:6]
    if filled is not None:
        row = rows[missing]
        assert row["raw"]["value"] is None
        assert row["scores"]["value"] == filled
        assert row["contributions"]["value"] == filled * 0.5
        assert row["composite"] == pytest.approx(sum(row["contributions"].values()))
        assert row["factor_details"]["value"]["imputed"] is True
    counts = result["metadata"]["availability_by_date"][day]["factors"]["value"]
    assert (counts["positive"], counts["zero"], counts["negative"], counts["missing"]) == (1, 1, 1, 1)
    assert counts["imputed"] == (0 if filled is None else 1)
    json.dumps(result, allow_nan=False)


def test_imputed_scores_never_enter_single_factor_ic_or_correlations(tmp_path):
    market, financials, _, _ = inputs(tmp_path)
    settings = EvaluationConfig((1,), 3, 3)
    outputs = []
    for policy in ("zero", "neutral"):
        factors = market_factors(market, "fixture", config(policy), financials=financials)
        result = evaluate_factors(
            market, factors, sorted(market.date.unique()), start="2024-01-08", end="2024-01-26", config=settings
        )
        daily = result["factors"]["value"]["horizons"]["1"]["daily"]
        assert all(row["observations"] == 3 for row in daily if row["ic"] is not None)
        composite = result["factors"]["composite"]["horizons"]["1"]["daily"]
        assert all(row["observations"] == 4 for row in composite if row["ic"] is not None)
        pair = next(pair for pair in result["correlations"]["pairs"] if pair["first"] == pair["second"] == "value")
        assert all(row["observations"] == 3 for row in pair["daily"])
        outputs.append(result)
    assert outputs[0]["factors"]["value"] == outputs[1]["factors"]["value"]
    assert outputs[0]["correlations"] == outputs[1]["correlations"]


def test_future_reports_and_market_caps_cannot_change_past_earnings(tmp_path):
    market, original, reports, codes = inputs(tmp_path)
    future = publish(
        original_root := tmp_path / "future-financials",
        [
            *reports,
            *[income(code, period="20241231", announce="20250320", n_income_attr_p=9999.0) for code in codes],
        ],
        seen_on="2025-03-20",
    )
    updated = load_fundamentals(FinancialBinding(original_root, future["id"]), codes)
    baseline = market_factors(market, "same", config(), financials=original)
    cutoff = pd.Timestamp("2024-01-15")
    changed = market.copy()
    changed.loc[changed.date.gt(cutoff), ["total_mv", "close", "adj_factor"]] *= 9
    after = market_factors(changed, "same", config(), financials=updated)
    for key in ("history", "diagnostics"):
        assert [r for r in baseline[key] if r["date"] <= str(cutoff.date())] == [
            r for r in after[key] if r["date"] <= str(cutoff.date())
        ]


def test_only_missing_factors_cannot_create_a_portfolio(tmp_path):
    market, _, _, _ = inputs(tmp_path)
    with pytest.raises(ValueError, match="有效评分不足"):
        market_factors(market, "fixture", FactorConfig((FactorSpec("value", 1),)))


def test_missing_policy_changes_fingerprint_and_rejects_unsupported_values():
    assert len({config(policy).scheme for policy in ("zero", "neutral", "exclude")}) == 3
    for policy in ("", "bogus", [], 1):
        with pytest.raises(ValueError, match="missing_policy"):
            FactorSpec("value", 1, missing_policy=policy)
    with pytest.raises(ValueError, match="missing_policy"):
        FactorSpec("momentum", 1, 2, missing_policy="zero")


def test_ttm_uses_ytd_plus_prior_annual_minus_prior_ytd_after_announcement(tmp_path):
    market = frame(3, 110)
    codes = [from_qlib(code) for code in market.instrument.unique()]
    reports = [
        income(code, period=period, announce=announce, n_income_attr_p=profit)
        for code in codes
        for period, announce, profit in [
            ("20230331", "20230420", 3.0),
            ("20231231", "20240320", 12.0),
            ("20240331", "20240422", -2.0),
        ]
    ]
    root = tmp_path / "financials"
    version = publish(root, reports, seen_on="2024-04-22")
    financials = load_fundamentals(FinancialBinding(root, version["id"]), codes)
    result = market_factors(market, "fixture", config(), financials=financials)
    for day, expected in [("2024-04-22", 12.0), ("2024-04-23", 7.0)]:
        rows = [row for row in result["history"] if row["date"] == day]
        assert len(rows) == 3
        assert all(row["factor_details"]["value"]["ttm_net_profit"] == expected for row in rows)


def test_industry_processing_uses_only_observed_values_before_filling_scores(tmp_path):
    market, financials, _, _ = inputs(tmp_path)
    industry = history(market.instrument.unique())
    result = market_factors(
        market,
        "fixture",
        config("neutral"),
        financials=financials,
        neutralization=NeutralizationConfig("industry"),
        industry_history=industry,
    )
    rows = [row for row in result["history"] if row["date"] == "2024-01-08"]
    observed = [row for row in rows if row["raw"]["value"] is not None]
    values = pd.Series({row["instrument"]: row["raw"]["value"] for row in observed})
    groups = pd.Series({row["instrument"]: row["industry_code"] for row in observed})
    expected = residualize(values, groups)
    for row in observed:
        assert row["processed"]["value"] == pytest.approx(expected[row["instrument"]])
    filled = next(row for row in rows if row["raw"]["value"] is None)
    assert filled["processed"]["value"] is None and filled["scores"]["value"] == 50
    json.dumps(result, allow_nan=False)


def test_legacy_pe_evaluations_remain_readable_but_cannot_silently_seed_new_ttm_strategies():
    evaluation = {
        "metadata": {
            "factor_config": [{"name": "value", "weight": 1}],
            "factor_definitions": {"value": {"version": "causal-daily-v1"}},
        }
    }
    original = json.dumps(evaluation, sort_keys=True)
    with pytest.raises(ValueError, match="旧PE口径"):
        factor_configs(evaluation, {"selected_keys": ["value"]})
    assert json.dumps(evaluation, sort_keys=True) == original


def test_streamed_exclusions_keep_input_details_and_do_not_claim_unpublished_imputations(tmp_path):
    market, financials, _, _ = inputs(tmp_path)
    market = market.loc[market.date.ge("2024-01-04")].copy()
    market["pb"] = 1.0
    missing_stock = market.instrument.unique()[-1]
    market.loc[market.instrument.eq(missing_stock) & market.date.eq("2024-01-15"), "pb"] = float("nan")
    early = financials.events.copy()
    early["ttm_net_profit"] = 0.0
    early.loc[early.instrument.eq(missing_stock), "ttm_net_profit"] = float("nan")
    later = financials.events.copy()
    later["financial_available_at"] = pd.Timestamp("2024-01-10T01:30:00Z")
    financials = replace(financials, events=pd.concat([early, later], ignore_index=True))
    settings = FactorConfig((FactorSpec("value", 0.5), FactorSpec("book_to_price", 0.5)))
    store = ResultStore(tmp_path / "runs")
    with StreamingTables(store, "factors", "streamed-value") as tables:
        result = market_factors(
            market,
            "fixture",
            settings,
            financials=financials,
            neutralization=NeutralizationConfig("industry"),
            industry_history=history(market.instrument.unique()),
            row_sink=tables.append,
        )
        result["tables"] = tables.finish()
    result["metadata"]["id"] = "streamed-value"
    store.publish("factors", "streamed-value", result)
    archived = store.get("factors", "streamed-value", hydrate=False)
    early_rows, _ = read_rows(
        store, "factors", "streamed-value", archived, "diagnostics", filters={"date": "2024-01-04"}
    )
    assert len(early_rows) == 4
    assert all("value" in row["factor_details"] for row in early_rows)
    assert result["metadata"]["availability_by_date"]["2024-01-04"]["factors"]["value"]["imputed"] == 0
    later_rows, _ = read_rows(
        store,
        "factors",
        "streamed-value",
        archived,
        "diagnostics",
        filters={"date": "2024-01-15", "instrument": missing_stock},
    )
    assert len(later_rows) == 1 and later_rows[0]["factor_details"]["value"]["reason"] == "missing_or_stale_ttm_profit"
