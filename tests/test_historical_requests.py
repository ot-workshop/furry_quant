import json

import pytest
from fastapi.testclient import TestClient
from test_historical_workflow import request, warehouse_fixture

from finance_quant.application import market_download
from finance_quant.application.research_requests import validate_request
from finance_quant.data.historical_warehouse import warehouse_option
from support.fakes import make_app, make_service, run_claimed


def setup(tmp_path, monkeypatch):
    root = tmp_path / "warehouse"
    warehouse_fixture(root)
    monkeypatch.setattr(market_download, "warehouse_roots", lambda data_root: [root.resolve()])
    body = request()
    body["data"]["warehouse_id"] = warehouse_option(root)["id"]
    return root, body


@pytest.mark.parametrize(
    "case", ["version", "arbitrary_path", "user_codes", "outside_dates", "warmup", "non_session", "too_many_holdings"]
)
def test_historical_request_strict_bounds_and_pinned_version(tmp_path, monkeypatch, case):
    root, body = setup(tmp_path, monkeypatch)
    if case == "version":
        body["data"]["warehouse_id"] = "old"
    if case == "arbitrary_path":
        body["data"]["warehouse_dir"] = "C:/outside"
    if case == "user_codes":
        body["data"]["codes"] = ["600000.SH"]
    if case == "outside_dates":
        body["backtest"]["end"] = "2024-02-05"
    if case == "warmup":
        body["factors"][0]["window"] = 126
    if case == "non_session":
        body["backtest"]["start"] = "2024-01-07"
    if case == "too_many_holdings":
        body["backtest"]["topk"] = 301
    with pytest.raises(ValueError):
        validate_request(body, tmp_path / "data", tmp_path / "runs")


def test_csi300_version_counts_as_a_dataset_and_sets_option_defaults(tmp_path, monkeypatch):
    _, body = setup(tmp_path, monkeypatch)
    runs, data = tmp_path / "runs", tmp_path / "data"
    client = TestClient(make_app(runs, data_root=data), base_url="http://127.0.0.1")
    status = client.get("/api/v1/data-status").json()
    assert status["snapshots"] == []
    [listed] = status["historical_indices"]
    assert listed["id"] == body["data"]["warehouse_id"]
    assert "trading_dates" not in listed and listed["trading_days"] > 0
    options = client.get("/api/v1/research/options").json()
    option, backtest = options["historical_indices"][0], options["defaults"]["backtest"]
    assert option["start"] <= backtest["start"] <= backtest["end"] == option["end"]
    assert backtest["start"] in option["trading_dates"]
    assert backtest["topk"] == 3


def test_web_submit_worker_results_and_correct_result_links(tmp_path, monkeypatch):
    root, body = setup(tmp_path, monkeypatch)
    runs = tmp_path / "runs"
    data = tmp_path / "data"
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data_root=data, service=service), base_url="http://127.0.0.1")
    options = client.get("/api/v1/research/options").json()
    assert options["historical_indices"][0]["id"] == body["data"]["warehouse_id"]
    response = client.post("/api/v1/research/tasks", json=body, headers={"Idempotency-Key": "historical-valid-12345"})
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    assert service.store.claim() == identifier
    task = run_claimed(service, identifier)
    assert task["status"] == "succeeded", task
    assert task["progress"] == {"completed": 4, "total": 4}
    outputs = task["result"]
    assert outputs["factor_as_of"] == "2024-01-30"
    factor = client.get(
        "/api/v1/factor-runs/" + outputs["factor_run_id"] + "/scores", params={"date": "2024-01-11"}
    ).json()
    assert any(r["instrument"] == "SH688001" for r in factor["items"])
    backtest = client.get("/api/v1/backtests/" + outputs["backtest_run_id"]).json()
    assert (
        backtest["strategy"] == "HistoricalIndexTopkStrategy" and backtest["factor_run_id"] == outputs["factor_run_id"]
    )
    events = client.get("/api/v1/backtests/" + outputs["backtest_run_id"] + "/execution-events").json()
    assert any(e["reason"] == "index_exit" for e in events["items"])
    evaluation = client.get("/api/v1/evaluations/" + outputs["evaluation_run_id"]).json()
    assert evaluation["metadata"]["factor_run_id"] == outputs["factor_run_id"]
    again = client.post("/api/v1/research/tasks", json=body, headers={"Idempotency-Key": "historical-valid-12345"})
    assert again.status_code == 202 and again.json()["id"] == identifier


def test_worker_refuses_changed_warehouse_and_does_not_publish_results(tmp_path, monkeypatch):
    root, body = setup(tmp_path, monkeypatch)
    runs = tmp_path / "runs"
    data = tmp_path / "data"
    service = make_service(runs, data)
    task = service.submit(body, "historical-pinned-12345")
    service.store.claim()
    path = root / "manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["revision"] = "changed"
    path.write_text(json.dumps(document), encoding="utf-8")
    failed = run_claimed(service, task["id"])
    assert failed["status"] == "failed" and "版本已变化" in failed["error"]
    assert failed["result"] is None and not (runs / "backtests").exists()
