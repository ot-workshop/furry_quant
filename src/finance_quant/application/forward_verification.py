"""Internal verification of a sealed forward score against its first eligible label capture."""

from __future__ import annotations

import logging
import math
from datetime import datetime, time
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import Connection, select, text

from finance_quant.application.forward_score import ScoreInputError, _frozen_schedule, _market_dates
from finance_quant.application.research_requests import lookup_label_snapshot_metas
from finance_quant.data.market import load_frozen_snapshot
from finance_quant.data.symbols import to_qlib
from finance_quant.persistence.forward import (
    SHANGHAI,
    attempt_sealed,
    canonical_sha256,
    confirm_attempt,
    enqueue_attempt,
    fail_forward_task,
    find_attempt_replay,
    registration_row_confirmed,
    start_forward_attempt,
)
from finance_quant.persistence.schema import forward_attempt, forward_registration, research_task
from finance_quant.persistence.tasks import PgTaskStore, lock_submissions
from finance_quant.task_contracts import FORWARD_SCORE_MODE, FORWARD_VERIFY_MODE, TaskConflict, submission_fingerprint

LOGGER = logging.getLogger(__name__)


class VerificationInputError(ValueError):
    """The supplied evidence is insufficient to verify the registered score."""


def _maturity(row: dict) -> datetime:
    mature_at = datetime.combine(row["exit_date"], time(15), tzinfo=SHANGHAI)
    rules = row["data_rules"]
    if rules.get("label_capture_after") != mature_at.isoformat() or rules.get("label_snapshot") != "first_eligible":
        raise VerificationInputError("frozen-label-rules-mismatch")
    strategy = row["strategy_rules"]
    if (
        strategy.get("holding_sessions") != 1
        or strategy.get("portfolio_weighting") != "equal"
        or strategy.get("benchmark") != "equal_weight_fixed_pool"
        or strategy.get("return_price") != "adjusted_open"
        or strategy.get("main_metric") != "candidate_return_minus_pool_return_pp"
        or strategy.get("fees_included") is not False
    ):
        raise VerificationInputError("frozen-return-rules-mismatch")
    return mature_at


def _is_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _score_candidates(row: dict, evidence: dict) -> list[str]:
    codes, top_k = row["pool_codes"], row["top_k"]
    if codes != sorted(set(codes)) or len(codes) != row["pool_size"] or not 0 < top_k < len(codes):
        raise VerificationInputError("frozen-pool-mismatch")
    scores = evidence.get("scores")
    if (
        not isinstance(scores, list)
        or len(scores) != len(codes)
        or evidence.get("pool_size") != len(codes)
        or evidence.get("top_k") != top_k
    ):
        raise VerificationInputError("invalid-sealed-score")
    for code, score in zip(codes, scores, strict=True):
        to_qlib(code)
        if (
            not isinstance(score, dict)
            or score.get("code") != code
            or score.get("reasons") != []
            or not _is_number(score.get("momentum"))
            or type(score.get("is_suspended")) is not bool
        ):
            raise VerificationInputError("invalid-sealed-score")
    direction = -1 if row["factor_direction"] == "high" else 1
    ranked = sorted(scores, key=lambda score: (direction * score["momentum"], score["code"]))
    candidates = [score["code"] for score in ranked[:top_k]]
    if candidates != evidence.get("candidate_codes"):
        raise VerificationInputError("invalid-sealed-score")
    return candidates


def _read_verification_inputs(connection: Connection, registration_id: str, *, lock: bool = False) -> tuple:
    registration = forward_registration
    query = select(registration).where(registration.c.id == registration_id)
    saved = connection.execute(query.with_for_update() if lock else query).mappings().one_or_none()
    if saved is None or not registration_row_confirmed(saved):
        raise VerificationInputError("registration-unsealed")
    row = dict(saved)
    sessions, score_deadline = _frozen_schedule(row)
    mature_at = _maturity(row)
    attempt = forward_attempt
    score = (
        connection.execute(
            select(attempt).where(
                attempt.c.registration_id == registration_id, attempt.c.kind == "score", attempt.c.status == "succeeded"
            )
        )
        .mappings()
        .one_or_none()
    )
    if score is None or not attempt_sealed(dict(score), score_deadline) or not isinstance(score["evidence"], dict):
        raise VerificationInputError("invalid-sealed-score")
    evidence = score["evidence"]
    queue = connection.execute(
        select(research_task.c.status, research_task.c.result, research_task.c.execution).where(
            research_task.c.id == score["task_id"]
        )
    ).one_or_none()
    binding = row["score_snapshot"]
    if (
        not isinstance(binding, dict)
        or evidence.get("score_snapshot") != binding
        or evidence.get("registration_id") != registration_id
        or evidence.get("registration_sha256") != row["confirmed_evidence_sha256"]
        or evidence.get("signal_date") != row["signal_date"].isoformat()
        or queue is None
        or queue.status != "succeeded"
        or queue.result != {"registration_id": registration_id, "score_attempt_id": score["id"]}
        or queue.execution.get("mode") != FORWARD_SCORE_MODE
        or queue.execution.get("binding_sha256") != canonical_sha256(binding)
    ):
        raise VerificationInputError("invalid-sealed-score")
    try:
        captured = datetime.fromisoformat(binding["captured_at"])
    except (KeyError, TypeError, ValueError):
        raise VerificationInputError("invalid-sealed-score") from None
    signal_close = datetime.combine(row["signal_date"], time(15), tzinfo=SHANGHAI)
    if captured.utcoffset() is None or not signal_close < captured <= score["staging_at"]:
        raise VerificationInputError("invalid-sealed-score")
    candidates = _score_candidates(row, evidence)
    now = connection.execute(text("select clock_timestamp()")).scalar_one()
    if now < mature_at:
        raise VerificationInputError("before-verification-maturity")
    return row, score, candidates, sessions, mature_at, now


def _capture_at(row: dict, manifest: dict, mature_at: datetime, now: datetime) -> datetime:
    capture = manifest.get("label_capture")
    if not isinstance(capture, dict) or not isinstance(capture.get("captured_at"), str):
        raise VerificationInputError("missing-label-capture-proof")
    try:
        captured = datetime.fromisoformat(capture["captured_at"])
    except ValueError:
        raise VerificationInputError("invalid-label-capture-time") from None
    if captured.utcoffset() is None or not mature_at <= captured <= now:
        raise VerificationInputError("invalid-label-capture-time")
    if (
        capture.get("data_end") != row["exit_date"].isoformat()
        or capture.get("pool_codes") != row["pool_codes"]
        or (row["dataset_id"] is not None and capture.get("source_dataset_id") != row["dataset_id"])
    ):
        raise VerificationInputError("label-capture-binding-mismatch")
    return captured


def _load_labels(data_root: Path, binding: dict, row: dict, sessions: tuple, mature_at: datetime, now: datetime):
    try:
        market, _, calendar, manifest = load_frozen_snapshot(data_root, binding).inputs
        if _capture_at(row, manifest, mature_at, now).isoformat() != binding["captured_at"]:
            raise VerificationInputError("label-capture-binding-mismatch")
        dates = _market_dates(calendar)
        opened = calendar["is_open"]
        if not dates.is_unique or opened.isna().any() or not pd.api.types.is_bool_dtype(opened):
            raise VerificationInputError("label-calendar-mismatch")
        signal, entry, exit_ = (pd.Timestamp(row[f"{stage}_date"]) for stage in ("signal", "entry", "exit"))
        relevant = (dates >= signal) & (dates <= exit_)
        known = dates[opened]
        if (
            entry not in known
            or exit_ not in known
            or (opened[relevant].to_numpy(dtype=bool) != dates[relevant].isin(pd.DatetimeIndex(sessions))).any()
        ):
            raise VerificationInputError("label-calendar-mismatch")
        market_dates = _market_dates(market)
        if (market_dates > exit_).any() or not market_dates.isin(known).all():
            raise VerificationInputError("label-market-dates-mismatch")
        if (
            not market["instrument"].isin([to_qlib(code) for code in row["pool_codes"]]).all()
            or market.duplicated(["instrument", "date"]).any()
        ):
            raise VerificationInputError("label-pool-mismatch")
        return market
    except VerificationInputError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise VerificationInputError("invalid-label-snapshot") from None


def _first_labels(data_root: Path, row: dict, sessions: tuple, mature_at: datetime, now: datetime) -> dict | None:
    eligible = []
    for path, manifest, digest in lookup_label_snapshot_metas(data_root):
        try:
            captured = _capture_at(row, manifest, mature_at, now)
        except VerificationInputError:
            continue
        eligible.append(
            (
                captured,
                manifest["id"],
                {
                    "snapshot_dir": path.as_posix(),
                    "manifest_sha256": digest,
                    "data_version": manifest["id"],
                    "files": manifest["files"],
                    "captured_at": captured.isoformat(),
                },
            )
        )
    for _, _, binding in sorted(eligible, key=lambda item: item[:2]):
        try:
            _load_labels(data_root, binding, row, sessions, mature_at, now)
        except VerificationInputError:
            continue
        return binding
    return None


def _verification_request(row: dict, score) -> dict:
    return {"name": row["name"], "registration_id": row["id"], "score_attempt_id": score["id"]}


def enqueue_forward_verification(store: PgTaskStore, data_root: Path, registration_id: str, *, key: str) -> dict | None:
    """Queue a mature score. Return None while awaiting the first qualifying label capture."""
    submission_fingerprint("research", {}, key)
    while True:
        with store.engine.connect() as connection:
            row, score, _, sessions, mature_at, now = _read_verification_inputs(connection, registration_id)
            request = _verification_request(row, score)
            replay = find_attempt_replay(store, connection, registration_id, "verify", key, request)
            if replay is not None:
                return replay
        bound = row["label_snapshot"]
        if bound is None:
            binding = _first_labels(data_root, row, sessions, mature_at, now)
        else:
            _load_labels(data_root, bound, row, sessions, mature_at, now)
            binding = bound
        with store.engine.begin() as connection:
            lock_submissions(connection)
            saved, current_score, _, _, maturity, current_time = _read_verification_inputs(
                connection, registration_id, lock=True
            )
            if (
                saved["confirmed_evidence_sha256"] != row["confirmed_evidence_sha256"]
                or current_score["id"] != score["id"]
                or current_score["evidence_sha256"] != score["evidence_sha256"]
            ):
                raise VerificationInputError("verification-inputs-changed")
            if saved["label_snapshot"] != bound:
                continue  # Another submission pinned the first capture; verify that one outside the lock.
            replay = find_attempt_replay(store, connection, registration_id, "verify", key, request)
            if replay is not None:
                return replay
            attempt = forward_attempt.c
            prior = connection.execute(
                select(attempt.attempt_no, attempt.status)
                .where(attempt.registration_id == registration_id, attempt.kind == "verify")
                .order_by(attempt.attempt_no.desc())
                .limit(1)
            ).one_or_none()
            if prior is not None and prior.status == "succeeded":
                raise TaskConflict("verification-already-sealed")
            if binding is None:
                return None
            if datetime.fromisoformat(binding["captured_at"]) > current_time:
                raise VerificationInputError("invalid-label-capture-time")
            if bound is None:
                connection.execute(
                    forward_registration.update()
                    .where(forward_registration.c.id == registration_id)
                    .values(label_snapshot=binding)
                )
            return enqueue_attempt(
                store,
                connection,
                registration_id=registration_id,
                kind="verify",
                attempt_no=1 if prior is None else prior.attempt_no + 1,
                deadline=maturity,
                task_key=key,
                request=request,
                execution={
                    "mode": FORWARD_VERIFY_MODE,
                    "label_binding_sha256": canonical_sha256(binding),
                    "score_evidence_sha256": score["evidence_sha256"],
                },
            )


def _adjusted_open(quote, phase: str, reasons: list[str]) -> float | None:
    if quote is None:
        reasons.append(f"{phase}-missing-quote")
        return None
    suspended = quote.get("is_suspended", False)
    if not isinstance(suspended, (bool, np.bool_)):
        reasons.append(f"{phase}-missing-trading-state")
        return None
    if suspended:
        reasons.append(f"{phase}-suspended")
        return None
    prices = [quote.get("open"), quote.get("adj_factor")]
    if any(isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, float, np.number)) for value in prices):
        reasons.append(f"{phase}-missing-price")
        return None
    open_price, adjustment = map(float, prices)
    adjusted = open_price * adjustment
    if open_price <= 0 or adjustment <= 0 or not math.isfinite(adjusted) or adjusted <= 0:
        reasons.append(f"{phase}-missing-price")
        return None
    return adjusted


def _returns(row: dict, candidates: list[str], market: pd.DataFrame) -> dict:
    entry, exit_ = pd.Timestamp(row["entry_date"]), pd.Timestamp(row["exit_date"])
    by_key = market.loc[market["date"].isin((entry, exit_))].set_index(["instrument", "date"]).to_dict("index")
    labels = []
    returns = {}
    for code in row["pool_codes"]:
        symbol = to_qlib(code)
        reasons: list[str] = []
        opening = _adjusted_open(by_key.get((symbol, entry)), "entry", reasons)
        closing = _adjusted_open(by_key.get((symbol, exit_)), "exit", reasons)
        value = closing / opening - 1 if opening is not None and closing is not None else None
        if value is not None:
            if math.isfinite(value):
                returns[code] = value
            else:
                reasons.append("nonfinite-return")
                value = None
        labels.append(
            {
                "code": code,
                "entry_adjusted_open": opening,
                "exit_adjusted_open": closing,
                "return": value,
                "reasons": reasons,
            }
        )
    top_k, pool_size = row["top_k"], row["pool_size"]
    candidate_valid = sum(code in returns for code in candidates)
    candidate_return = math.fsum(returns[code] / top_k for code in candidates) if candidate_valid == top_k else None
    pool_return = math.fsum(value / pool_size for value in returns.values()) if len(returns) == pool_size else None
    excess = (
        100 * (candidate_return - pool_return) if candidate_return is not None and pool_return is not None else None
    )
    if excess is not None and not math.isfinite(excess):
        raise VerificationInputError("nonfinite-verification-return")
    return {
        "pool_size": pool_size,
        "top_k": top_k,
        "candidate_codes": candidates,
        "candidate_valid": candidate_valid,
        "pool_valid": len(returns),
        "labels": labels,
        "missing_codes": [label["code"] for label in labels if label["reasons"]],
        "candidate_return": candidate_return,
        "pool_return": pool_return,
        "excess_return_pp": excess,
        "entry_date": row["entry_date"].isoformat(),
        "exit_date": row["exit_date"].isoformat(),
    }


def run_forward_verification(store: PgTaskStore, task: dict, data_root: Path) -> None:
    identifier = task["id"]
    try:
        registration_id = task["request"]["registration_id"]
        with store.engine.begin() as connection:
            attempt = start_forward_attempt(connection, identifier, registration_id, kind="verify")
            row, score, candidates, sessions, mature_at, now = _read_verification_inputs(connection, registration_id)
        binding = row["label_snapshot"]
        if (
            not isinstance(binding, dict)
            or attempt["deadline_at"] != mature_at
            or task["request"]["score_attempt_id"] != score["id"]
            or task["execution"]["score_evidence_sha256"] != score["evidence_sha256"]
            or task["execution"]["label_binding_sha256"] != canonical_sha256(binding)
        ):
            raise VerificationInputError("verification-binding-mismatch")
        market = _load_labels(data_root, binding, row, sessions, mature_at, now)
        evidence = {
            "registration_sha256": row["confirmed_evidence_sha256"],
            "score_attempt_id": score["id"],
            "score_evidence_sha256": score["evidence_sha256"],
            "label_snapshot": binding,
            "result": _returns(row, candidates, market),
        }
        proof = confirm_attempt(
            store,
            attempt_id=attempt["id"],
            evidence=evidence,
            record_registration_result=True,
            task_result={"registration_id": registration_id, "verification_attempt_id": attempt["id"]},
        )
        if not proof.sealed:
            fail_forward_task(store, identifier, proof.detail, kind="verify")
    except (VerificationInputError, ScoreInputError) as error:
        fail_forward_task(store, identifier, str(error), kind="verify")
    except Exception as error:
        if fail_forward_task(store, identifier, "verification-execution-failed", kind="verify"):
            LOGGER.error("Forward verification task %s failed (%s)", identifier, type(error).__name__)
