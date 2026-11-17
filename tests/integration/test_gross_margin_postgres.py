"""Real isolated PostgreSQL task/result round-trip for gross-margin policies."""

import copy
import os

import pytest
from fastapi.testclient import TestClient
from test_gross_margin_workflow import seed_financials
from test_postgres_runtime import engine as engine

from finance_quant.api import create_app
from finance_quant.application.task_worker import run_task
from finance_quant.application.tasks import TaskService
from finance_quant.persistence.market import MarketRepository
from finance_quant.persistence.results import PgResultStore
from finance_quant.persistence.tasks import PgTaskStore
from finance_quant.research.report import evaluation_markdown

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL"
)


def test_gross_policies_and_details_survive_postgres_task_runtime(engine, tmp_path, workspace):
    data, runs, source = workspace
    document = copy.deepcopy(source)
    codes = ["600000.SH", "000001.SZ", "600519.SH", "600036.SH"]
    document["data"]["codes"] = codes
    document["backtest"].update(start="2025-01-02", end="2025-01-17", topk=2, n_drop=1)
    document["factors"] = [
        {"name": "momentum", "weight": 0.5, "window": 2},
        {"name": "gross_margin_ttm", "weight": 0.5},
    ]
    seed_financials(data, codes)
    queue = PgTaskStore(runs, engine=engine)
    service = TaskService(runs, data, task_store=queue)
    store = PgResultStore(runs, engine=engine, storage_root=tmp_path)
    client = TestClient(create_app(result_store=store, task_service=service), base_url="http://127.0.0.1")
    saved = {}
    for index, policy in enumerate(("zero", "neutral", "exclude", "neutral")):
        document["factors"][1]["missing_policy"] = policy
        response = client.post(
            "/api/v1/research/tasks", json=document, headers={"Idempotency-Key": f"pg-gross-{index}"}
        )
        assert response.status_code == 202, response.text
        identifier = response.json()["id"]
        assert queue.claim() == identifier
        run_task(
            runs,
            data,
            identifier,
            task_store=queue,
            result_store_factory=lambda dataset_id: PgResultStore(
                runs, engine=engine, storage_root=tmp_path, dataset_id=dataset_id
            ),
            market_repository=MarketRepository(engine),
            storage_root=tmp_path,
        )
        task = queue.get(identifier)
        assert task["status"] == "succeeded", task["error"]
        result = task["result"]
        response = client.get(f"/api/v1/factor-runs/{result['factor_run_id']}/scores?date=2025-01-08")
        assert response.status_code == 200
        scores = response.json()
        assert scores["total"] == (3 if policy == "exclude" else 4)
        bank = next(row for row in scores["items"] if row["instrument"] == "SH600000")
        assert bank["scores"]["gross_margin_ttm"] == 50
        assert bank["factor_details"]["gross_margin_ttm"]["components"][0]["company_type"] == "2"
        assert scores["metadata"]["coverage"]["factors"]["gross_margin_ttm"]["not_applicable"] == 1
        for category, key in (("backtests", "backtest_run_id"), ("evaluations", "evaluation_run_id")):
            run_id = result[key]
            assert client.get(f"/api/v1/{category}/{run_id}").status_code == 200
            saved[category, run_id] = store.digest(category, run_id)
        evaluation_id = result["evaluation_run_id"]
        archived = store.get("evaluations", evaluation_id, hydrate=False)
        report = store.path("evaluations", evaluation_id).with_name("report.md").read_text(encoding="utf-8")
        assert report == evaluation_markdown(archived)
        if index == 3:
            assert result["backtest_reused"] and result["evaluation_reused"]
    assert all(store.digest(category, identifier) == digest for (category, identifier), digest in saved.items())
