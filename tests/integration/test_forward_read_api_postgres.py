"""Exercise the public GET routes against persisted synthetic proof and real PostgreSQL."""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from test_forward_score_postgres import CODES, _execute, _fixture
from test_forward_verification_postgres import _publish_labels, _registered
from test_postgres_runtime import engine as engine

from finance_quant.api import create_app
from finance_quant.application.forward_score import enqueue_forward_score
from finance_quant.application.forward_verification import enqueue_forward_verification
from finance_quant.application.tasks import TaskService
from finance_quant.persistence.forward import canonical_sha256, registration_evidence_digest
from finance_quant.persistence.results import PgResultStore
from finance_quant.persistence.schema import forward_attempt, forward_registration, research_task
from finance_quant.persistence.tasks import PgTaskStore

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL"
)
URL = "/api/v1/forward-registrations"


@pytest.fixture
def client(engine, tmp_path):
    runs, data = tmp_path / "http-runs", tmp_path / "absent-http-market"
    service = TaskService(runs, data, task_store=PgTaskStore(runs, engine=engine))
    store = PgResultStore(runs, engine=engine, storage_root=tmp_path)
    return TestClient(create_app(result_store=store, task_service=service), base_url="http://testserver")


def _stored(engine):
    with engine.connect() as connection:
        return [
            connection.execute(select(table).order_by(table.c.id)).mappings().all()
            for table in (forward_registration, forward_attempt, research_task)
        ]


def _detail(client, identifier):
    response = client.get(f"{URL}/{identifier}")
    assert response.status_code == 200, response.text
    return response.json(), response.text


def test_list_paginates_on_timestamp_and_id_and_unknown_registration_is_404(engine, tmp_path, client):
    registration, _, _ = _fixture(engine, tmp_path)
    with engine.begin() as connection:
        saved = dict(connection.execute(select(forward_registration)).mappings().one())
        for identifier, created_at in (
            ("fwd-zzz", saved["created_at"]),
            ("fwd-newest", saved["created_at"] + timedelta(seconds=1)),
        ):
            row = dict(saved, id=identifier, idempotency_key=f"key-{identifier}", created_at=created_at)
            row["confirmed_evidence_sha256"] = registration_evidence_digest(row)
            connection.execute(forward_registration.insert().values(**row))

    expected = ["fwd-newest", "fwd-zzz", registration]
    for offset in range(4):
        response = client.get(URL, params={"limit": 1, "offset": offset})
        assert response.status_code == 200
        page = response.json()
        assert (page["limit"], page["offset"], page["total"]) == (1, offset, 3)
        assert [item["id"] for item in page["items"]] == expected[offset : offset + 1]
        for item in page["items"]:
            assert set(item) == {
                "id",
                "name",
                "status",
                "signal_date",
                "entry_date",
                "exit_date",
                "pool_size",
                "top_k",
                "factor",
                "factor_window",
                "factor_direction",
                "created_at",
            }
    assert client.get(URL, params={"limit": 201}).status_code == 422
    assert client.get(URL, params={"offset": -1}).status_code == 422
    assert client.get(f"{URL}/no-such-registration").json() == {"detail": "前瞻登记不存在"}
    assert client.get(f"{URL}/no-such-registration").status_code == 404
    assert client.post(URL, json={}).status_code == 405


@pytest.mark.parametrize("scenario", ["complete", "zero", "missing"])
def test_sealed_detail_uses_read_only_sql_and_keeps_zero_and_missing_distinct(
    engine, tmp_path, client, monkeypatch, scenario
):
    store, row, _ = _registered(engine, tmp_path)
    _publish_labels(
        tmp_path, row, "public-label", missing=CODES[-1] if scenario == "missing" else None, zero=scenario == "zero"
    )
    queued = enqueue_forward_verification(store, tmp_path, row["id"], key="verify-api-fixture")
    _, task = _execute(store, tmp_path, queued)
    assert task["status"] == "succeeded"
    before = _stored(engine)
    sql = []

    def collect(connection, cursor, statement, parameters, context, executemany):
        sql.append(statement.lower())

    def unexpected(*args, **kwargs):
        pytest.fail("GET touched a market file or invoked the queue")

    with monkeypatch.context() as patch:
        patch.setattr("pandas.read_parquet", unexpected)
        patch.setattr(PgTaskStore, "claim", unexpected)
        event.listen(engine, "before_cursor_execute", collect)
        try:
            assert client.get(URL).status_code == 200
            detail, text = _detail(client, row["id"])
        finally:
            event.remove(engine, "before_cursor_execute", collect)

    assert _stored(engine) == before
    assert sql.count("set transaction read only") == 2
    assert all(statement == "set transaction read only" or statement.startswith("select") for statement in sql)
    assert not any("for update" in statement for statement in sql)
    assert detail["confirmed"] is True
    assert (detail["pool_size"], detail["top_k"]) == (3, 1)
    assert detail["versions"]["score_snapshot"] == "synthetic-score"
    assert detail["versions"]["label_snapshot"].startswith("forward-label-")
    assert all(len(value) == 64 for value in detail["audit"].values())
    assert [attempt["kind"] for attempt in detail["attempts"]] == ["score", "verify"]
    assert all(attempt["sealed"] and len(attempt["evidence_sha256"]) == 64 for attempt in detail["attempts"])
    for secret in (
        str(tmp_path),
        "snapshot_dir",
        "market.parquet",
        "idempotency_key",
        "execution",
        "score_evidence_sha256",
        "entry_adjusted_open",
    ):
        assert secret not in text
    result = detail["result"]
    if scenario != "missing":
        assert detail["state"] == "verified"
        assert result["excess_return_pp"] == pytest.approx(0.0 if scenario == "zero" else 13.333333333)
        assert result["missing"] == []
    else:
        assert detail["state"] == "missing_labels"
        assert result["excess_return_pp"] is None and result["pool_valid"] == 2
        assert result["missing"] == [{"code": CODES[-1], "reasons": ["exit-missing-quote"]}]


@pytest.mark.parametrize(
    "case", ["unsealed", "awaiting_score", "awaiting_exit", "awaiting_quote", "failed", "verify_queued"]
)
def test_detail_distinguishes_unavailable_states(engine, tmp_path, client, case):
    if case in {"unsealed", "awaiting_score", "failed"}:
        identifier, _, _ = _fixture(engine, tmp_path, missing=case == "failed")
        if case == "unsealed":
            with engine.begin() as connection:
                connection.execute(forward_registration.update().values(confirmed_at=None))
        elif case == "failed":
            store = PgTaskStore(engine=engine)
            queued = enqueue_forward_score(
                store, tmp_path, identifier, "synthetic-score", key="incomplete-score-fixture"
            )
            assert _execute(store, tmp_path, queued)[1]["status"] == "failed"
    else:
        store, row, _ = _registered(engine, tmp_path, mature=case != "awaiting_exit")
        identifier = row["id"]
        if case == "verify_queued":
            _publish_labels(tmp_path, row, "ready-label")
            assert enqueue_forward_verification(store, tmp_path, identifier, key="queued-api-fixture")
    detail, _ = _detail(client, identifier)
    assert detail["state"] == case
    assert detail["result"] is None
    if case == "failed":
        assert detail["attempts"][0]["reason"] == "incomplete-score"
        assert detail["score_missing"][0]["code"] == CODES[-1]


@pytest.mark.parametrize("damage", ["registration", "score", "verify", "verification-task", "rehashed-result"])
def test_untrusted_proof_never_makes_a_result_available(engine, tmp_path, client, damage):
    store, row, score = _registered(engine, tmp_path)
    _publish_labels(tmp_path, row, "valid-label", zero=True)
    queued = enqueue_forward_verification(store, tmp_path, row["id"], key="tampered-api-fixture")
    assert _execute(store, tmp_path, queued)[1]["status"] == "succeeded"
    verify = forward_attempt.c
    with engine.begin() as connection:
        if damage == "registration":
            connection.execute(forward_registration.update().values(confirmed_evidence_sha256="0" * 64))
        elif damage == "score":
            connection.execute(
                forward_attempt.update().where(verify.id == score["attempt_id"]).values(evidence_row_id="bad")
            )
        elif damage == "verify":
            connection.execute(
                forward_attempt.update().where(verify.id == queued["attempt_id"]).values(evidence_sha256="0" * 64)
            )
        elif damage == "verification-task":
            connection.execute(
                research_task.update()
                .where(research_task.c.id == queued["task_id"])
                .values(execution={"private": "C:/staging/private"})
            )
        else:
            evidence = connection.scalar(select(verify.evidence).where(verify.id == queued["attempt_id"]))
            evidence["result"]["excess_return_pp"] = 20.0
            connection.execute(
                forward_attempt.update()
                .where(verify.id == queued["attempt_id"])
                .values(evidence=evidence, evidence_sha256=canonical_sha256(evidence))
            )
            connection.execute(forward_registration.update().values(result=evidence["result"]))
    detail, text = _detail(client, row["id"])
    assert detail["state"] == "invalid_evidence"
    assert detail["result"] is None
    assert "C:/staging/private" not in text
    assert detail["attempts"][-1]["sealed"] is False


def test_attempt_with_private_error_exposes_only_safe_category(engine, tmp_path, client):
    store, row, _ = _registered(engine, tmp_path)
    _publish_labels(tmp_path, row, "ready-label")
    queued = enqueue_forward_verification(store, tmp_path, row["id"], key="verify-private-error")
    with engine.begin() as connection:
        connection.execute(
            forward_attempt.update()
            .where(forward_attempt.c.id == queued["attempt_id"])
            .values(status="failed", error="SQL SELECT details from C:/staging/private; raw traceback")
        )
    detail, text = _detail(client, row["id"])
    assert detail["state"] == "failed" and detail["result"] is None
    assert detail["attempts"][-1]["reason"] == "attempt-failed"
    assert "C:/staging/private" not in text and "traceback" not in text


def test_database_error_uses_the_public_unavailable_response(engine, client):
    def fail(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("select"):
            raise OperationalError(statement, parameters, Exception("C:/staging/private"))

    event.listen(engine, "before_cursor_execute", fail)
    try:
        response = client.get(URL)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert response.status_code == 503
    assert response.json() == {"detail": "前瞻登记暂不可读"}
