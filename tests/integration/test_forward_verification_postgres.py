"""Maturity and label semantics through the real PostgreSQL queue and worker."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import event, select
from test_forward_score_postgres import CODES, SOURCE, _execute, _fixture
from test_postgres_runtime import engine as engine

from finance_quant.application import forward_verification
from finance_quant.application.forward_score import enqueue_forward_score
from finance_quant.application.forward_verification import VerificationInputError, enqueue_forward_verification
from finance_quant.checksums import file_sha256
from finance_quant.data.symbols import to_qlib
from finance_quant.persistence.forward import canonical_sha256
from finance_quant.persistence.schema import forward_attempt, forward_registration, research_task
from finance_quant.persistence.tasks import PgTaskStore, worker_lock
from finance_quant.task_contracts import TaskConflict

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL"
)
VERIFY_KEY = "synthetic-maturity-key"


def _registered(engine, root, *, mature=True):
    reg, score_leaf, _ = _fixture(engine, root, late=mature)
    store = PgTaskStore(engine=engine)
    score = enqueue_forward_score(store, root, reg, score_leaf.name, key="historically-sealed-score")
    assert store.claim() == score["task_id"]
    with engine.begin() as connection:
        row = connection.execute(select(forward_registration)).mappings().one()
        binding = row["score_snapshot"]
        staging = datetime.fromisoformat(binding["captured_at"]) + timedelta(minutes=1)
        evidence = {
            "registration_id": reg,
            "registration_sha256": row["confirmed_evidence_sha256"],
            "signal_date": row["signal_date"].isoformat(),
            "score_snapshot": binding,
            "pool_size": 3,
            "top_k": 1,
            "scores": [
                {"code": code, "momentum": value, "is_suspended": False, "reasons": []}
                for code, value in zip(CODES, (0.2, 0.2, -0.1), strict=True)
            ],
            "candidate_codes": [CODES[0]],
        }
        # Simulate the prior historical score. Verification uses the actual queue and
        # sealing workflow, and its proof timestamps come from the real database.
        connection.execute(
            forward_attempt.update()
            .where(forward_attempt.c.id == score["attempt_id"])
            .values(
                status="succeeded",
                staging_at=staging,
                sealed_at=staging + timedelta(seconds=1),
                evidence=evidence,
                evidence_sha256=canonical_sha256(evidence),
                evidence_row_id=score["attempt_id"],
            )
        )
        store.finish(
            score["task_id"],
            "succeeded",
            result={"registration_id": reg, "score_attempt_id": score["attempt_id"]},
            connection=connection,
        )
    return store, row, score


def _publish_labels(
    root,
    row,
    identifier,
    *,
    seconds=0,
    missing=None,
    source=SOURCE,
    wrong_calendar=False,
    zero=False,
    trading_state=True,
):
    leaf = root / "research" / "subsets" / identifier
    leaf.mkdir(parents=True)
    entry, exit_ = row["entry_date"], row["exit_date"]
    maturity = datetime.fromisoformat(row["data_rules"]["label_capture_after"])
    prices = ((6.0, 2.0), (11.0, 1.0), (9.0, 1.0))
    records = []
    for code, (raw_open, factor) in zip(CODES, prices, strict=True):
        records.append(
            {
                "instrument": to_qlib(code),
                "date": entry,
                "open": 10.0,
                "close": 10.0,
                "adj_factor": 1.0,
                "is_suspended": False,
            }
        )
        if code != missing:
            price = 10.0 / factor if zero else raw_open
            records.append(
                {
                    "instrument": to_qlib(code),
                    "date": exit_,
                    "open": price,
                    "close": price,
                    "adj_factor": factor,
                    "is_suspended": False,
                }
            )
    market = pd.DataFrame(records).assign(date=lambda frame: pd.to_datetime(frame.date))
    if not trading_state:
        market = market.drop(columns="is_suspended")
    frames = {
        "market.parquet": market,
        "benchmark.parquet": pd.DataFrame({"date": pd.to_datetime([entry, exit_])}),
        "calendar.parquet": pd.DataFrame(
            {"date": pd.to_datetime([entry, exit_]), "is_open": [True, not wrong_calendar]}
        ),
    }
    for name, frame in frames.items():
        frame.to_parquet(leaf / name, index=False)
    manifest = {
        "id": identifier,
        "schema_version": "market-v1",
        "files": {name: file_sha256(leaf / name) for name in frames},
        "label_capture": {
            "captured_at": (maturity + timedelta(seconds=seconds)).isoformat(),
            "data_end": exit_.isoformat(),
            "pool_codes": CODES,
            "source_dataset_id": source,
        },
    }
    (leaf / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return leaf


def _stored_registration(engine):
    with engine.connect() as connection:
        return connection.execute(select(forward_registration)).mappings().one()


def test_waits_for_a_valid_first_capture_then_seals_hand_calculated_result(engine, tmp_path):
    store, row, score = _registered(engine, tmp_path)
    reg = row["id"]
    _publish_labels(tmp_path, row, "before-maturity", seconds=-0.000001)
    _publish_labels(tmp_path, row, "wrong-source", source="other")
    _publish_labels(tmp_path, row, "wrong-calendar", wrong_calendar=True)
    corrupted = _publish_labels(tmp_path, row, "corrupt-label")
    with (corrupted / "market.parquet").open("ab") as stream:
        stream.write(b"invalid")
    assert enqueue_forward_verification(store, tmp_path, reg, key=VERIFY_KEY) is None
    assert _stored_registration(engine)["label_snapshot"] is None
    assert store.list()["total"] == 1

    _publish_labels(tmp_path, row, "later-visible", seconds=60, zero=True)
    first = _publish_labels(tmp_path, row, "first-qualified")
    queued = enqueue_forward_verification(store, tmp_path, reg, key=VERIFY_KEY)
    assert _stored_registration(engine)["label_snapshot"]["data_version"] == first.name
    _publish_labels(tmp_path, row, "new-version", seconds=120, zero=True)
    assert enqueue_forward_verification(store, tmp_path, reg, key=VERIFY_KEY) == queued

    attempt, task = _execute(store, tmp_path, queued)
    saved = _stored_registration(engine)
    mature_at = datetime.fromisoformat(row["data_rules"]["label_capture_after"])
    assert mature_at == attempt["deadline_at"] <= attempt["staging_at"] <= attempt["sealed_at"]
    assert task["status"] == attempt["status"] == "succeeded"
    assert saved["result_recorded_at"] == attempt["sealed_at"]
    assert attempt["evidence"]["score_attempt_id"] == score["attempt_id"]
    result = saved["result"]
    assert result == attempt["evidence"]["result"]
    assert canonical_sha256(attempt["evidence"]) == attempt["evidence_sha256"]
    assert result["entry_date"] == row["entry_date"].isoformat()
    assert result["exit_date"] == row["exit_date"].isoformat()
    assert result["candidate_codes"] == [CODES[0]]
    assert (result["candidate_valid"], result["top_k"], result["pool_valid"], result["pool_size"]) == (1, 1, 3, 3)
    assert [item["return"] for item in result["labels"]] == pytest.approx([0.2, 0.1, -0.1])
    assert result["labels"][0]["exit_adjusted_open"] == 12.0
    assert result["excess_return_pp"] == pytest.approx(100 * (0.2 - (0.2 + 0.1 - 0.1) / 3))
    assert enqueue_forward_verification(store, tmp_path, reg, key=VERIFY_KEY) == queued
    with pytest.raises(TaskConflict, match="verification-already-sealed"):
        enqueue_forward_verification(store, tmp_path, reg, key=VERIFY_KEY + "-new")
    print(f"maturity={mature_at.isoformat()}, binding={first.name}, excess_pp={result['excess_return_pp']:.9f}")


@pytest.mark.parametrize("missing", [None, CODES[0], CODES[2]], ids=["zero", "missing-candidate", "missing-pool"])
def test_incomplete_labels_keep_frozen_denominators_and_true_zero_is_available(engine, tmp_path, missing):
    store, row, _ = _registered(engine, tmp_path)
    _publish_labels(tmp_path, row, "label-version", missing=missing, zero=missing is None)
    queued = enqueue_forward_verification(store, tmp_path, row["id"], key=VERIFY_KEY)
    attempt, task = _execute(store, tmp_path, queued)
    assert task["status"] == attempt["status"] == "succeeded"
    result = _stored_registration(engine)["result"]
    assert (result["top_k"], result["pool_size"], result["candidate_codes"]) == (1, 3, [CODES[0]])
    assert result["missing_codes"] == ([missing] if missing else [])
    assert result["candidate_valid"] == int(missing != CODES[0])
    assert result["pool_valid"] == (2 if missing else 3)
    assert result["excess_return_pp"] == (None if missing else 0)
    if missing:
        label = next(item for item in result["labels"] if item["code"] == missing)
        assert label["reasons"] == ["exit-missing-quote"]
        assert label["return"] is None


def test_before_maturity_does_not_bind_or_enqueue(engine, tmp_path):
    store, row, _ = _registered(engine, tmp_path, mature=False)
    _publish_labels(tmp_path, row, "future-label")
    with pytest.raises(VerificationInputError, match="before-verification-maturity"):
        enqueue_forward_verification(store, tmp_path, row["id"], key=VERIFY_KEY)
    assert _stored_registration(engine)["label_snapshot"] is None
    assert store.list()["total"] == 1


def test_missing_trading_state_column_keeps_original_denominators_and_null_metric(engine, tmp_path):
    store, row, _ = _registered(engine, tmp_path)
    _publish_labels(tmp_path, row, "missing-trading-state", trading_state=False)
    queued = enqueue_forward_verification(store, tmp_path, row["id"], key=VERIFY_KEY)
    attempt, task = _execute(store, tmp_path, queued)
    assert task["status"] == attempt["status"] == "succeeded"
    result = _stored_registration(engine)["result"]
    assert result == attempt["evidence"]["result"]
    assert (result["top_k"], result["pool_size"], result["candidate_codes"]) == (1, 3, [CODES[0]])
    assert (result["candidate_valid"], result["pool_valid"]) == (0, 0)
    assert result["missing_codes"] == CODES
    assert result["candidate_return"] is result["pool_return"] is result["excess_return_pp"] is None
    for item in result["labels"]:
        assert item["reasons"] == ["entry-missing-trading-state", "exit-missing-trading-state"]
        assert item["return"] is None


@pytest.mark.parametrize("zone", ["UTC", "Asia/Shanghai"])
def test_exact_maturity_boundary_uses_absolute_sample(engine, tmp_path, zone):
    store, row, _ = _registered(engine, tmp_path)
    _publish_labels(tmp_path, row, "on-boundary")
    maturity = datetime.fromisoformat(row["data_rules"]["label_capture_after"]).astimezone(timezone.utc)
    now = maturity - timedelta(microseconds=1)

    def clock(connection, cursor, statement, parameters, context, executemany):
        if statement == "select clock_timestamp()":
            return f"select timezone('{zone}', cast('{now.isoformat()}' as timestamptz)) at time zone '{zone}'", ()
        return statement, parameters

    # Inject an exact absolute sample in PostgreSQL. Ordinary end-to-end tests
    # above use the real clock for both admission and sealing.
    event.listen(engine, "before_cursor_execute", clock, retval=True)
    try:
        with pytest.raises(VerificationInputError, match="before-verification-maturity"):
            enqueue_forward_verification(store, tmp_path, row["id"], key=VERIFY_KEY)
        now = maturity
        queued = enqueue_forward_verification(store, tmp_path, row["id"], key=VERIFY_KEY)
        attempt, task = _execute(store, tmp_path, queued)
    finally:
        event.remove(engine, "before_cursor_execute", clock)
    assert attempt["staging_at"] == attempt["sealed_at"] == maturity
    assert task["status"] == "succeeded"


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        ("unsealed", "invalid-sealed-score"),
        ("bad-score-digest", "invalid-sealed-score"),
        ("bad-candidate", "invalid-sealed-score"),
        ("score-binding", "invalid-sealed-score"),
        ("label-binding", "verification-binding-mismatch"),
        ("label-file", "invalid-label-snapshot"),
        ("label-manifest", "invalid-label-snapshot"),
    ],
)
def test_invalid_bound_evidence_ends_queue_without_a_result(engine, tmp_path, damage, reason):
    store, row, score = _registered(engine, tmp_path)
    leaf = _publish_labels(tmp_path, row, "label-version")
    queued = enqueue_forward_verification(store, tmp_path, row["id"], key=VERIFY_KEY)
    with engine.begin() as connection:
        if damage in {"unsealed", "bad-score-digest", "bad-candidate"}:
            update = forward_attempt.update().where(forward_attempt.c.id == score["attempt_id"])
            if damage == "bad-candidate":
                evidence = connection.scalar(
                    select(forward_attempt.c.evidence).where(forward_attempt.c.id == score["attempt_id"])
                )
                evidence["candidate_codes"] = [CODES[1]]
                connection.execute(update.values(evidence=evidence, evidence_sha256=canonical_sha256(evidence)))
            else:
                values = {"sealed_at": None} if damage == "unsealed" else {"evidence_sha256": "0" * 64}
                connection.execute(update.values(**values))
        if damage == "score-binding":
            connection.execute(forward_registration.update().values(score_snapshot={}))
        if damage == "label-binding":
            execution = connection.scalar(
                select(research_task.c.execution).where(research_task.c.id == queued["task_id"])
            )
            execution["label_binding_sha256"] = "0" * 64
            connection.execute(
                research_task.update().where(research_task.c.id == queued["task_id"]).values(execution=execution)
            )
    if damage in {"label-file", "label-manifest"}:
        file = "market.parquet" if damage == "label-file" else "manifest.json"
        with (leaf / file).open("ab") as stream:
            stream.write(b"bad")
    if reason == "invalid-sealed-score":
        with pytest.raises(VerificationInputError, match=reason):
            enqueue_forward_verification(store, tmp_path, row["id"], key=VERIFY_KEY + "-invalid")
        assert store.list()["total"] == 2
    attempt, task = _execute(store, tmp_path, queued)
    assert task["status"] == attempt["status"] == "failed"
    assert task["error"] == attempt["error"] == reason
    assert attempt["sealed_at"] is None
    assert _stored_registration(engine)["result"] is None


@pytest.mark.parametrize("interrupted", [False, True], ids=["cancel", "recover"])
def test_retry_keeps_the_first_binding_after_a_new_version(engine, tmp_path, interrupted):
    store, row, _ = _registered(engine, tmp_path)
    first = _publish_labels(tmp_path, row, "first-available", seconds=60)
    original = enqueue_forward_verification(store, tmp_path, row["id"], key=VERIFY_KEY)
    if interrupted:
        with worker_lock(engine):
            assert store.claim() == original["task_id"]
            store.recover()
    else:
        store.cancel(original["task_id"])
    _publish_labels(tmp_path, row, "newly-published-with-earlier-capture", zero=True)
    assert enqueue_forward_verification(store, tmp_path, row["id"], key=VERIFY_KEY) == original
    retry = enqueue_forward_verification(store, tmp_path, row["id"], key=VERIFY_KEY + "-retry")
    attempt, task = _execute(store, tmp_path, retry)
    assert task["status"] == attempt["status"] == "succeeded"
    assert attempt["attempt_no"] == 2 and attempt["evidence"]["label_snapshot"]["data_version"] == first.name
    assert _stored_registration(engine)["result"]["excess_return_pp"] == pytest.approx(13.333333333)


def test_lost_response_keeps_already_sealed_verification(engine, tmp_path, monkeypatch):
    store, row, _ = _registered(engine, tmp_path)
    _publish_labels(tmp_path, row, "label-version")
    queued = enqueue_forward_verification(store, tmp_path, row["id"], key=VERIFY_KEY)
    confirm = forward_verification.confirm_attempt

    def lose_response(*args, **kwargs):
        assert confirm(*args, **kwargs).sealed
        raise ConnectionError("synthetic loss after successful COMMIT")

    monkeypatch.setattr(forward_verification, "confirm_attempt", lose_response)
    attempt, task = _execute(store, tmp_path, queued)
    assert task["status"] == attempt["status"] == "succeeded"
    assert _stored_registration(engine)["result"]["excess_return_pp"] == pytest.approx(13.333333333)


@pytest.mark.parametrize("damage", ["attempt-digest", "task-status", "task-result", "task-binding"])
def test_score_evidence_invalidated_after_worker_read_prevents_seal(engine, tmp_path, monkeypatch, damage):
    store, row, score = _registered(engine, tmp_path)
    _publish_labels(tmp_path, row, "label-version")
    queued = enqueue_forward_verification(store, tmp_path, row["id"], key=VERIFY_KEY)
    confirm = forward_verification.confirm_attempt

    def invalidate_then_confirm(*args, **kwargs):
        with engine.begin() as connection:
            if damage == "attempt-digest":
                connection.execute(
                    forward_attempt.update()
                    .where(forward_attempt.c.id == score["attempt_id"])
                    .values(evidence_sha256="0" * 64)
                )
            else:
                update = research_task.update().where(research_task.c.id == score["task_id"])
                if damage == "task-status":
                    connection.execute(update.values(status="failed"))
                elif damage == "task-result":
                    connection.execute(
                        update.values(result={"registration_id": row["id"], "score_attempt_id": "other"})
                    )
                else:
                    execution = connection.scalar(
                        select(research_task.c.execution).where(research_task.c.id == score["task_id"])
                    )
                    connection.execute(update.values(execution={**execution, "binding_sha256": "0" * 64}))
        return confirm(*args, **kwargs)

    monkeypatch.setattr(forward_verification, "confirm_attempt", invalidate_then_confirm)
    attempt, task = _execute(store, tmp_path, queued)
    assert task["status"] == attempt["status"] == "failed"
    assert task["error"] == attempt["error"] == "verification-evidence-changed"
    assert attempt["staging_at"] is not None and attempt["sealed_at"] is None
    assert attempt["evidence"]["score_attempt_id"] == score["attempt_id"]
    assert attempt["evidence"]["score_task_id"] == score["task_id"]
    assert canonical_sha256(attempt["evidence"]) == attempt["evidence_sha256"]
    saved = _stored_registration(engine)
    assert saved["result"] is saved["result_recorded_at"] is None
    assert saved["label_snapshot"] == attempt["evidence"]["label_snapshot"]


def test_concurrent_first_submission_replays_one_attempt_and_binding(engine, tmp_path):
    store, row, _ = _registered(engine, tmp_path)
    label = _publish_labels(tmp_path, row, "label-version")

    def submit():
        return enqueue_forward_verification(store, tmp_path, row["id"], key=VERIFY_KEY)

    with ThreadPoolExecutor(max_workers=2) as workers:
        left, right = workers.submit(submit), workers.submit(submit)
        assert left.result(timeout=15) == right.result(timeout=15)
    assert store.list()["total"] == 2
    assert _stored_registration(engine)["label_snapshot"]["data_version"] == label.name


def test_queue_limit_rolls_back_the_first_label_binding(engine, tmp_path):
    store, row, _ = _registered(engine, tmp_path)
    _publish_labels(tmp_path, row, "label-version")
    for index in range(20):
        store.create({"name": "ordinary queued task"}, {"mode": "existing"}, f"ordinary-task-{index}")
    with pytest.raises(TaskConflict, match="20"):
        enqueue_forward_verification(store, tmp_path, row["id"], key=VERIFY_KEY)
    assert _stored_registration(engine)["label_snapshot"] is None
    with engine.connect() as connection:
        assert connection.scalar(select(forward_attempt.c.id).where(forward_attempt.c.kind == "verify")) is None
