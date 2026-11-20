import copy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from finance_quant.checksums import file_sha256
from finance_quant.data.temporal_audit import factor_temporal_audit
from finance_quant.research.factor_library import describe_spec, requires_financials
from finance_quant.research.registry import FactorConfig, FactorSpec
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
    metadata = {"factor_definitions": definitions}
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
    assert requires_financials(FactorConfig((spec,))) is False  # Running is allowed without statements.
    definition = describe_spec(spec)
    assert definition["requires_financials"] is True
    audit = factor_temporal_audit({"factor_definitions": {"value": definition}})
    assert financial_item(audit)["status"] == "missing_binding"


@pytest.mark.parametrize("definitions,financial,status", CASES)
def test_legacy_api_corrects_only_audit_and_keeps_frozen_metrics_and_files(tmp_path, definitions, financial, status):
    result = {
        "metadata": {
            "id": "legacy-evaluation",
            "name": "全市场旧评估",
            "created_at": "2026-11-12T00:00:00Z",
            "is_synthetic": True,
            "factor_definitions": definitions,
            "temporal_audit": {
                "version": "temporal-execution-audit-v1",
                "items": [
                    {"key": "signal", "status": "guarded", "detail": "keep this"},
                    {"key": "financials", "status": "not_used", "detail": "本次入选因子不使用财务报表。"},
                ],
            },
        },
        "factors": {
            "value": {
                "horizons": {
                    str(horizon): {
                        "daily": [{"date": "2025-03-03", "observations": 0, "ic": None, "group_returns": [None] * 3}]
                    }
                    for horizon in [1, 5, 10]
                }
            }
        },
    }
    if financial is not None:
        result["metadata"]["financial_data"] = financial
    frozen = copy.deepcopy(result)
    store = ResultStore(tmp_path)
    document = store.publish("evaluations", "legacy-evaluation", result)
    report = store.publish_report("legacy-evaluation", "Frozen original report\n")
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
    assert client.get("/api/v1/evaluations/legacy-evaluation").json() == corrected
    assert store.get("evaluations", "legacy-evaluation") == frozen
    assert {path: file_sha256(path) for path in before} == before
