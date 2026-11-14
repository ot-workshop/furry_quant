"""Factor hint coverage follows the chosen source, parameters and historical visibility."""

import copy
import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from test_dynamic_experiments import pool, refresh

from finance_quant.application.factor_coverage import factor_coverage
from finance_quant.data.financials import financial_root
from finance_quant.data.market_universe import market_option
from support.fakes import make_app
from support.financial import income, publish


def request(tmp_path, mode="index_warehouse", size=3):
    data, root, document = pool(tmp_path, mode, size)
    return data, root, {"data": document["data"], "start": "2024-01-08", "factor": {"name": "value"}}


@pytest.mark.parametrize("mode", ["index_warehouse", "market_warehouse"])
def test_dynamic_coverage_uses_actual_membership_and_is_read_only(tmp_path, mode):
    data, root, document = request(tmp_path, mode)
    before = {p: p.read_bytes() for p in data.rglob("*") if p.is_file()}
    result = factor_coverage(document, data)
    assert result["as_of"] == "2024-01-05"
    assert result["total"] == (2 if mode == "index_warehouse" else 3)
    assert result["available"] == result["total"] and result["unavailable"] == 0
    assert any("非正PE" in note for note in result["notes"])
    assert all(p.read_bytes() == value for p, value in before.items())
    assert set(before) == {p for p in data.rglob("*") if p.is_file()}
    assert not (tmp_path / "runs").exists()
    if mode == "index_warehouse":
        document["start"] = "2024-01-19"
        assert factor_coverage(document, data)["available"] == 2  # membership changed; no current-pool backfill


def test_snapshot_coverage_respects_custom_stock_pool_and_windows(workspace):
    data, _, research = workspace
    document = {
        "data": research["data"],
        "start": research["backtest"]["start"],
        "factor": {"name": "momentum", "window": 5},
    }
    result = factor_coverage(document, data)
    assert result["total"] == result["available"] == 3
    document["data"]["codes"] = document["data"]["codes"][:2]
    assert factor_coverage(document, data)["total"] == 2
    document["factor"]["window"] = 126
    longer = factor_coverage(document, data)
    assert longer["available"] == 0 and longer["unavailable"] == 2
    assert longer["reasons"][0]["code"] == "history"


def test_missing_financial_version_is_counted_without_downloading(tmp_path):
    data, _, document = request(tmp_path)
    document["factor"] = {"name": "gross_margin_ttm"}
    result = factor_coverage(document, data)
    assert result["unavailable"] == result["total"] == 2
    assert result["reasons"] == [{"code": "no_financials", "label": "尚无财务数据版本", "count": 2}]
    assert not financial_root(data).exists()
    assert any("金融行业" in note for note in result["notes"])


def test_gross_margin_missing_field_differs_from_missing_report_and_future_cannot_fill_it(tmp_path):
    data, _, document = request(tmp_path)
    root = financial_root(data)
    manifest = publish(
        root,
        [
            income(period="20231231", announce="20240103", oper_cost=None),
            income(ts_code="000001.SZ", period="20231231", announce="20240103"),
        ],
        seen_on="2024-01-03",
    )
    document.update(factor={"name": "gross_margin_ttm"}, financial_version=manifest["id"])
    result = factor_coverage(document, data)
    assert result["available"] == 1 and result["unavailable"] == 1
    assert result["reasons"][0]["code"] == "financial_inputs"
    assert result["examples"][0]["code"] == "600000.SH"
    # A later report with complete costs must not fix the earlier signal date.
    future = publish(
        root,
        [
            income(period="20231231", announce="20240103", oper_cost=None),
            income(ts_code="000001.SZ", period="20231231", announce="20240103"),
            income(period="20241231", announce="20250320", oper_cost=40),
        ],
        seen_on="2025-03-20",
    )
    document["financial_version"] = future["id"]
    after = factor_coverage(document, data)
    assert (after["available"], after["unavailable"], after["reasons"]) == (
        result["available"],
        result["unavailable"],
        result["reasons"],
    )
    document["financial_version"] = manifest["id"]
    assert factor_coverage(document, data) == result


def test_filters_custom_codes_and_future_prices_change_only_relevant_coverage(tmp_path):
    data, root, document = request(tmp_path, "market_warehouse")
    document["data"]["codes"] = ["600000.SH", "000001.SZ"]
    before = factor_coverage(document, data)
    assert before["available"] == 2
    manifest = json.loads((root / "manifest.json").read_bytes())
    path = root / manifest["assets"]["600000.SH"]["file"]
    prices = pd.read_parquet(path)
    prices.loc[prices.date.gt("2024-01-05"), ["pe_ttm", "close"]] = -999
    prices.to_parquet(path, index=False)
    refresh(root, manifest)
    document["data"]["warehouse_id"] = market_option(root)["id"]
    result = factor_coverage(document, data)
    assert result["available"] == before["available"]
    document["data"]["filters"] = {"boards": ["star"], "exclude_st": False, "min_listing_days": 0}
    assert factor_coverage(document, data)["total"] == 0


def test_api_returns_actual_counts_rejects_bad_parameters_and_creates_no_tasks(tmp_path):
    data, _, document = request(tmp_path)
    client = TestClient(make_app(tmp_path / "runs", data), base_url="http://127.0.0.1")
    response = client.post("/api/v1/research/factor-coverage", json=document)
    assert response.status_code == 200, response.text
    assert response.json()["available"] == 2
    for patch in (
        {"start": "2024-01-06"},
        {"factor": {"name": "momentum", "window": 1}},
        {"data": {"mode": "snapshot", "snapshot_id": "../../dev", "codes": ["600000.SH", "000001.SZ"]}},
    ):
        bad = copy.deepcopy(document) | patch
        response = client.post("/api/v1/research/factor-coverage", json=bad)
        assert response.status_code == 422, response.text
    assert client.get("/api/v1/research/tasks").json()["total"] == 0
    assert not (tmp_path / "runs").exists()
