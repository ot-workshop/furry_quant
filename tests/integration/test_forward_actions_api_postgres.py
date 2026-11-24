"""Use real POST routes, managed synthetic snapshots, and the PostgreSQL worker."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from shutil import copytree
from threading import Event

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from test_forward_label_capture_postgres import label_setup as label_setup
from test_forward_register_api_postgres import two_connection_engine as two_connection_engine
from test_forward_score_capture_postgres import setup_capture as setup_capture
from test_forward_score_postgres import _execute, _fixture
from test_forward_verification_postgres import _stored_registration
from test_postgres_runtime import engine as engine

from finance_quant.api import create_app
from finance_quant.application import forward_score
from finance_quant.application.forward_label_capture import publish_forward_label_snapshot
from finance_quant.application.forward_score_capture import publish_forward_score_snapshot
from finance_quant.application.research_requests import lookup_snapshot_meta
from finance_quant.application.tasks import TaskService
from finance_quant.data.market import managed_score_snapshot_id, score_snapshot_leaf_name
from finance_quant.persistence.maintenance import write_gate
from finance_quant.persistence.results import PgResultStore
from finance_quant.persistence.schema import forward_attempt, forward_registration, research_task
from finance_quant.persistence.tasks import PgTaskStore

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL"
)
URL = "/api/v1/forward-registrations"
SCORE_KEY = "manual-score-fixture-key"
VERIFY_KEY = "manual-verification-fixture-key"


def _client(engine, data_root, storage_root, monkeypatch):
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    runs = storage_root / "runs"
    store = PgResultStore(runs, engine=engine, storage_root=storage_root)
    service = TaskService(runs, data_root, task_store=PgTaskStore(runs, engine=engine))
    return TestClient(create_app(result_store=store, task_service=service), base_url="http://127.0.0.1")


def _post(client, identifier, stage, body, key):
    return client.post(f"{URL}/{identifier}/{stage}", json=body, headers={"idempotency-key": key})


def _accepted(response):
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert set(body) == {"status", "task_id", "attempt_id"}
    return body


def _queue_rows(engine):
    with engine.connect() as connection:
        return [
            connection.execute(select(table).order_by(table.c.id)).mappings().all()
            for table in (research_task, forward_attempt)
        ]


def _concurrent_post(client, identifier, stage, body, key):
    with ThreadPoolExecutor(max_workers=5) as workers:
        replies = [workers.submit(_post, client, identifier, stage, body, key) for _ in range(5)]
        bindings = [_accepted(reply.result(timeout=15)) for reply in replies]
    assert all(binding == bindings[0] for binding in bindings)
    return bindings[0]


def test_score_options_only_list_verified_match_and_reuse_original_binding(
    engine, tmp_path, setup_capture, monkeypatch
):
    root, row, supplier, publish = setup_capture
    client = _client(engine, root, tmp_path, monkeypatch)
    identifier = row["id"]
    options_url = f"{URL}/{identifier}/score-options"
    assert client.get(options_url).json() == {"bound": None, "items": []}
    assert client.get(f"{URL}/missing/score-options").status_code == 404

    first = publish()
    second = publish_forward_score_snapshot(
        engine, root, identifier, storage_root=tmp_path, provider=supplier, key="second-option"
    )
    relative, manifest, _ = lookup_snapshot_meta(root, first["id"], allow_score=True)
    for change in (
        {"registration_sha256": "f" * 64},
        {"registration_id": "some-other-registration"},
        {"captured_at": row["confirmed_at"].isoformat()},
        {"source_dataset_id": "snapshot-" + "f" * 64},
        {"pool_codes": list(reversed(row["pool_codes"]))},
        {"data_end": row["exit_date"].isoformat()},
    ):
        foreign = deepcopy(manifest)
        foreign["score_capture"].update(change)
        foreign["id"] = managed_score_snapshot_id(foreign)
        leaf = root / relative.parent / score_snapshot_leaf_name(foreign)
        copytree(root / relative, leaf)
        (leaf / "manifest.json").write_text(json.dumps(foreign), encoding="utf-8")
    calls = len(supplier.calls)
    with write_gate(engine, exclusive=True):
        result = client.get(options_url)
    assert result.status_code == 200
    assert result.json()["bound"] is None
    assert len(supplier.calls) == calls
    assert _queue_rows(engine) == [[], []]
    items = result.json()["items"]
    assert {item["id"] for item in items} == {first["id"], second["id"]}
    assert all(set(item) == {"id", "data_end", "captured_at"} for item in items)
    assert all(item["data_end"] == row["signal_date"].isoformat() for item in items)
    assert client.get(f"{URL}/{identifier}").json()["available_actions"] == {"score": True, "verify": False}

    accepted = _accepted(_post(client, identifier, "score", {"score_snapshot_id": first["id"]}, SCORE_KEY))
    PgTaskStore(engine=engine).cancel(accepted["task_id"])
    (root / relative / "manifest.json").unlink()
    assert client.get(options_url).json() == {"bound": first["id"], "items": []}
    assert client.get(f"{URL}/{identifier}").json()["available_actions"] == {"score": True, "verify": False}


def test_manual_score_and_verification_run_in_worker_and_read_back(
    engine, tmp_path, two_connection_engine, setup_capture, monkeypatch
):
    root, row, supplier, publish_score = setup_capture
    client = _client(two_connection_engine, root, tmp_path, monkeypatch)
    store = PgTaskStore(engine=engine)
    identifier = row["id"]
    score_snapshot = publish_score()
    score_body = {"score_snapshot_id": score_snapshot["id"]}
    calls_before = len(supplier.calls)
    score = _concurrent_post(client, identifier, "score", score_body, SCORE_KEY)
    assert len(supplier.calls) == calls_before
    queued = client.get(f"{URL}/{identifier}").json()
    assert queued["state"] == "score_queued"
    assert queued["attempts"][0]["id"] == score["attempt_id"]
    assert queued["versions"]["score_snapshot"] == score_snapshot["id"]
    assert len(_queue_rows(engine)[0]) == 1
    active = _post(client, identifier, "score", score_body, "second-score-while-active")
    assert active.status_code == 409 and "排队/运行" in active.json()["detail"]
    changed = {"score_snapshot_id": "forward-score-" + "f" * 64}
    assert _post(client, identifier, "score", changed, SCORE_KEY).status_code == 409
    store.cancel(score["task_id"])
    score_retry = _accepted(_post(client, identifier, "score", score_body, "manual-score-retry"))
    assert score_retry["attempt_id"] != score["attempt_id"]
    assert _execute(store, root, score_retry)[1]["status"] == "succeeded"
    path, _, _ = lookup_snapshot_meta(root, score_snapshot["id"], allow_score=True)
    (root / path / "manifest.json").unlink()
    assert _accepted(_post(client, identifier, "score", score_body, SCORE_KEY)) == score
    assert _accepted(_post(client, identifier, "score", score_body, "manual-score-retry")) == score_retry

    before = _queue_rows(engine)
    early = _post(client, identifier, "verify", {}, VERIFY_KEY)
    assert early.status_code == 409 and early.json()["detail"] == "before-verification-maturity"
    wrong_stage = _post(client, identifier, "verify", {}, SCORE_KEY)
    assert wrong_stage.status_code == 409 and "其他登记或阶段" in wrong_stage.json()["detail"]
    assert _queue_rows(engine) == before
    assert client.get(f"{URL}/{identifier}").json()["state"] == "awaiting_exit"

    # Supply the frozen maturity from PostgreSQL for this synthetic future registration.
    as_of = datetime.fromisoformat(row["data_rules"]["label_capture_after"])
    supplier.sessions = pd.DatetimeIndex([row[f"{name}_date"] for name in ("signal", "entry", "exit")])

    def clock(connection, cursor, statement, parameters, context, executemany):
        if statement.lower().startswith("select clock_timestamp()"):
            return "select cast(%s as timestamptz)", (as_of.isoformat(),)
        return statement, parameters

    for candidate_engine in (engine, two_connection_engine):
        event.listen(candidate_engine, "before_cursor_execute", clock, retval=True)
    try:
        waiting = _post(client, identifier, "verify", {}, VERIFY_KEY)
        assert waiting.status_code == 200 and waiting.json() == {"status": "awaiting_quote"}
        assert _queue_rows(engine) == before and _stored_registration(engine)["label_snapshot"] is None
        assert _post(client, identifier, "verify", {}, SCORE_KEY).status_code == 409
        first = publish_forward_label_snapshot(
            engine, root, identifier, storage_root=tmp_path, provider=supplier, key="manual-first-label"
        )
        verify = _concurrent_post(client, identifier, "verify", {}, VERIFY_KEY)
        assert client.get(f"{URL}/{identifier}").json()["state"] == "verify_queued"
        assert len(_queue_rows(engine)[0]) == 3

        as_of += timedelta(seconds=1)
        later = publish_forward_label_snapshot(
            engine, root, identifier, storage_root=tmp_path, provider=supplier, key="manual-later-label"
        )
        assert later["id"] != first["id"]
        store.cancel(verify["task_id"])
        assert _accepted(_post(client, identifier, "verify", {}, VERIFY_KEY)) == verify
        retry = _accepted(_post(client, identifier, "verify", {}, "manual-verification-retry"))
        assert retry["attempt_id"] != verify["attempt_id"]
        assert _execute(store, root, retry)[1]["status"] == "succeeded"
        assert _accepted(_post(client, identifier, "verify", {}, VERIFY_KEY)) == verify
        done = client.get(f"{URL}/{identifier}").json()
        assert done["state"] == "verified" and done["versions"]["label_snapshot"] == first["id"]
        assert done["result"]["excess_return_pp"] == pytest.approx(2.333333333)
        assert [attempt["status"] for attempt in done["attempts"]] == [
            "cancelled",
            "succeeded",
            "cancelled",
            "succeeded",
        ]
    finally:
        for candidate_engine in (engine, two_connection_engine):
            event.remove(candidate_engine, "before_cursor_execute", clock)

    assert two_connection_engine.pool.checkedout() == 0
    with write_gate(engine, exclusive=True):
        assert _stored_registration(engine)["label_snapshot"]["data_version"] == first["id"]


@pytest.mark.parametrize("winner", ["same-version", "different-version", "different-stage", "different-key", "none"])
def test_score_rechecks_committed_key_after_snapshot_lookup_fails(
    engine, tmp_path, two_connection_engine, monkeypatch, request, winner
):
    if winner == "different-stage":
        label_root, _, verify_row, _, publish_label = request.getfixturevalue("label_setup")
        publish_label()
        verify_client = _client(two_connection_engine, label_root, tmp_path, monkeypatch)

    root, row, _, publish = request.getfixturevalue("setup_capture")
    client = _client(two_connection_engine, root, tmp_path, monkeypatch)
    identifier = row["id"]
    published_id = publish()["id"]
    relative, _, _ = lookup_snapshot_meta(root, published_id, allow_score=True)
    requested_id = "forward-score-" + "f" * 64 if winner == "different-version" else published_id
    entered_lookup, finish_lookup = Event(), Event()

    def pause_first_lookup(data_root, snapshot_id, *, allow_score=False):
        if not entered_lookup.is_set():
            entered_lookup.set()
            assert finish_lookup.wait(timeout=15)
        return lookup_snapshot_meta(data_root, snapshot_id, allow_score=allow_score)

    monkeypatch.setattr(forward_score, "lookup_snapshot_meta", pause_first_lookup)

    def score_binding():
        with engine.connect() as connection:
            table = forward_registration.c
            return connection.scalar(select(table.score_snapshot).where(table.id == identifier))

    before_posts = _queue_rows(engine)
    body = {"score_snapshot_id": requested_id}
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(_post, client, identifier, "score", body, SCORE_KEY)
        try:
            assert entered_lookup.wait(timeout=10)
            assert two_connection_engine.pool.checkedout() == 1
            assert score_binding() is None
            if winner == "different-stage":
                original = _accepted(_post(verify_client, verify_row["id"], "verify", {}, SCORE_KEY))
            elif winner != "none":
                key = "other-score-key" if winner == "different-key" else SCORE_KEY
                original = _accepted(_post(client, identifier, "score", {"score_snapshot_id": published_id}, key))
            before = _queue_rows(engine)
            binding = score_binding()
            (root / relative / "manifest.json").unlink()
        finally:
            finish_lookup.set()
        response = pending.result(timeout=15)

    assert two_connection_engine.pool.checkedout() == 0
    with write_gate(engine, exclusive=True):
        assert _queue_rows(engine) == before
        assert score_binding() == binding
    added = 0 if winner == "none" else 1
    assert [len(after) - len(initial) for initial, after in zip(before_posts, before, strict=True)] == [added, added]
    if winner == "same-version":
        assert _accepted(response) == original
    elif winner == "different-version":
        assert response.status_code == 409 and "参数已改变" in response.json()["detail"]
    elif winner == "different-stage":
        assert response.status_code == 409
        assert response.json()["detail"] == "该请求标识已用于其他登记或阶段"
        assert binding is None
    else:
        with pytest.raises(ValueError) as error:
            lookup_snapshot_meta(root, requested_id, allow_score=True)
        assert response.status_code == 422 and response.json()["detail"] == str(error.value)
        if winner == "none":
            assert binding is None


@pytest.mark.parametrize("stage", ["score", "verify"])
def test_shared_queue_limit_keeps_first_binding_and_attempt_absent(engine, tmp_path, request, monkeypatch, stage):
    if stage == "score":
        root, row, _, publish = request.getfixturevalue("setup_capture")
        body = {"score_snapshot_id": publish()["id"]}
    else:
        root, _, row, _, publish = request.getfixturevalue("label_setup")
        publish()
        body = {}
    store = PgTaskStore(engine=engine)
    for index in range(20):
        if index % 2:
            store.create({"name": "Standard task"}, {}, f"manual-fill-{index}")
        else:
            store.create_download("market_download", {"name": "Standard download"}, {}, f"manual-fill-{index}")
    before = _queue_rows(engine)
    response = _post(_client(engine, root, tmp_path, monkeypatch), row["id"], stage, body, "full-queue-fixture-key")
    assert response.status_code == 409 and "20" in response.json()["detail"]
    assert _queue_rows(engine) == before
    field = "score_snapshot" if stage == "score" else "label_snapshot"
    assert _stored_registration(engine)[field] is None


def test_actions_use_maintenance_and_http_admission_without_side_effects(
    engine, tmp_path, two_connection_engine, monkeypatch
):
    identifier, leaf, _ = _fixture(engine, tmp_path)
    client = _client(two_connection_engine, tmp_path, tmp_path, monkeypatch)
    unknown = {"score_snapshot_id": "forward-score-" + "b" * 64}
    with write_gate(engine, exclusive=True):
        for stage, body in (("score", unknown), ("verify", {})):
            held = _post(client, identifier, stage, body, f"maintenance-{stage}-key")
            assert held.status_code == 409 and "维护" in held.json()["detail"]
        assert client.get(f"{URL}/{identifier}").status_code == 200
    for stage, body in (("score", unknown), ("verify", {})):
        assert _post(client, "unknown-registration", stage, body, "unknown-reg-key").status_code == 404
        cross_site = client.post(f"{URL}/{identifier}/{stage}", json=body, headers={"sec-fetch-site": "cross-site"})
        assert cross_site.status_code == 403
    assert _post(client, identifier, "score", {"score_snapshot_id": leaf.name}, SCORE_KEY).status_code == 422
    assert _post(client, identifier, "score", unknown, SCORE_KEY).status_code == 422
    assert _post(client, identifier, "verify", {"label_snapshot_id": "caller-version"}, VERIFY_KEY).status_code == 422
    assert _post(client, identifier, "verify", {}, VERIFY_KEY).status_code == 409
    assert _queue_rows(engine) == [[], []]
    saved = _stored_registration(engine)
    assert saved["score_snapshot"] is saved["label_snapshot"] is None
    assert two_connection_engine.pool.checkedout() == 0
    with write_gate(engine, exclusive=True):
        assert _queue_rows(engine) == [[], []]
