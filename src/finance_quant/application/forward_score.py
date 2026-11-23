"""Internal submission and execution of one fixed-pool momentum score."""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import select, text

from finance_quant.application.research_requests import lookup_snapshot_meta
from finance_quant.data.market import SCORE_SNAPSHOT_PREFIX, load_frozen_snapshot
from finance_quant.data.symbols import to_qlib
from finance_quant.persistence.forward import (
    SHANGHAI,
    canonical_sha256,
    confirm_attempt,
    enqueue_attempt,
    fail_forward_task,
    registration_row_confirmed,
    start_forward_attempt,
)
from finance_quant.persistence.schema import forward_attempt, forward_registration
from finance_quant.persistence.tasks import PgTaskStore, lock_submissions
from finance_quant.provenance import source_provenance
from finance_quant.research.market_factors import calculate_factor_values
from finance_quant.research.registry import FactorConfig, FactorSpec
from finance_quant.task_contracts import FORWARD_SCORE_MODE, TaskConflict

LOGGER = logging.getLogger(__name__)


class ScoreInputError(ValueError):
    """An input cannot produce a usable score. Messages are stable audit reasons."""


def managed_score_snapshot_id(manifest: dict) -> str:
    """Bind a newly published score version to its manifest content."""
    return SCORE_SNAPSHOT_PREFIX + canonical_sha256(
        {key: value for key, value in manifest.items() if key not in {"id", "snapshot_dir"}}
    )


def _frozen_schedule(row: dict) -> tuple[tuple[date, ...], datetime]:
    rules = row["data_rules"]
    signal = row["signal_date"]
    if (
        rules.get("timezone") != "Asia/Shanghai"
        or rules.get("score_data_end") != signal.isoformat()
        or rules.get("score_capture_after") != "signal_close"
        or rules.get("score_requires_trading_state") is not True
    ):
        raise ScoreInputError("frozen-score-rules-mismatch")
    calendar = rules["calendar"]
    if calendar["version"] != row["calendar_version"] or canonical_sha256(calendar) != row["calendar_fingerprint"]:
        raise ScoreInputError("frozen-calendar-mismatch")
    sessions = tuple(date.fromisoformat(day) for day in calendar["trading_dates"])
    if sessions != tuple(sorted(set(sessions))) or signal not in sessions:
        raise ScoreInputError("frozen-calendar-mismatch")
    position = sessions.index(signal)
    if sessions[position + 1 : position + 3] != (row["entry_date"], row["exit_date"]):
        raise ScoreInputError("frozen-calendar-mismatch")
    return sessions, datetime.combine(row["entry_date"], time(9, 15), tzinfo=SHANGHAI)


def _capture_time(row: dict, manifest: dict, deadline: datetime, now: datetime) -> datetime:
    # Explicit capture evidence supplied by the trusted internal snapshot publisher and
    # covered by the pinned manifest hash. Ordinary research snapshots need not have it.
    capture = manifest.get("score_capture")
    if not isinstance(capture, dict) or not isinstance(capture.get("captured_at"), str):
        raise ScoreInputError("missing-score-capture-proof")
    identifier = manifest.get("id", "")
    if identifier.startswith(SCORE_SNAPSHOT_PREFIX) and (
        identifier != managed_score_snapshot_id(manifest)
        or capture.get("registration_id") != row["id"]
        or capture.get("registration_sha256") != row["confirmed_evidence_sha256"]
    ):
        raise ScoreInputError("score-capture-registration-mismatch")
    try:
        captured_at = datetime.fromisoformat(capture["captured_at"])
    except ValueError:
        raise ScoreInputError("invalid-score-capture-time") from None
    signal = row["signal_date"]
    if (
        captured_at.utcoffset() is None
        or not datetime.combine(signal, time(15), tzinfo=SHANGHAI) < captured_at < deadline
        or captured_at > now
    ):
        raise ScoreInputError("invalid-score-capture-time")
    if capture.get("data_end") != signal.isoformat():
        raise ScoreInputError("score-data-end-mismatch")
    if capture.get("pool_codes") != row["pool_codes"]:
        raise ScoreInputError("score-pool-mismatch")
    if row["dataset_id"] is not None:
        # The source dataset and the independently captured snapshot have distinct IDs.
        source = capture.get("source_dataset_id")
        if not isinstance(source, str) or not source.strip():
            raise ScoreInputError("missing-score-source-dataset")
        if source != row["dataset_id"]:
            raise ScoreInputError("score-source-dataset-mismatch")
    return captured_at


def enqueue_forward_score(
    store: PgTaskStore, data_root: Path, registration_id: str, snapshot_id: str, *, key: str
) -> dict:
    """Bind a published snapshot and queue a score on the existing submission lock."""
    table = forward_registration
    query = select(table).where(table.c.id == registration_id)
    with store.engine.connect() as connection:
        existing = connection.execute(query).mappings().one_or_none()
    if existing is None or not registration_row_confirmed(existing):
        raise TaskConflict("registration-unsealed")
    data_root = data_root.resolve()
    # Resolve a first binding outside the shared queue lock. A retry uses the original
    # binding even if its leaf disappeared, so the worker can record that failure.
    metadata = (
        lookup_snapshot_meta(data_root, snapshot_id, allow_score=True) if existing["score_snapshot"] is None else None
    )

    with store.engine.begin() as connection:
        lock_submissions(connection)
        row = dict(connection.execute(query.with_for_update()).mappings().one())
        if not registration_row_confirmed(row):
            raise TaskConflict("registration-unsealed")
        _, deadline = _frozen_schedule(row)
        binding = row["score_snapshot"]
        if binding is None:
            assert metadata is not None
            path, manifest, digest = metadata
            now = connection.scalar(text("select clock_timestamp()"))
            captured_at = _capture_time(row, manifest, deadline, now)
            binding = {
                "snapshot_dir": path.as_posix(),
                "manifest_sha256": digest,
                "data_version": snapshot_id,
                "files": manifest["files"],
                "captured_at": captured_at.isoformat(),
            }
            connection.execute(table.update().where(table.c.id == registration_id).values(score_snapshot=binding))
        if binding["data_version"] != snapshot_id:
            raise TaskConflict("score-snapshot-already-bound")

        attempt = forward_attempt.c
        prior = connection.execute(
            select(attempt.attempt_no, attempt.status, attempt.idempotency_key)
            .where(attempt.registration_id == registration_id, attempt.kind == "score")
            .order_by(attempt.attempt_no.desc())
            .limit(1)
        ).one_or_none()
        if prior is not None and prior.status == "succeeded" and prior.idempotency_key != key:
            # Earlier keys still replay their own original attempt.
            replay = connection.scalar(select(attempt.id).where(attempt.idempotency_key == key))
            if replay is None:
                raise TaskConflict("score-already-sealed")
        return enqueue_attempt(
            store,
            connection,
            registration_id=registration_id,
            kind="score",
            attempt_no=1 if prior is None else prior.attempt_no + 1,
            deadline=deadline,
            task_key=key,
            request={"name": row["name"], "registration_id": registration_id, "score_snapshot_id": snapshot_id},
            execution={"mode": FORWARD_SCORE_MODE, "binding_sha256": canonical_sha256(binding)},
        )


def _market_dates(frame: pd.DataFrame) -> pd.DatetimeIndex:
    dates = pd.DatetimeIndex(frame["date"])
    if dates.hasnans or dates.tz is not None or not dates.equals(dates.normalize()):
        raise ScoreInputError("invalid-exchange-dates")
    return dates.as_unit("ns")


def _price_evidence(section: pd.DataFrame) -> bool:
    fields = ("close", "adj_factor")
    if any(
        field not in section
        or not pd.api.types.is_numeric_dtype(section[field])
        or pd.api.types.is_bool_dtype(section[field])
        for field in fields
    ):
        return False
    prices = section[list(fields)].to_numpy(dtype=float, na_value=np.nan)
    return bool((np.isfinite(prices) & (prices > 0)).all())


def _compute_score(row: dict, sessions: tuple[date, ...], market, benchmark, calendar) -> dict:
    signal = pd.Timestamp(row["signal_date"])
    frozen_dates = pd.DatetimeIndex(sessions)
    dates = _market_dates(calendar)
    opened = calendar["is_open"]
    if not dates.is_unique or opened.isna().any() or not pd.api.types.is_bool_dtype(opened):
        raise ScoreInputError("snapshot-calendar-mismatch")
    comparable = dates[opened & (dates <= signal) & (dates >= frozen_dates[0])].sort_values()
    expected = frozen_dates[(frozen_dates >= dates.min()) & (frozen_dates <= signal)]
    if signal not in comparable or not comparable.equals(expected):
        raise ScoreInputError("snapshot-calendar-mismatch")
    # Compare only future calendar rows the snapshot actually covers. Expected trading
    # sessions and the deadline still come from the frozen registration.
    covered = (dates > signal) & (dates <= pd.Timestamp(row["exit_date"]))
    if (opened[covered].to_numpy(dtype=bool) != dates[covered].isin(frozen_dates)).any():
        raise ScoreInputError("snapshot-calendar-mismatch")
    for frame in (market, benchmark):
        if not frame.empty and not _market_dates(frame).isin(comparable).all():
            raise ScoreInputError("score-data-outside-frozen-calendar")
    if market.duplicated(["instrument", "date"]).any():
        raise ScoreInputError("duplicate-score-market-row")

    codes = row["pool_codes"]
    instruments = {to_qlib(code): code for code in codes}
    if codes != sorted(set(codes)) or len(codes) != row["pool_size"] or not 0 < row["top_k"] < len(codes):
        raise ScoreInputError("frozen-pool-mismatch")
    if not market["instrument"].isin(instruments).all():
        raise ScoreInputError("score-pool-mismatch")
    spec = FactorSpec("momentum", 1.0, row["factor_window"])
    if (
        row["factor"] != spec.name
        or row["strategy_rules"]["factor_parameters"] != spec.parameters
        or row["strategy_rules"]["tie_break"] != "code_ascending"
    ):
        raise ScoreInputError("frozen-factor-mismatch")
    through_signal = frozen_dates[frozen_dates <= signal]
    required = through_signal[-(row["factor_window"] + 1) :]
    scores = []
    complete = []
    grouped = {instrument: section.set_index("date") for instrument, section in market.groupby("instrument")}
    empty = market.iloc[:0].set_index("date")
    for instrument, code in instruments.items():
        section = grouped.get(instrument, empty)
        reasons = []
        if len(required) <= row["factor_window"] or not required[:-1].isin(section.index).all():
            reasons.append("missing-warmup")
        available = section.reindex(required)
        if not _price_evidence(available):
            reasons.append("missing-price")
        state = available.get("is_suspended")
        state_known = state is not None and state.map(lambda value: isinstance(value, (bool, np.bool_))).all()
        if not state_known:
            reasons.append("missing-trading-state")
        signal_state = state.loc[signal] if state is not None else None
        scores.append(
            {
                "code": code,
                "momentum": None,
                "is_suspended": bool(signal_state) if isinstance(signal_state, (bool, np.bool_)) else None,
                "reasons": reasons,
            }
        )
        if not reasons:
            complete.append(available.rename_axis("date").reset_index())

    if complete:
        values, _, _ = calculate_factor_values(pd.concat(complete, ignore_index=True), FactorConfig(specs=(spec,)))
        signal_values = values.loc[values.date.eq(signal)].set_index("instrument")["factor_" + spec.key]
        for score in scores:
            if not score["reasons"]:
                value = float(signal_values.loc[to_qlib(score["code"])])
                if np.isfinite(value):
                    score["momentum"] = value
                else:
                    score["reasons"].append("nonfinite-momentum")

    candidates = None
    if all(not score["reasons"] for score in scores):
        sign = -1 if row["factor_direction"] == "high" else 1
        ranked = sorted(scores, key=lambda score: (sign * score["momentum"], score["code"]))
        candidates = [score["code"] for score in ranked[: row["top_k"]]]
    return {"pool_size": row["pool_size"], "top_k": row["top_k"], "scores": scores, "candidate_codes": candidates}


def run_forward_score(store: PgTaskStore, task: dict, data_root: Path) -> None:
    identifier = task["id"]
    try:
        registration_id = task["request"]["registration_id"]
        with store.engine.begin() as connection:
            attempt = start_forward_attempt(connection, identifier, registration_id, kind="score")
            row = dict(
                connection.execute(select(forward_registration).where(forward_registration.c.id == registration_id))
                .mappings()
                .one()
            )
            now = connection.scalar(text("select clock_timestamp()"))
        if not registration_row_confirmed(row):
            raise ScoreInputError("registration-unsealed")
        sessions, deadline = _frozen_schedule(row)
        if deadline != attempt["deadline_at"]:
            raise ScoreInputError("score-deadline-mismatch")
        binding = row["score_snapshot"]
        if (
            not isinstance(binding, dict)
            or canonical_sha256(binding) != task["execution"]["binding_sha256"]
            or binding["data_version"] != task["request"]["score_snapshot_id"]
        ):
            raise ScoreInputError("score-binding-mismatch")
        provenance = source_provenance()
        if provenance["code_sha256"] != row["code_sha256"]:
            raise ScoreInputError("score-code-mismatch")
        if provenance["dependency_lock_sha256"] != row["data_rules"]["dependency_lock_sha256"]:
            raise ScoreInputError("score-dependency-lock-mismatch")
        try:
            market, benchmark, calendar, manifest = load_frozen_snapshot(data_root, binding).inputs
        except (OSError, ValueError):
            raise ScoreInputError("invalid-score-snapshot") from None
        captured_at = _capture_time(row, manifest, deadline, now)
        if captured_at.isoformat() != binding["captured_at"]:
            raise ScoreInputError("score-binding-mismatch")
        evidence = {
            "registration_id": registration_id,
            "registration_sha256": row["confirmed_evidence_sha256"],
            "signal_date": row["signal_date"].isoformat(),
            "score_snapshot": binding,
            **_compute_score(row, sessions, market, benchmark, calendar),
        }
        if evidence["candidate_codes"] is None:
            fail_forward_task(store, identifier, "incomplete-score", kind="score", evidence=evidence)
            return
        proof = confirm_attempt(
            store,
            attempt_id=attempt["id"],
            evidence=evidence,
            task_result={"registration_id": registration_id, "score_attempt_id": attempt["id"]},
            finish_on_miss=True,
        )
        if not proof.sealed and proof.detail != "deadline-passed":
            fail_forward_task(store, identifier, proof.detail, kind="score")
    except ScoreInputError as error:
        fail_forward_task(store, identifier, str(error), kind="score")
    except Exception as error:
        # Exception arguments from filesystem or DB errors can carry private paths or connection settings.
        if fail_forward_task(store, identifier, "score-execution-failed", kind="score"):
            LOGGER.error("Forward score task %s failed (%s)", identifier, type(error).__name__)
