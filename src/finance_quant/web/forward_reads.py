"""Read-side projections of persisted forward registration and seal evidence."""

from __future__ import annotations

import math
import re
from datetime import datetime, time

from sqlalchemy import Connection, func, select

from finance_quant.application.forward_score import _frozen_schedule
from finance_quant.application.forward_verification import (
    VerificationInputError,
    _is_number,
    _maturity,
    _score_candidates,
)
from finance_quant.persistence.forward import (
    REGISTRATION_SUMMARY_FIELDS,
    SHANGHAI,
    _matches_evidence,
    attempt_sealed,
    canonical_sha256,
    get_registration,
    list_registrations,
    registration_row_confirmed,
    score_task_matches,
)
from finance_quant.persistence.schema import forward_registration, research_task
from finance_quant.persistence.tasks import TASK_ID_PATTERN
from finance_quant.task_contracts import FORWARD_VERIFY_MODE

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_SCORE_REASONS = ("missing-warmup", "missing-price", "missing-trading-state", "nonfinite-momentum")
_LABEL_REASONS = ("nonfinite-return",) + tuple(
    f"{phase}-{reason}"
    for phase in ("entry", "exit")
    for reason in ("missing-quote", "missing-trading-state", "suspended", "missing-price")
)
_ATTEMPT_ERRORS = (
    "deadline-passed",
    "incomplete-score",
    "invalid-score-snapshot",
    "invalid-label-snapshot",
    "invalid-sealed-score",
    "score-execution-failed",
    "verification-execution-failed",
    "score-binding-mismatch",
    "verification-binding-mismatch",
    "verification-evidence-changed",
)


def _token(value: object, pattern: re.Pattern[str] = _IDENTIFIER) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _summary(row: dict) -> dict:
    return {key: row[key] for key in REGISTRATION_SUMMARY_FIELDS}


def registration_page(connection: Connection, *, offset: int, limit: int) -> dict:
    return {
        "items": [_summary(row) for row in list_registrations(connection, offset=offset, limit=limit)],
        "total": connection.scalar(select(func.count()).select_from(forward_registration)),
        "offset": offset,
        "limit": limit,
    }


def _public_attempt(attempt: dict, sealed: bool) -> dict:
    reason = attempt["error"]
    return {
        **{key: attempt[key] for key in ("id", "kind", "attempt_no", "status", "deadline_at", "created_at")},
        # The nullable foreign key guarantees existence. The task GET also requires this ID format.
        "task_id": _token(attempt["task_id"], TASK_ID_PATTERN),
        "sealed": sealed,
        "sealed_at": attempt["sealed_at"] if sealed else None,
        "evidence_sha256": _token(attempt["evidence_sha256"], _DIGEST) if sealed else None,
        "reason": reason if reason in _ATTEMPT_ERRORS else "attempt-failed" if attempt["status"] == "failed" else None,
    }


def _version(binding: object) -> str | None:
    return _token(binding.get("data_version")) if isinstance(binding, dict) else None


def _missing_score(row: dict, attempt: dict | None) -> list[dict]:
    if (
        attempt is None
        or attempt["status"] != "failed"
        or attempt["error"] != "incomplete-score"
        or not isinstance(attempt["evidence"], dict)
        or not _matches_evidence(attempt, attempt["evidence_sha256"])
    ):
        return []
    evidence = attempt["evidence"]
    scores = evidence.get("scores")
    if (
        evidence.get("registration_id") != row["id"]
        or evidence.get("registration_sha256") != row["confirmed_evidence_sha256"]
        or evidence.get("score_snapshot") != row["score_snapshot"]
        or not isinstance(scores, list)
        or not isinstance(row["pool_codes"], list)
    ):
        return []
    missing = []
    for score in scores:
        if (
            isinstance(score, dict)
            and score.get("code") in row["pool_codes"]
            and isinstance(score.get("reasons"), list)
        ):
            reasons = [value for value in _SCORE_REASONS if value in score["reasons"]]
            if reasons:
                missing.append({"code": score["code"], "reasons": reasons})
    return missing


def _checked_label_return(label: dict, reasons: list[str]) -> float | None:
    opening, closing = label["entry_adjusted_open"], label["exit_adjusted_open"]
    if any(price is not None and (not _is_number(price) or price <= 0) for price in (opening, closing)):
        raise VerificationInputError("invalid-result-labels")
    value = label["return"]
    if opening is None or closing is None:
        missing_sides = [side for side, price in (("entry", opening), ("exit", closing)) if price is None]
        if value is None and [reason.split("-", 1)[0] for reason in reasons] == missing_sides:
            return None
    else:
        expected = float(closing) / float(opening) - 1
        if _is_number(expected):
            if not reasons and _is_number(value) and value == expected:
                return expected
        elif value is None and reasons == ["nonfinite-return"]:
            return None
    raise VerificationInputError("invalid-result-labels")


def _public_metrics(row: dict, result: dict, candidates: list[str]) -> dict:
    labels, codes = result.get("labels"), row["pool_codes"]
    if not isinstance(labels, list) or len(labels) != len(codes):
        raise VerificationInputError("invalid-result-labels")
    returns = {}
    missing = []
    for code, label in zip(codes, labels, strict=True):
        if not isinstance(label, dict) or label.get("code") != code or "return" not in label:
            raise VerificationInputError("invalid-result-labels")
        reasons = label.get("reasons")
        if not isinstance(reasons, list) or any(reason not in _LABEL_REASONS for reason in reasons):
            raise VerificationInputError("invalid-result-labels")
        value = _checked_label_return(label, reasons)
        if value is None:
            missing.append({"code": code, "reasons": reasons})
        else:
            returns[code] = value
    top_k, pool_size = row["top_k"], row["pool_size"]
    candidate_valid = sum(code in returns for code in candidates)
    candidate_return = math.fsum(returns[code] / top_k for code in candidates) if candidate_valid == top_k else None
    pool_return = math.fsum(item / pool_size for item in returns.values()) if len(returns) == pool_size else None
    excess = (
        100 * (candidate_return - pool_return) if candidate_return is not None and pool_return is not None else None
    )
    if excess is not None and not _is_number(excess):
        raise VerificationInputError("invalid-result-metrics")
    metrics = {
        "candidate_valid": candidate_valid,
        "pool_valid": len(returns),
        "candidate_return": candidate_return,
        "pool_return": pool_return,
        "excess_return_pp": excess,
    }
    expected = {
        **metrics,
        "top_k": row["top_k"],
        "pool_size": row["pool_size"],
        "candidate_codes": candidates,
        "missing_codes": [item["code"] for item in missing],
        "entry_date": row["entry_date"].isoformat(),
        "exit_date": row["exit_date"].isoformat(),
    }
    # Canonical comparison also distinguishes a malformed false from numerical zero.
    if canonical_sha256({key: result[key] for key in expected}) != canonical_sha256(expected):
        raise VerificationInputError("invalid-result-metrics")
    return {**metrics, "candidate_codes": candidates, "missing": missing}


def _score_proof(connection: Connection, row: dict, score: dict, deadline: datetime, now: datetime) -> list[str]:
    evidence, binding = score["evidence"], row["score_snapshot"]
    if (
        not attempt_sealed(score, deadline)
        or score["sealed_at"] > now
        or not isinstance(evidence, dict)
        or not isinstance(binding, dict)
        or not _version(binding)
        or not _token(binding.get("manifest_sha256"), _DIGEST)
        or evidence.get("score_snapshot") != binding
        or evidence.get("registration_id") != row["id"]
        or evidence.get("registration_sha256") != row["confirmed_evidence_sha256"]
        or evidence.get("signal_date") != row["signal_date"].isoformat()
        or not score_task_matches(connection, score, binding)
    ):
        raise VerificationInputError("invalid-sealed-score")
    captured_at = datetime.fromisoformat(binding["captured_at"])
    close = datetime.combine(row["signal_date"], time(15), tzinfo=SHANGHAI)
    if captured_at.utcoffset() is None or not close < captured_at <= score["staging_at"]:
        raise VerificationInputError("invalid-sealed-score")
    return _score_candidates(row, evidence)


def _sealed_result(
    connection: Connection,
    row: dict,
    score: dict,
    verify: dict,
    candidates: list[str],
    mature_at: datetime,
    now: datetime,
) -> dict:
    evidence, binding, result = verify["evidence"], row["label_snapshot"], row["result"]
    if (
        not attempt_sealed(verify, mature_at)
        or verify["sealed_at"] > now
        or row["result_recorded_at"] != verify["sealed_at"]
        or not isinstance(evidence, dict)
        or not isinstance(binding, dict)
        or not isinstance(result, dict)
        or not _version(binding)
        or not _token(binding.get("manifest_sha256"), _DIGEST)
        or evidence.get("registration_sha256") != row["confirmed_evidence_sha256"]
        or evidence.get("score_attempt_id") != score["id"]
        or evidence.get("score_task_id") != score["task_id"]
        or evidence.get("score_evidence_sha256") != score["evidence_sha256"]
        or canonical_sha256(evidence.get("label_snapshot")) != canonical_sha256(binding)
        or canonical_sha256(evidence.get("result")) != canonical_sha256(result)
    ):
        raise VerificationInputError("invalid-sealed-result")
    captured_at = datetime.fromisoformat(binding["captured_at"])
    if captured_at.utcoffset() is None or not mature_at <= captured_at <= verify["staging_at"]:
        raise VerificationInputError("invalid-sealed-result")
    task = research_task.c
    saved = connection.execute(
        select(task.status, task.result, task.execution).where(task.id == verify["task_id"])
    ).one_or_none()
    if (
        saved is None
        or saved.status != "succeeded"
        or saved.result != {"registration_id": row["id"], "verification_attempt_id": verify["id"]}
        or not isinstance(saved.execution, dict)
        or saved.execution.get("mode") != FORWARD_VERIFY_MODE
        or saved.execution.get("label_binding_sha256") != canonical_sha256(binding)
        or saved.execution.get("score_evidence_sha256") != score["evidence_sha256"]
    ):
        raise VerificationInputError("invalid-sealed-result")
    return _public_metrics(row, result, candidates)


def _pending_state(stage: str, attempt: dict | None, idle: str) -> str:
    if attempt is None:
        return idle
    return {
        "queued": f"{stage}_queued",
        "running": "scoring" if stage == "score" else "verifying",
        "failed": "failed",
        "missed": "missed",
    }.get(attempt["status"], idle)


def registration_detail(connection: Connection, registration_id: str) -> dict | None:
    row = get_registration(connection, registration_id, include_evidence=True)
    if row is None:
        return None
    now = connection.scalar(select(func.clock_timestamp()))
    assert isinstance(now, datetime)
    attempts = row["attempts"]
    latest = {attempt["kind"]: attempt for attempt in attempts}
    succeeded = {attempt["kind"]: attempt for attempt in attempts if attempt["status"] == "succeeded"}
    score, verify = succeeded.get("score"), succeeded.get("verify")
    confirmed = False
    sealed_ids: set[str] = set()
    result = None
    score_deadline = mature_at = None
    state = "expired" if row["status"] == "expired" else "unsealed"
    try:
        confirmed = registration_row_confirmed(row) and row["confirmed_at"] <= now
        if confirmed:
            _, score_deadline = _frozen_schedule(row)
            mature_at = _maturity(row)
            if score is None:
                if verify is not None or row["result"] is not None or row["result_recorded_at"] is not None:
                    raise VerificationInputError("invalid-sealed-result")
                state = _pending_state(
                    "score", latest.get("score"), "missed" if now >= score_deadline else "awaiting_score"
                )
            else:
                candidates = _score_proof(connection, row, score, score_deadline, now)
                sealed_ids.add(score["id"])
                if verify is not None:
                    result = _sealed_result(connection, row, score, verify, candidates, mature_at, now)
                    sealed_ids.add(verify["id"])
                    state = "verified" if result["excess_return_pp"] is not None else "missing_labels"
                elif row["result"] is not None or row["result_recorded_at"] is not None:
                    raise VerificationInputError("invalid-sealed-result")
                elif now < mature_at:
                    state = "awaiting_exit"
                else:
                    idle = "awaiting_verification" if row["label_snapshot"] is not None else "awaiting_quote"
                    state = _pending_state("verify", latest.get("verify"), idle)
        elif state != "expired" and (row["confirmed_at"] is not None or row["result"] is not None or succeeded):
            state = "invalid_evidence"
    except (AttributeError, KeyError, ValueError, TypeError, OverflowError):
        state = "invalid_evidence"

    strategy = row["strategy_rules"]
    holding_sessions = strategy.get("holding_sessions") if isinstance(strategy, dict) else None
    return {
        **_summary(row),
        "state": state,
        "pool_codes": row["pool_codes"] if confirmed else None,
        "holding_sessions": holding_sessions if type(holding_sessions) is int and holding_sessions > 0 else None,
        "confirmed": confirmed,
        "confirmed_at": row["confirmed_at"] if confirmed else None,
        "score_deadline_at": score_deadline,
        "matures_at": mature_at,
        "versions": {
            "calendar": _token(row["calendar_version"]),
            "source_dataset": _token(row["dataset_id"]),
            "score_snapshot": _version(row["score_snapshot"]),
            "label_snapshot": _version(row["label_snapshot"]),
        },
        "audit": {
            "registration_sha256": _token(row["confirmed_evidence_sha256"], _DIGEST) if confirmed else None,
            "calendar_sha256": _token(row["calendar_fingerprint"], _DIGEST) if confirmed else None,
            "code_sha256": _token(row["code_sha256"], _DIGEST) if confirmed else None,
            "dependency_lock_sha256": _token(row["data_rules"].get("dependency_lock_sha256"), _DIGEST)
            if confirmed
            else None,
        },
        "attempts": [_public_attempt(attempt, attempt["id"] in sealed_ids) for attempt in attempts],
        "score_missing": _missing_score(row, latest.get("score")) if confirmed and state != "invalid_evidence" else [],
        "result": result,
    }
