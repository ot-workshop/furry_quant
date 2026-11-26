import copy
import json
import shutil
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from test_dynamic_experiments import pool

from finance_quant.application import experiment_tasks, market_requests
from finance_quant.application import warehouse_comparison_limits as limits
from finance_quant.checksums import sha256_bytes
from support.fakes import make_app, make_service, run_claimed

URL = "/api/v1/research/experiments"


def assert_no_heavy_input(monkeypatch):
    read_parquet = pd.read_parquet

    def references_only(path, *args, **kwargs):
        assert "assets" not in Path(path).parts, "an asset body was read before the budget decision"
        return read_parquet(path, *args, **kwargs)

    def unexpected(*args, **kwargs):
        pytest.fail("budget rejection crossed the preparation or publication boundary")

    monkeypatch.setattr(pd, "read_parquet", references_only)
    monkeypatch.setattr(experiment_tasks, "prepare_warehouse", unexpected)
    monkeypatch.setattr(experiment_tasks, "run_experiment", unexpected)


@pytest.mark.parametrize("dimension", ["codes", "calendar", "warmup", "weight"])
def test_http_limits_fail_before_warehouse_resolution(tmp_path, monkeypatch, dimension):
    data, _, document = pool(tmp_path, "market_warehouse")
    if dimension == "codes":
        document["data"]["codes"] = [f"{600000 + i}.SH" for i in range(limits.MAX_STOCKS + 1)]
        expected = "显式股票数"
    elif dimension == "calendar":
        document["plan"]["periods"]["test"]["end"] = "2026-02-02"
        expected = "自然日数"
    elif dimension == "warmup":
        monkeypatch.setattr(limits, "MAX_SESSIONS", 3)
        expected = "最少交易日数"
    else:
        monkeypatch.setattr(limits, "MAX_PLAN_WEIGHT", 19)
        expected = "方案权重"
    service = make_service(tmp_path / "runs", data)
    client = TestClient(make_app(service.runs_root, data, service=service), base_url="http://127.0.0.1")
    monkeypatch.setattr(limits, "_pinned_manifest", lambda *args: pytest.fail("HTTP opened the warehouse"))
    response = client.post(URL, json=document, headers={"Idempotency-Key": f"warehouse-http-{dimension}"})
    assert response.status_code == 422 and expected in response.text
    assert service.store.list()["total"] == 0


@pytest.mark.parametrize("mode", ["market_warehouse", "index_warehouse"])
def test_effective_calendar_and_stock_boundary(tmp_path, monkeypatch, mode):
    data, root, document = pool(tmp_path, mode)
    plan = experiment_tasks._plan(document)
    expected_days = len(pd.bdate_range("2024-01-08", "2024-02-02")) + 3
    monkeypatch.setattr(limits, "MAX_STOCKS", 5)
    monkeypatch.setattr(limits, "MAX_SESSIONS", expected_days)
    monkeypatch.setattr(limits, "MAX_WEIGHTED_SESSIONS", 5 * expected_days * 20)
    assert_no_heavy_input(monkeypatch)
    assert limits.check_warehouse(data, document["data"], plan)[0] == root.resolve()
    with monkeypatch.context() as lowered:
        lowered.setattr(limits, "MAX_SESSIONS", expected_days - 1)
        with pytest.raises(ValueError, match="含预热交易日数"):
            limits.check_warehouse(data, document["data"], plan)
    with monkeypatch.context() as lowered:
        lowered.setattr(limits, "MAX_STOCKS", 4)
        with pytest.raises(ValueError, match="股票数"):
            limits.check_warehouse(data, document["data"], plan)
    monkeypatch.setattr(limits, "MAX_WEIGHTED_SESSIONS", 5 * expected_days * 20 - 1)
    with pytest.raises(ValueError, match="加权股票交易日"):
        limits.check_warehouse(data, document["data"], plan)


def test_common_sample_costs_more_than_independent_with_same_pool(tmp_path, monkeypatch):
    data, _, document = pool(tmp_path, "market_warehouse")
    document["plan"]["periods"] = {"analysis": {"start": "2024-01-08", "end": "2024-02-02"}}
    document["plan"]["comparison"] = {"baseline_id": "momentum", "sample_mode": "independent"}
    expected_days = len(pd.bdate_range("2024-01-08", "2024-02-02")) + 3
    monkeypatch.setattr(limits, "MAX_WEIGHTED_SESSIONS", 5 * expected_days * 12)
    assert_no_heavy_input(monkeypatch)
    limits.check_warehouse(data, document["data"], experiment_tasks._plan(document))
    document["plan"]["comparison"]["sample_mode"] = "common"
    with pytest.raises(ValueError, match="加权股票交易日"):
        limits.check_warehouse(data, document["data"], experiment_tasks._plan(document))


@pytest.mark.parametrize(
    "dimension,message",
    [
        ("MAX_WAREHOUSE_FILES", "整仓文件数"),
        ("MAX_WAREHOUSE_BYTES", "整仓冻结字节数"),
        ("MAX_INPUT_ROWS", "输入行数"),
        ("MAX_INPUT_BYTES", "Parquet 解压字节数"),
    ],
)
def test_physical_budget_uses_actual_files_and_selected_parquet_footers(tmp_path, monkeypatch, dimension, message):
    data, root, document = pool(tmp_path, "market_warehouse")
    document["data"]["codes"] = ["600000.SH", "600100.SH"]
    plan = experiment_tasks._plan(document)
    raw = (root / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    refs = [root / "reference" / name for name in manifest["reference_files"]]
    assets = {code: root / asset["file"] for code, asset in manifest["assets"].items()}
    opened = [pq.read_metadata(path) for path in [*refs, *(assets[code] for code in document["data"]["codes"])]]
    boundary = {
        "MAX_WAREHOUSE_FILES": len(refs) + len(assets) + 1,
        "MAX_WAREHOUSE_BYTES": len(raw) + sum(path.stat().st_size for path in [*refs, *assets.values()]),
        "MAX_INPUT_ROWS": sum(meta.num_rows for meta in opened),
        "MAX_INPUT_BYTES": sum(
            meta.row_group(i).total_byte_size for meta in opened for i in range(meta.num_row_groups)
        ),
    }[dimension]
    assert_no_heavy_input(monkeypatch)
    monkeypatch.setattr(limits, dimension, boundary)
    assert limits.check_warehouse(data, document["data"], plan)[0] == root.resolve()
    monkeypatch.setattr(limits, dimension, boundary - 1)
    with pytest.raises(ValueError, match=message):
        limits.check_warehouse(data, document["data"], plan)


@pytest.mark.parametrize("legacy", [False, True], ids=["pending", "legacy-complete-payload"])
def test_worker_rejection_leaves_queued_payload_and_replays_same_key(tmp_path, monkeypatch, legacy):
    data, _, document = pool(tmp_path, "market_warehouse")
    runs = tmp_path / "runs"
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")
    key = {"Idempotency-Key": "legacy-limit"}
    if legacy:
        _, pins = experiment_tasks.validate_experiment_request(document, data, runs)
        queued = service.store.create_experiment(document, pins, key["Idempotency-Key"], total=6)
    else:
        response = client.post(URL, json=document, headers=key)
        assert response.status_code == 202, response.text
        queued = response.json()
    identifier = queued["id"]
    original = service.store.get(identifier, private=True)["execution"]
    before = {path.relative_to(tmp_path) for root in (data, runs / "experiments") for path in root.rglob("*")}
    monkeypatch.setattr(limits, "MAX_STOCKS", 4)
    assert_no_heavy_input(monkeypatch)
    assert service.store.claim() == identifier
    task = run_claimed(service, identifier)
    assert task["status"] == "failed" and task["result"] is None
    assert task["error_details"]["stage"] == "prepare" and "展开后的股票数" in task["error"]
    assert service.store.get(identifier, private=True)["execution"] == original
    after = {path.relative_to(tmp_path) for root in (data, runs / "experiments") for path in root.rglob("*")}
    assert after == before
    assert client.post(URL, json=document, headers=key).json()["id"] == identifier
    changed = {**document, "name": "另一请求"}
    assert client.post(URL, json=changed, headers=key).status_code == 409
    assert service.store.list()["total"] == 1


def test_pending_warehouse_uses_submitted_id_and_rejects_rewritten_manifest(tmp_path, monkeypatch):
    data, root, document = pool(tmp_path, "market_warehouse")
    service = make_service(tmp_path / "runs", data)
    queued = service.submit_experiment(document, "frozen-warehouse")
    newer = root.parent.with_name("newer")
    shutil.copytree(root.parent, newer)
    replacement = newer / root.name / "manifest.json"
    replacement.write_bytes(replacement.read_bytes() + b"\n")
    assert "market-" + sha256_bytes(replacement.read_bytes())[:20] != document["data"]["warehouse_id"]
    observed = []
    market_option = market_requests.market_option

    def current_only(path, **kwargs):
        observed.append(path)
        return market_option(path, **kwargs)

    monkeypatch.setattr(market_requests, "market_option", current_only)
    _, pins = experiment_tasks.validate_experiment_request(document, data, service.runs_root)
    assert observed == [root.resolve()] and Path(pins["warehouse_dir"]) == root.resolve()
    (root / "manifest.json").write_bytes(replacement.read_bytes())
    assert_no_heavy_input(monkeypatch)
    assert service.store.claim() == queued["id"]
    failed = run_claimed(service, queued["id"])
    assert failed["status"] == "failed" and "版本不存在或已变化" in failed["error"]
    assert service.submit_experiment(copy.deepcopy(document), "frozen-warehouse")["id"] == queued["id"]


def test_original_key_skips_the_new_http_limit_for_old_task(tmp_path, monkeypatch):
    data, root, document = pool(tmp_path, "market_warehouse")
    document["data"]["codes"] = sorted(json.loads((root / "manifest.json").read_bytes())["assets"])
    service = make_service(tmp_path / "runs", data)
    client = TestClient(make_app(service.runs_root, data, service=service), base_url="http://127.0.0.1")
    original = client.post(URL, json=document, headers={"Idempotency-Key": "warehouse-old-key"})
    assert original.status_code == 202, original.text
    monkeypatch.setattr(limits, "MAX_STOCKS", 4)
    retry = client.post(URL, json=document, headers={"Idempotency-Key": "warehouse-old-key"})
    assert retry.status_code == 202 and retry.json()["id"] == original.json()["id"]
    rejected = client.post(URL, json=document, headers={"Idempotency-Key": "warehouse-new-key"})
    assert rejected.status_code == 422 and "显式股票数" in rejected.text
    assert service.store.list()["total"] == 1


def test_selected_oversized_manifest_has_its_own_reason(tmp_path):
    data, root, document = pool(tmp_path, "market_warehouse")
    oversized = root.parent.with_name("large")
    shutil.copytree(root.parent, oversized)
    manifest = oversized / root.name / "manifest.json"
    raw = manifest.read_bytes()
    oversized_bytes = limits.MAX_MANIFEST_BYTES + 1
    manifest.write_bytes(raw + b" " * (oversized_bytes - len(raw)))
    plan = experiment_tasks._plan(document)
    assert limits.check_warehouse(data, document["data"], plan)[0] == root.resolve()

    digest = sha256_bytes(manifest.read_bytes())
    selected = {**document["data"], "warehouse_id": "market-" + digest[:20]}
    with pytest.raises(ValueError, match=rf"清单字节数超限（{oversized_bytes}，上限 {limits.MAX_MANIFEST_BYTES}）"):
        limits.check_warehouse(data, selected, plan)
    legacy = {"mode": "market_warehouse", "warehouse_dir": str(manifest.parent), "manifest_sha256": digest}
    with pytest.raises(ValueError, match="清单字节数超限"):
        limits.check_warehouse(data, selected, plan, execution=legacy)
    with pytest.raises(ValueError, match="版本不存在或已变化"):
        limits.check_warehouse(data, {**selected, "warehouse_id": "market-" + "0" * 20}, plan)
