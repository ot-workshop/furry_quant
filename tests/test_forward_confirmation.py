"""Targeted checks on the inputs required to seal a committed proof."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from finance_quant.persistence import forward

NOW = datetime(2026, 11, 20, 0, 20, tzinfo=timezone.utc)
DEADLINE = NOW + timedelta(minutes=30)
EVIDENCE = {"top_k": 2, "codes": ["600000.SH", "000001.SZ"]}
DIGEST = forward.canonical_sha256(EVIDENCE)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def scalar_one(self):
        return self._rows[0]


class _Connection:
    """Minimal stand-in: returns one attempt row, a fixed clock, and records updates."""

    def __init__(self, row, now):
        self.row = row
        self.now = now
        self.updates = []

    def execute(self, statement, params=None):
        sql = str(statement).lower()
        if sql.startswith("select * from forward_attempt"):
            return _Result([self.row] if self.row else [])
        if "clock_timestamp" in sql:
            return _Result([self.now])
        if sql.startswith("update"):
            self.updates.append(params or {})
            return _Result([])
        return _Result([])


def _row(**overrides):
    row = {
        "id": "fat-1",
        "registration_id": "fwd-1",
        "kind": "score",
        "attempt_no": 1,
        "status": "running",
        "task_id": "task-1",
        "deadline_at": DEADLINE,
        "staging_at": datetime(2026, 11, 20, 0, 10, tzinfo=timezone.utc),
        "sealed_at": None,
        "evidence": EVIDENCE,
        "evidence_row_id": "fat-1",
        "evidence_sha256": DIGEST,
    }
    row.update(overrides)
    return row


def test_seal_succeeds_when_evidence_still_matches_its_digest():
    connection = _Connection(_row(), NOW)
    store = Mock()
    result = forward._seal_success(store, connection, "fat-1", DIGEST, None)
    assert result.sealed is True
    assert result.detail == "sealed"
    store.finish.assert_called_once_with("task-1", "succeeded", result=None, connection=connection)


def test_tampered_evidence_can_never_be_sealed():
    row = _row()
    row["evidence"] = {"top_k": 3, "codes": ["600000.SH"]}  # digest left untouched
    connection = _Connection(row, NOW)
    result = forward._seal_success(Mock(), connection, "fat-1", DIGEST, None)
    assert result.sealed is False
    assert result.detail == "evidence-mismatch"


def test_missing_staging_proof_is_not_a_success():
    connection = _Connection(_row(staging_at=None), NOW)
    result = forward._seal_success(Mock(), connection, "fat-1", DIGEST, None)
    assert result.sealed is False
    assert result.detail == "no-staging-proof"


def test_sampling_exactly_at_the_deadline_marks_the_attempt_missed():
    connection = _Connection(_row(), DEADLINE)
    result = forward._seal_success(Mock(), connection, "fat-1", DIGEST, None)
    assert result.sealed is False
    assert result.detail == "deadline-passed"
