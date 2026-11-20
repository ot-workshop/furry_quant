import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finance_quant.checksums import file_sha256
from finance_quant.data.temporal_audit import factor_temporal_audit
from finance_quant.research.factor_library import describe_spec, requires_financials
from finance_quant.research.registry import FactorConfig, FactorSpec
from finance_quant.research.report import evaluation_markdown
from finance_quant.storage import ResultStore
from finance_quant.web.evaluations import evaluation_router

CASES = [
    ({"value": {"requires_financials": True}}, None, "missing_binding"),
    ({"momentum": {"requires_financials": False}}, None, "not_used"),
    ({"value": {"requires_financials": True}}, {"version": "financial-frozen"}, "unverified"),
    ({}, None, "unknown"),
    ({"legacy": {}}, None, "unknown"),
]


def financial_item(audit):
    return next(item for item in audit["items"] if item["key"] == "financials")


@pytest.mark.parametrize("definitions,financial,status", CASES)
def test_new_audit_distinguishes_dependency_from_binding(definitions, financial, status):
    metadata = {"factor_definitions": definitions, "factor_config": [{"name": key} for key in definitions]}
    if financial is not None:
        metadata["financial_data"] = financial
    item = financial_item(factor_temporal_audit(metadata))
    assert item["status"] == status
    if status == "missing_binding":
        assert "需要财务" in item["detail"]
        assert "重新评估" in item["detail"]
    if financial:
        assert financial["version"] in item["detail"]


def test_value_zero_policy_still_declares_its_real_financial_dependency():
    spec = FactorSpec("value", 1.0, missing_policy="zero")
    config = FactorConfig((spec,))
    assert requires_financials(config) is False  # Running is allowed without statements.
    definition = describe_spec(spec)
    assert definition["requires_financials"] is True
    audit = factor_temporal_audit({"factor_definitions": {"value": definition}, "factor_config": config.as_list()})
    assert financial_item(audit)["status"] == "missing_binding"


@pytest.mark.parametrize("definitions,financial,status", CASES)
def test_legacy_api_corrects_only_audit_and_keeps_frozen_metrics_and_files(tmp_path, definitions, financial, status):
    result = {
        "metadata": {
            "id": "legacy-evaluation",
            "name": "全市场旧评估",
            "created_at": "2026-11-12T00:00:00Z",
            "is_synthetic": True,
            "evaluation": {"min_cross_section": 3, "groups": 3},
            "sample_size": 5,
            "start": "2025-03-03",
            "end": "2025-03-04",
            "data_version": "frozen-data",
            "factor_run_id": "frozen-factors",
            "factor_config": [{"name": key, "weight": 1.0, "window": None} for key in definitions],
            "factor_definitions": definitions,
            "temporal_audit": {
                "version": "temporal-execution-audit-v1",
                "items": [
                    {"key": "signal", "label": "信号", "status": "guarded", "detail": "keep this"},
                    {
                        "key": "financials",
                        "label": "财务报表",
                        "status": "not_used",
                        "detail": "本次入选因子不使用财务报表。",
                    },
                ],
            },
        },
        "factors": {
            "value": {
                "horizons": {
                    str(horizon): {
                        "summary": {
                            "ic": {"mean": None},
                            "rank_ic": {"valid_dates": 0, "mean": None, "ir": None},
                            "mean_high_minus_low": None,
                        },
                        "daily": [{"date": "2025-03-03", "observations": 0, "ic": None, "group_returns": [None] * 3}],
                    }
                    for horizon in [1, 5, 10]
                }
            }
        },
        "labels": {str(horizon): {"valid_observations": 0, "excluded_observations": 5} for horizon in [1, 5, 10]},
        "signal_coverage": [],
        "correlations": {"pairs": []},
        "methodology": {"limitations": ["Frozen limitations"]},
    }
    if financial is not None:
        result["metadata"]["financial_data"] = financial
    frozen = copy.deepcopy(result)
    store = ResultStore(tmp_path)
    document = store.publish("evaluations", "legacy-evaluation", result)
    frozen_report = evaluation_markdown(frozen)
    report = store.publish_report("legacy-evaluation", frozen_report)
    before = {path: file_sha256(path) for path in (document, report)}
    app = FastAPI()
    app.include_router(evaluation_router(store))
    client = TestClient(app)
    response = client.get("/api/v1/evaluations/legacy-evaluation")
    assert response.status_code == 200
    corrected = response.json()
    assert financial_item(corrected["metadata"]["temporal_audit"])["status"] == status
    assert corrected["factors"] == frozen["factors"]
    assert corrected["metadata"]["temporal_audit"]["items"][0] == frozen["metadata"]["temporal_audit"]["items"][0]
    assert client.get("/api/v1/evaluations/legacy-evaluation/download").json() == corrected
    assert client.get("/api/v1/evaluations/legacy-evaluation/report").text == frozen_report
    assert client.get("/api/v1/evaluations/legacy-evaluation").json() == corrected
    assert store.get("evaluations", "legacy-evaluation") == frozen
    assert {path: file_sha256(path) for path in before} == before


def test_legacy_nullable_policy_and_audit_rows_do_not_break_reads(tmp_path):
    result = {
        "metadata": {
            "factor_definitions": {"value": {"requires_financials": True}},
            "financial_data": {"version": "frozen-financials", "policy": None},
            "temporal_audit": {"items": [None]},
        },
        "labels": {"1": {"observations": []}},
    }
    frozen = copy.deepcopy(result)
    store = ResultStore(tmp_path)
    store.publish("evaluations", "nullable-legacy", result)
    app = FastAPI()
    app.include_router(evaluation_router(store))
    client = TestClient(app)
    for suffix in ["", "/download"]:
        response = client.get("/api/v1/evaluations/nullable-legacy" + suffix)
        assert response.status_code == 200
        item = financial_item(response.json()["metadata"]["temporal_audit"])
        assert item["status"] == "unverified"
        assert "frozen-financials" in item["detail"]
    assert client.get("/api/v1/evaluations/nullable-legacy/labels/1").status_code == 200
    assert store.get("evaluations", "nullable-legacy") == frozen


def test_missing_selected_definition_cannot_claim_no_financial_dependency():
    metadata = {
        "factor_config": [{"name": "value"}, {"name": "momentum"}],
        "factor_definitions": {"momentum": {"requires_financials": False}},
    }
    assert financial_item(factor_temporal_audit(metadata))["status"] == "unknown"


def test_request_binding_evidence_is_preserved_without_claiming_actual_coverage():
    metadata = {
        "factor_config": [{"name": "value"}],
        "factor_definitions": {"value": {"requires_financials": True}},
        "provenance": {"financial_version": "requested-financial-version"},
    }
    item = financial_item(factor_temporal_audit(metadata))
    assert item["status"] == "unknown"
    assert "requested-financial-version" in item["detail"]
    assert "实际读取及覆盖" in item["detail"]


def test_dependency_lookup_uses_selected_instance_ids_and_ignores_unselected_definitions():
    metadata = {
        "factor_config": [{"id": "custom_value", "name": "value"}],
        "factor_definitions": {
            "custom_value": {"requires_financials": True},
            "momentum": {"requires_financials": False},
        },
    }
    assert financial_item(factor_temporal_audit(metadata))["status"] == "missing_binding"
    metadata["factor_config"] = [{"name": "momentum"}]
    assert financial_item(factor_temporal_audit(metadata))["status"] == "not_used"
