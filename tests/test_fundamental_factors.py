"""Fundamental factors through the queued snapshot workflow: pinning, scoring and provenance."""

import pytest
from test_pool_workflow_matrix import codes_for, request_for, run_request

from finance_quant.data.financials import financial_root
from finance_quant.storage import ResultStore
from support.financial import balance, cashflow, income, publish

FUNDAMENTAL = ("roe_ttm", "roa_ttm", "gross_margin_ttm", "asset_turnover_ttm", "debt_to_assets")


def statements(codes):
    parts = []
    for index, code in enumerate(codes):
        for period, announce in (
            ("20230930", "20231025"),
            ("20231231", "20240320"),
            ("20240630", "20240820"),
            ("20240930", "20241025"),
        ):
            scale = 1.0 + index * 0.1
            parts += [
                income(
                    ts_code=code,
                    period=period,
                    announce=announce,
                    total_revenue=400.0 * scale,
                    oper_cost=220.0 * scale,
                    n_income_attr_p=40.0 * scale,
                ),
                balance(
                    ts_code=code,
                    period=period,
                    announce=announce,
                    total_assets=900.0 * scale,
                    total_liab=300.0 * scale,
                    total_hldr_eqy_exc_min_int=600.0 * scale,
                ),
                cashflow(ts_code=code, period=period, announce=announce, n_cashflow_act=30.0 * scale),
            ]
    return parts


def test_fundamental_factor_runs_through_the_queued_workflow(tmp_path):
    codes = codes_for(3, 71)
    manifest = publish(financial_root(tmp_path / "data"), statements(codes))
    request = request_for(codes)
    request["factors"] = [{"name": "roe_ttm", "weight": 1.0}]
    request["financial_policy"] = "announcement_estimate"
    task = run_request(request, tmp_path / "data", tmp_path / "runs")
    assert task["status"] == "succeeded", task["error"]

    store = ResultStore(tmp_path / "runs")
    factors = store.get("factors", task["result"]["factor_run_id"])
    assert factors["metadata"]["financial_data"]["version"] == manifest["id"]
    assert set(factors["history"][-1]["raw"]) == {"roe_ttm"}
    assert factors["metadata"]["provenance"]["financial_version"] == manifest["id"]
    backtest = store.get("backtests", task["result"]["backtest_run_id"])
    assert backtest["metadata"]["provenance"]["financial_version"] == manifest["id"]
    assert any("财报因子" in text for text in backtest["metadata"]["limitations"])
    evaluation = store.get("evaluations", task["result"]["evaluation_run_id"])
    assert evaluation["metadata"]["financial_data"]["version"] == manifest["id"]
    assert any(
        item["key"] == "financials" and item["status"] == "unverified"
        for item in evaluation["metadata"]["temporal_audit"]["items"]
    )


def test_price_only_runs_keep_their_old_fingerprint(tmp_path):
    codes = codes_for(3, 71)
    request = request_for(codes)
    task = run_request(request, tmp_path / "data", tmp_path / "runs")
    assert task["status"] == "succeeded", task["error"]
    store = ResultStore(tmp_path / "runs")
    factors = store.get("factors", task["result"]["factor_run_id"])
    assert "financial_version" not in factors["metadata"]["provenance"]


def test_request_without_a_financial_version_is_refused(tmp_path):
    codes = codes_for(3, 71)
    request = request_for(codes)
    request["factors"] = [{"name": "roe_ttm", "weight": 1.0}]
    with pytest.raises(ValueError, match="财务数据"):
        run_request(request, tmp_path / "data", tmp_path / "runs")


def test_insufficient_financial_coverage_fails_before_scoring(tmp_path):
    codes = codes_for(3, 71)
    publish(financial_root(tmp_path / "data"), statements(codes[:1]))
    request = request_for(codes)
    request["factors"] = [{"name": "roe_ttm", "weight": 1.0}]
    with pytest.raises(ValueError, match="覆盖|不足以"):
        run_request(request, tmp_path / "data", tmp_path / "runs")
