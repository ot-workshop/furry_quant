import copy
import hashlib
import json
import threading
import time
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from finance_quant.api import create_app
from finance_quant.application import task_worker
from finance_quant.application.research_requests import project_snapshot, research_options, validate_request
from finance_quant.application.tasks import TaskConflict, TaskService, TaskStore
from finance_quant.configuration import BacktestConfig, DataConfig
from finance_quant.data.managed import download_dataset
from finance_quant.data.market import load_market
from finance_quant.research.registry import EvaluationConfig, FactorConfig
from finance_quant.storage import ResultStore
from finance_quant.data.validation_config import CrosscheckConfig
from test_managed_data import Provider


@pytest.fixture
def workspace(tmp_path):
    data_root, runs_root = tmp_path / "data", tmp_path / "runs"
    data = DataConfig(data_root, "20241202", "20250228", ("600000.SH", "000001.SZ", "600519.SH", "600036.SH"))
    download_dataset(data, provider=Provider())
    manifest = load_market(data_root)[3]
    request = {"name": "网页研究", "data": {"mode": "snapshot", "snapshot_id": manifest["id"], "codes": list(data.codes[:3])},
               "backtest": asdict(BacktestConfig("2025-01-02", "2025-02-28", topk=2)),
               "factors": FactorConfig().as_list(), "evaluation": EvaluationConfig(horizons=(1, 3)).as_dict()}
    return data_root, runs_root, request


class PausedService(TaskService):
    """API contract tests keep queued tasks on disk without launching a worker."""
    def start(self):
        self.store.initialize()
        return True

    def close(self):
        pass


def make_client(data_root, runs_root):
    service = PausedService(runs_root, data_root)
    return TestClient(create_app(runs_root, data_root=data_root, task_service=service), base_url="http://127.0.0.1"), service


def test_options_and_task_queries_do_not_create_missing_directories(tmp_path):
    data, runs = tmp_path / "missing-data", tmp_path / "missing-runs"
    client, _ = make_client(data, runs)
    options = client.get("/api/v1/research/options").json()
    assert options["snapshots"] == [] and options["defaults"]["data"]["mode"] == "download"
    assert client.get("/api/v1/research/tasks").json() == {"items": [], "total": 0}
    assert not data.exists() and not runs.exists()


def test_valid_request_normalizes_without_changing_input_and_rejects_unknown_paths(workspace):
    data, runs, request = workspace
    original = copy.deepcopy(request)
    normalized, execution = validate_request(request, data, runs)
    assert request == original
    assert normalized["data"]["codes"] == sorted(request["data"]["codes"])
    assert Path(execution["snapshot_dir"]).parent.name == "snapshots"
    for bad in (dict(request, runs_dir="../outside"), dict(request, data={**request["data"], "root": "../outside"})):
        with pytest.raises(ValueError, match="字段"):
            validate_request(bad, data, runs)


def test_options_publish_real_sessions_for_frontend_warmup_checks(workspace):
    data, runs, _ = workspace
    options = research_options(data, runs)
    _, _, calendar, manifest = load_market(data)
    item = next(row for row in options["snapshots"] if row["id"] == manifest["id"])
    expected = calendar.loc[calendar.is_open & calendar.date.between(manifest["start"], manifest["end"]), "date"].dt.strftime("%Y-%m-%d").tolist()
    assert item["trading_dates"] == expected and len(expected) == item["trading_days"]


def test_optional_evaluation_benchmark_preserves_legacy_request_normalization(workspace):
    data,runs,request=workspace
    legacy=copy.deepcopy(request);legacy['backtest'].pop('benchmark',None)
    normalized,_=validate_request(legacy,data,runs)
    explicit_none=copy.deepcopy(legacy);explicit_none['backtest']['benchmark']=None
    assert validate_request(explicit_none,data,runs)[0]==normalized
    assert 'benchmark' not in normalized['backtest']
    selected=copy.deepcopy(legacy);selected['backtest']['benchmark']='H00300.CSI'
    assert validate_request(selected,data,runs)[0]['backtest']['benchmark']=='H00300.CSI'
    options=research_options(data,runs)
    assert any(item['code']=='H00300.CSI' for item in options['comparison_benchmarks'])
    assert 'benchmark' not in options['defaults']['backtest']


@pytest.mark.parametrize("change", [
    lambda r: r.update(name=""),
    lambda r: r["data"].update(snapshot_id="../../outside"),
    lambda r: r["data"].update(codes=["600000.SH"]),
    lambda r: r["data"].update(codes=["SH600000", "600000.SH"]),
    lambda r: r["data"].update(codes=["600000.SH", "000858.SZ"]),
    lambda r: r.update(factors=[]),
    lambda r: r["factors"][0].update(weight=.9),
    lambda r: r["factors"][0].update(window=252),
    lambda r: r["factors"][0].update(window=True),
    lambda r: r["backtest"].update(topk=4),
    lambda r: r["backtest"].update(initial_cash=float("nan")),
    lambda r: r["backtest"].update(start="2025-01-04"),
    lambda r: r["backtest"].update(end="2025-03-03"),
    lambda r: r["backtest"].update(end="2025-01-03"),
    lambda r: r["evaluation"].update(groups=4, min_cross_section=3),
])
def test_bad_web_configs_fail_before_task_or_data_writes(workspace, change):
    data, runs, request = workspace
    change(request)
    with pytest.raises(ValueError):
        validate_request(request, data, runs)
    assert not runs.exists() and not (data / "research").exists()


def test_subset_snapshot_preserves_parent_and_recalculates_stock_universe(workspace):
    data, runs, request = workspace
    market, _, _, parent = load_market(data)
    source = Path(parent["snapshot_dir"])
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir() if p.is_file()}
    subset = project_snapshot(source, tuple(request["data"]["codes"]), data)
    selected, _, _, manifest = load_market(subset)
    assert len(selected) == len(market) * .75
    assert manifest["projection"]["parent_data_version"] == parent["id"]
    assert set(manifest["codes"]) == set(request["data"]["codes"])
    assert project_snapshot(source, tuple(reversed(request["data"]["codes"])), data) == subset
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir() if p.is_file()} == before
    assert load_market(data)[3]["id"] == parent["id"]
    assert manifest["id"] in {row["id"] for row in research_options(data, runs)["snapshots"]}


def test_joint_subset_keeps_per_stock_evidence_and_recomputes_fallback_status(tmp_path):
    from test_joint_data import Secondary
    class Primary(Provider):
        def query(self, api, **params):
            frame = super().query(api, **params)
            if api == "daily":
                frame["close"], frame["pct_chg"] = 10., 0.
            if api == "adj_factor" and params.get("ts_code") == "000001.SZ":
                frame.loc[frame.trade_date == "20250108", "adj_factor"] = .2
            return frame
    data = DataConfig(tmp_path / "data", "20241202", "20250228",
                      ("600000.SH", "000001.SZ", "600519.SH", "600036.SH"), validation=CrosscheckConfig("fallback"))
    download_dataset(data, provider=Primary(), secondary_provider=Secondary())
    parent = load_market(data.root)[3]
    assert parent["cross_validation"]["fallback_codes"] == ["000001.SZ"]
    for codes, status in ((data.codes[:3], "fallback"), (("600000.SH", "600519.SH", "600036.SH"), "verified")):
        subset = project_snapshot(Path(parent["snapshot_dir"]), codes, data.root)
        market, _, _, manifest = load_market(subset)
        assert manifest["schema_version"] == "market-v3"
        assert manifest["cross_validation"]["status"] == status
        assert {s["code"] for s in manifest["cross_validation"]["stocks"]} == set(codes)
        assert set(market.price_source) == ({"tushare", "baostock"} if status == "fallback" else {"tushare"})


def test_api_submission_is_idempotent_and_cancel_is_race_safe(workspace):
    data, runs, request = workspace
    client, service = make_client(data, runs)
    url = "/api/v1/research/tasks"
    headers = {"Idempotency-Key": "one-request-123", "Origin": "http://127.0.0.1"}
    response = client.post(url, json=request, headers=headers)
    assert response.status_code == 202
    task = response.json()
    assert task["status"] == "queued" and task["result"] is None
    assert "execution" not in task and "snapshot_dir" not in json.dumps(task)
    assert client.post(url, json=request, headers=headers).json()["id"] == task["id"]
    changed = dict(request, name="另一个研究")
    assert client.post(url, json=changed, headers=headers).status_code == 409
    assert client.get(url).json()["total"] == 1
    assert client.get(url + "/" + task["id"]).json() == task
    assert client.post(url + "/" + task["id"] + "/cancel", json={}).json()["status"] == "cancelled"
    assert client.post(url + "/" + task["id"] + "/cancel", json={}).status_code == 409
    assert client.get(url + "/missing").status_code == 404
    second = client.post(url, json=request, headers={"Idempotency-Key": "second-request-123"}).json()
    assert service.store.claim() == second["id"]
    assert client.post(url + "/" + second["id"] + "/cancel", json={}).status_code == 409


@pytest.mark.parametrize("headers,status", [
    ({"Origin": "https://attacker.example"}, 403),
    ({"Host": "attacker.example"}, 403),
    ({"Sec-Fetch-Site": "cross-site"}, 403),
    ({"Content-Type": "text/plain"}, 415),
    ({"Origin": "http://localhost:invalid"}, 403),
])
def test_foreign_or_form_submissions_cannot_start_tasks(workspace, headers, status):
    data, runs, request = workspace
    client, _ = make_client(data, runs)
    response = client.post("/api/v1/research/tasks", content=json.dumps(request),
                           headers={"Idempotency-Key": "test-origin-123", "Content-Type": "application/json", **headers})
    assert response.status_code == status and not runs.exists()


def test_duplicate_json_fields_and_missing_idempotency_are_rejected(workspace):
    data, runs, request = workspace
    client, _ = make_client(data, runs)
    assert client.post("/api/v1/research/tasks", content='{"name":"a","name":"b"}', headers={"Content-Type": "application/json"}).status_code == 422
    assert client.post("/api/v1/research/tasks", json=request).status_code == 422
    assert client.get("/api/v1/research/tasks").json()["total"] == 0


def test_recovery_preserves_queued_jobs_and_marks_old_running_interrupted(workspace):
    data, runs, request = workspace
    service = PausedService(runs, data)
    first = service.submit(request, "recovery-first-123")
    second = service.submit(request, "recovery-second-123")
    assert service.store.claim() == first["id"]
    reopened = TaskStore(runs)
    reopened.recover()
    assert reopened.get(first["id"])["status"] == "interrupted"
    assert reopened.get(second["id"])["status"] == "queued"
    reopened.finish(first["id"], "succeeded", result={"bad": "late worker"})
    assert reopened.get(first["id"])["status"] == "interrupted"


def test_native_worker_uses_subset_and_exact_artifact_links(workspace):
    data, runs, request = workspace
    service = PausedService(runs, data)
    original_pointer = (data / "manifest.json").read_bytes()
    task = service.submit(request, "native-subset-123")
    service.store.claim()
    task_worker.run_task(runs, data, task["id"])
    completed = service.store.get(task["id"])
    assert completed["status"] == "succeeded", completed["error"]
    assert completed["progress"] == {"completed": 4, "total": 4}
    store = ResultStore(runs)
    result = completed["result"]
    factors = store.get("factors", result["factor_run_id"])
    backtest = store.get("backtests", result["backtest_run_id"])
    evaluation = store.get("evaluations", result["evaluation_run_id"])
    assert factors["metadata"]["sample_size"] == 3
    assert backtest["metadata"]["data_version"] == evaluation["metadata"]["data_version"] == result["data_version"]
    assert backtest["metadata"]["factor_run_id"] == evaluation["metadata"]["factor_run_id"] == result["factor_run_id"]
    client, _ = make_client(data, runs)
    instruments = client.get("/api/v1/instruments", params={"run_id": result["factor_run_id"]}).json()
    assert {row["instrument"] for row in instruments["items"]} == {code[-2:] + code[:6] for code in request["data"]["codes"]}
    assert client.get("/api/v1/instruments", params={"run_id": "missing"}).status_code == 404
    assert backtest["equity"][0]["equity"] == 1 and backtest["trades"]
    assert not (runs / "active-research.json").exists()
    assert (data / "manifest.json").read_bytes() == original_pointer
    repeated = service.submit(request, "native-repeat-123")
    service.store.claim()
    task_worker.run_task(runs, data, repeated["id"])
    assert service.store.get(repeated["id"])["result"]["backtest_reused"]


def test_download_mode_runs_through_managed_acquisition_with_offline_provider(workspace, monkeypatch):
    data, runs, request = workspace
    request["data"] = {"mode": "download", "codes": ["600000.SH", "000001.SZ", "600519.SH"],
                       "start": "2024-12-02", "end": "2025-02-28", "benchmark": "000300.SH", "validation_mode": "off"}
    calls = []
    def acquire(config, **kwargs):
        calls.append(config)
        return download_dataset(config, provider=Provider())
    monkeypatch.setattr(task_worker, "download_dataset", acquire)
    service = PausedService(runs, data)
    task = service.submit(request, "new-download-123")
    service.store.claim()
    task_worker.run_task(runs, data, task["id"])
    completed = service.store.get(task["id"])
    assert completed["status"] == "succeeded", completed["error"]
    assert completed["progress"]["total"] == 5 and len(calls) == 1
    assert calls[0].root.is_relative_to(data / "research/downloads")
    assert completed["result"]["data_version"] in {row["id"] for row in research_options(data, runs)["snapshots"]}


def test_worker_failures_do_not_publish_success_or_expose_credentials(workspace, monkeypatch):
    data, runs, request = workspace
    def fail(*args, **kwargs):
        raise ValueError("token=hidden-value tsr_ThisIsAFakeTokenForTestingOnly")
    monkeypatch.setattr(task_worker, "run_configured", fail)
    service = PausedService(runs, data)
    task = service.submit(request, "worker-failure-123")
    service.store.claim()
    task_worker.run_task(runs, data, task["id"])
    completed = service.store.get(task["id"])
    assert completed["status"] == "failed" and completed["result"] is None
    assert "hidden-value" not in completed["error"] and "tsr_" not in completed["error"]


def test_single_dispatcher_serializes_jobs_and_releases_lock(workspace, monkeypatch):
    data, runs, request = workspace
    service = TaskService(runs, data)
    entered, release = threading.Event(), threading.Event()
    active, maximum, calls = 0, 0, []
    def execute(identifier):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        calls.append(identifier)
        entered.set()
        release.wait(timeout=5)
        service.store.finish(identifier, "succeeded", result={"saved": True})
        active -= 1
    monkeypatch.setattr(service, "_run_process", execute)
    try:
        first = service.submit(request, "serial-first-123")
        assert entered.wait(timeout=5)
        second = service.submit(request, "serial-second-123")
        assert service.store.get(second["id"])["status"] == "queued"
        other = TaskService(runs, data)
        assert not other.start()
        release.set()
        deadline = time.monotonic() + 5
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(.02)
        assert calls == [first["id"], second["id"]] and maximum == 1
    finally:
        release.set()
        service.close()
    assert other.start()
    other.close()


def test_real_background_process_keeps_http_available_and_persists_results(workspace):
    data, runs, request = workspace
    service = TaskService(runs, data)
    with TestClient(create_app(runs, data_root=data, task_service=service), base_url="http://127.0.0.1") as client:
        response = client.post("/api/v1/research/tasks", json=request, headers={"Idempotency-Key": "http-background-123"})
        assert response.status_code == 202
        identifier = response.json()["id"]
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            assert client.get("/api/v1/health").status_code == 200
            task = client.get("/api/v1/research/tasks/" + identifier).json()
            if task["status"] not in {"queued", "running"}:
                break
            time.sleep(.05)
        assert task["status"] == "succeeded", task.get("error")
        assert client.get("/api/v1/backtests/" + task["result"]["backtest_run_id"]).status_code == 200
    assert TaskStore(runs).get(identifier)["status"] == "succeeded"
