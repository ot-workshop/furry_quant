"""Matching revenue/cost, explicit financial applicability and causal score fills."""

import copy
import json

import pandas as pd
import pytest
from test_factor_library import frame
from test_industry_neutralization import history

from finance_quant.application import research_requests
from finance_quant.application.selection_context import factor_configs
from finance_quant.data.financials import FinancialBinding
from finance_quant.data.industries import publish_industries
from finance_quant.data.symbols import from_qlib
from finance_quant.research.evaluation import evaluate_factors
from finance_quant.research.fundamentals import load_fundamentals
from finance_quant.research.market_factors import market_factors
from finance_quant.research.neutralization import NeutralizationConfig
from finance_quant.research.registry import EvaluationConfig, FactorConfig, FactorSpec
from finance_quant.storage import ResultStore
from finance_quant.storage_tables import StreamingTables, read_rows
from support.financial import income, publish


def config(policy="zero"):
    return FactorConfig((FactorSpec("gross_margin_ttm", 0.5, missing_policy=policy), FactorSpec("momentum", 0.5, 2)))


def inputs(tmp_path):
    market = frame(6, 20)
    market["open"] = market.close
    codes = [from_qlib(code) for code in market.instrument.unique()]
    # Revenue differs from total revenue; zero cost is observed, None is absent.
    variants = [
        {"revenue": 100, "total_revenue": 200, "oper_cost": 60},
        {"comp_type": "2", "oper_cost": None},
        {"oper_cost": None},
        {"oper_cost": 100},
        {"oper_cost": 120},
        {"oper_cost": 0},
    ]
    reports = [
        income(code, period="20231231", announce="20240103", **values)
        for code, values in zip(codes, variants, strict=True)
    ]
    root = tmp_path / "financials"
    version = publish(root, reports, seen_on="2024-01-03")
    return market, load_fundamentals(FinancialBinding(root, version["id"]), codes), reports, codes


@pytest.mark.parametrize("policy,fill", [("zero", 0), ("neutral", 50), ("exclude", None)])
def test_applicability_missing_signed_and_zero_are_separate(tmp_path, policy, fill):
    market, financials, _, _ = inputs(tmp_path)
    result = market_factors(market, "fixture", config(policy), financials=financials)
    symbols = market.instrument.unique()
    rows = {row["instrument"]: row for row in result["history"] if row["date"] == "2024-01-08"}
    assert len(rows) == (5 if fill is None else 6)
    for index, raw in ((0, 0.4), (3, 0), (4, -0.2), (5, 1)):
        assert rows[symbols[index]]["raw"]["gross_margin_ttm"] == pytest.approx(raw)
        assert not rows[symbols[index]]["factor_details"]["gross_margin_ttm"]["imputed"]
    bank = rows[symbols[1]]
    assert bank["raw"]["gross_margin_ttm"] is None
    assert bank["scores"]["gross_margin_ttm"] == 50
    assert bank["factor_details"]["gross_margin_ttm"]["status"] == "not_applicable"
    if fill is not None:
        missing = rows[symbols[2]]
        assert missing["raw"]["gross_margin_ttm"] is None
        assert missing["scores"]["gross_margin_ttm"] == fill
        assert missing["contributions"]["gross_margin_ttm"] == fill * 0.5
        assert missing["factor_details"]["gross_margin_ttm"]["reason"] == "missing_operating_cost"
    counts = result["metadata"]["availability_by_date"]["2024-01-08"]["factors"]["gross_margin_ttm"]
    assert (counts["valid"], counts["not_applicable"], counts["missing"]) == (4, 1, 1)
    assert counts["not_applicable_scored"] == 1
    assert counts["imputed"] == (0 if fill is None else 1)
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("company_type", ["2", "3", "4", "7"])
def test_financial_statement_types_never_invent_gross_margin(tmp_path, company_type):
    market, _, _, codes = inputs(tmp_path)
    root = tmp_path / "specialized"
    manifest = publish(
        root,
        [income(code, period="20231231", announce="20240103", comp_type=company_type) for code in codes],
        seen_on="2024-01-03",
    )
    result = market_factors(
        market, "test", config("exclude"), financials=load_fundamentals(FinancialBinding(root, manifest["id"]), codes)
    )
    assert all(
        row["raw"]["gross_margin_ttm"] is None and row["scores"]["gross_margin_ttm"] == 50 for row in result["history"]
    )
    # Imputed N/A is never enough to run a portfolio alone.
    with pytest.raises(ValueError, match="有效评分不足"):
        market_factors(
            market,
            "test",
            FactorConfig((FactorSpec("gross_margin_ttm", 1),)),
            financials=load_fundamentals(FinancialBinding(root, manifest["id"]), codes),
        )


def test_unknown_type_and_missing_revenue_are_not_industrial_fallbacks(tmp_path):
    market, _, _, codes = inputs(tmp_path)
    variants = [
        {"comp_type": None},
        {"revenue": None},
        {"revenue": 0},
        {"revenue": -10},
        {"oper_cost": None},
        {"comp_type": "1"},
    ]
    root = tmp_path / "unknown"
    manifest = publish(
        root,
        [
            income(code, period="20231231", announce="20240103", **values)
            for code, values in zip(codes, variants, strict=True)
        ],
        seen_on="2024-01-03",
    )
    result = market_factors(
        market, "test", config(), financials=load_fundamentals(FinancialBinding(root, manifest["id"]), codes)
    )
    rows = {row["instrument"]: row for row in result["history"] if row["date"] == "2024-01-08"}
    reasons = [
        "unknown_statement_type",
        "missing_revenue",
        "nonpositive_revenue",
        "nonpositive_revenue",
        "missing_operating_cost",
    ]
    for symbol, reason in zip(market.instrument.unique()[:5], reasons, strict=True):
        assert rows[symbol]["factor_details"]["gross_margin_ttm"]["reason"] == reason
        assert rows[symbol]["scores"]["gross_margin_ttm"] == 0


def test_quarterly_ttm_uses_matching_statements_and_visible_announcement(tmp_path):
    market = frame(3, 30)
    codes = [from_qlib(code) for code in market.instrument.unique()]
    reports = []
    for index, code in enumerate(codes):
        for period, revenue, cost, announced in (
            ("20220930", 60, 40, "20221025"),
            ("20221231", 90, 60, "20230320"),
            ("20230930", 80, 50, "20240105"),
        ):
            reports.append(
                income(
                    code,
                    period=period,
                    announce=announced,
                    revenue=revenue,
                    total_revenue=revenue * 3,
                    oper_cost=cost,
                    comp_type="2" if index == 1 and period == "20220930" else "1",
                )
            )
    root = tmp_path / "ttm"
    manifest = publish(root, reports, seen_on="2024-01-05")
    financials = load_fundamentals(FinancialBinding(root, manifest["id"]), codes)
    result = market_factors(market, "same", config(), financials=financials)
    first, mixed = market.instrument.unique()[:2]
    before = next(row for row in result["history"] if row["instrument"] == first and row["date"] == "2024-01-05")
    after = next(row for row in result["history"] if row["instrument"] == first and row["date"] == "2024-01-08")
    assert before["raw"]["gross_margin_ttm"] == pytest.approx(1 / 3)
    assert after["raw"]["gross_margin_ttm"] == pytest.approx((110 - 70) / 110)
    detail = after["factor_details"]["gross_margin_ttm"]
    assert detail["revenue_ttm"] == 110 and detail["operating_cost_ttm"] == 70
    assert [piece["coefficient"] for piece in detail["components"]] == [1, 1, -1]
    assert all(
        pd.Timestamp(piece["available_at"]) <= pd.Timestamp("2024-01-08", tz="Asia/Shanghai") + pd.Timedelta(hours=18)
        for piece in detail["components"]
    )
    invalid = next(row for row in result["history"] if row["instrument"] == mixed and row["date"] == "2024-01-08")
    assert invalid["factor_details"]["gross_margin_ttm"]["reason"] == "mixed_statement_types"
    later = publish(
        tmp_path / "future",
        [*reports, *[income(code, period="20231231", announce="20240125", comp_type="2") for code in codes]],
        seen_on="2024-01-25",
    )
    updated = market_factors(
        market,
        "same",
        config(),
        financials=load_fundamentals(FinancialBinding(tmp_path / "future", later["id"]), codes),
    )
    assert [row for row in result["history"] if row["date"] < "2024-01-25"] == [
        row for row in updated["history"] if row["date"] < "2024-01-25"
    ]


def test_historical_industry_only_resolves_unknown_at_signal_date(tmp_path):
    market, _, _, _ = inputs(tmp_path)
    h = history(market.instrument.unique())
    h.frame["industry_code"] = "480000"
    h.frame["industry_name"] = "银行"
    h.frame["in_date"] = pd.Timestamp("2024-01-10")
    before = market_factors(market, "same", config(), industry_history=h)
    assert all(row["scores"]["gross_margin_ttm"] == 0 for row in before["history"])
    pinned = market_factors(
        market,
        "same",
        config(),
        industry_history=h,
        neutralization=NeutralizationConfig(industry_version=h.manifest["id"]),
    )
    assert all(
        row["scores"]["gross_margin_ttm"] == (50 if row["date"] >= "2024-01-11" else 0) for row in pinned["history"]
    )
    assert all(
        row["factor_details"]["gross_margin_ttm"]["reason"] == "no_report"
        for row in pinned["history"]
        if row["date"] <= "2024-01-10"
    )


def test_industry_version_is_pinned_when_research_is_submitted(workspace, monkeypatch):
    data, runs, source = workspace
    document = copy.deepcopy(source)
    document.pop("neutralization", None)
    document["factors"] = [
        {"name": "momentum", "weight": 0.5, "window": 2},
        {"name": "gross_margin_ttm", "weight": 0.5},
    ]
    industry_root = data / "industry-fixture"
    monkeypatch.setenv("FINANCE_QUANT_INDUSTRY_ROOT", str(industry_root))
    h = publish_industries(history(["SH600000", "SZ000001", "SH600519"]).frame, root=industry_root)
    normalized, _ = research_requests.validate_request(document, data, runs)
    assert normalized["neutralization"] == {"mode": "off", "industry_version": h.manifest["id"]}
    monkeypatch.setattr(research_requests, "industry_context", lambda config: None)
    without_history, _ = research_requests.validate_request(document, data, runs)
    assert "neutralization" not in without_history


def test_secondary_industry_only_cannot_be_used_as_a_financial_l1(tmp_path):
    market, _, _, _ = inputs(tmp_path)
    h = history(market.instrument.unique())
    h.frame["level"] = "L2"
    h.frame["industry_code"] = "480000"
    result = market_factors(
        market,
        "same",
        config(),
        industry_history=h,
        neutralization=NeutralizationConfig(industry_version=h.manifest["id"]),
    )
    assert all(row["scores"]["gross_margin_ttm"] == 0 for row in result["history"])


def test_no_filled_values_in_ic_correlations_or_industry_residuals(tmp_path):
    market, financials, _, _ = inputs(tmp_path)
    outputs = []
    for policy in ("zero", "neutral"):
        factors = market_factors(market, "same", config(policy), financials=financials)
        result = evaluate_factors(
            market,
            factors,
            sorted(market.date.unique()),
            start="2024-01-08",
            end="2024-01-26",
            config=EvaluationConfig((1,), 3, 3),
        )
        assert all(
            row["observations"] == 4
            for row in result["factors"]["gross_margin_ttm"]["horizons"]["1"]["daily"]
            if row["ic"] is not None
        )
        outputs.append(result)
    assert outputs[0]["correlations"] == outputs[1]["correlations"]
    h = history(market.instrument.unique())
    neutral = market_factors(
        market,
        "same",
        config(),
        financials=financials,
        industry_history=h,
        neutralization=NeutralizationConfig("industry", h.manifest["id"]),
    )
    missing = market.instrument.unique()[2]
    bank = market.instrument.unique()[1]
    for row in neutral["history"]:
        if row["instrument"] in {missing, bank}:
            assert row["processed"]["gross_margin_ttm"] is None
            assert row["scores"]["gross_margin_ttm"] == (50 if row["instrument"] == bank else 0)


def test_nested_details_survive_streaming_storage(tmp_path):
    market, financials, _, _ = inputs(tmp_path)
    store = ResultStore(tmp_path / "runs")
    with StreamingTables(store, "factors", "gross") as tables:
        result = market_factors(market, "same", config(), financials=financials, row_sink=tables.append)
        result["tables"] = tables.finish()
    result["metadata"]["id"] = "gross"
    store.publish("factors", "gross", result)
    rows, _ = read_rows(store, "factors", "gross", store.get("factors", "gross", hydrate=False), "history")
    bank = next(row for row in rows if row["instrument"] == market.instrument.unique()[1])
    assert bank["factor_details"]["gross_margin_ttm"]["status"] == "not_applicable"
    assert bank["factor_details"]["gross_margin_ttm"]["components"][0]["company_type"] == "2"


def test_old_gross_formula_is_not_silently_reused_but_value_v4_stays_valid():
    with pytest.raises(ValueError, match="毛利率"):
        factor_configs(
            {
                "metadata": {
                    "factor_config": [{"name": "gross_margin_ttm", "weight": 1}],
                    "factor_definitions": {"gross_margin_ttm": {"version": "fundamental-ttm-v4"}},
                }
            },
            {"selected_keys": ["gross_margin_ttm"]},
        )
    baseline, selected = factor_configs(
        {
            "metadata": {
                "factor_config": [{"name": "value", "weight": 1}],
                "factor_definitions": {"value": {"version": "fundamental-ttm-v4"}},
            }
        },
        {"selected_keys": ["value"]},
    )
    assert baseline.as_list() == selected.as_list()
