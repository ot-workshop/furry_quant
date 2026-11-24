"""Submit the existing forward workflows under the shared maintenance lock."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from os import scandir
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import Connection, Engine, func, select

from finance_quant.application.forward_score import (
    ScoreInputError,
    _capture_time,
    _frozen_schedule,
    enqueue_forward_score,
)
from finance_quant.application.forward_verification import enqueue_forward_verification
from finance_quant.data.market import SCORE_SNAPSHOT_LOCATION, validate_score_snapshot_identity
from finance_quant.persistence.forward import (
    attempt_for_submission_key,
    find_attempt_replay,
    registration_row_confirmed,
)
from finance_quant.persistence.schema import forward_registration
from finance_quant.persistence.tasks import PgTaskStore
from finance_quant.task_contracts import TaskConflict, submission_fingerprint
from finance_quant.web.forward_writes import _regular_file
from finance_quant.web.write_requests import research_write_gate

_SNAPSHOT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
_PUBLISHED_SCORE_ID = re.compile(r"forward-score-[0-9a-f]{64}")


class _SubmissionConnection:
    """Borrow the gated session for the enqueue services' existing transaction scopes."""

    def __init__(self, connection: Connection):
        self.connection = connection

    @contextmanager
    def connect(self):
        try:
            yield self.connection
        finally:
            self.connection.rollback()

    @contextmanager
    def begin(self):
        with self.connection.begin():
            yield self.connection


def _registration(connection: Connection, identifier: str) -> dict:
    table = forward_registration.c
    row = (
        connection.execute(select(table.id, table.name, table.score_snapshot).where(table.id == identifier))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(404, "前瞻登记不存在")
    return dict(row)


def _unique_manifest_fields(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate manifest field")
        result[key] = value
    return result


def _published_score_manifests(data_root: Path) -> Iterator[dict]:
    """Inspect only the internal publication directory and bound metadata reads."""
    root = data_root.resolve()
    catalog = root.joinpath(*SCORE_SNAPSHOT_LOCATION)
    try:
        resolved = catalog.resolve(strict=True)
    except FileNotFoundError:
        return
    if resolved != catalog:
        raise ValueError("Invalid score catalog location")

    per_manifest = 1024 * 1024
    remaining = 32 * per_manifest
    with scandir(catalog) as entries:
        for index, entry in enumerate(entries):
            if index >= 4096:
                raise ValueError("Score catalog scan limit exceeded")
            if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                continue
            leaf = catalog / entry.name
            try:
                marker = leaf / "rejected.json"
                if leaf.resolve(strict=True) != leaf or marker.exists() or marker.is_symlink():
                    continue
                with _regular_file(leaf / "manifest.json").open("rb") as stream:
                    payload = stream.read(min(per_manifest, remaining) + 1)
            except (FileNotFoundError, ValueError):
                continue
            remaining -= len(payload)
            if remaining < 0 or len(payload) > per_manifest:
                raise ValueError("Score catalog metadata limit exceeded")

            try:
                manifest = json.loads(payload, object_pairs_hook=_unique_manifest_fields)
                if not isinstance(manifest, dict):
                    continue
                validate_score_snapshot_identity(leaf.relative_to(root), manifest)
                files = manifest.get("files")
                if not isinstance(files, dict) or set(files) != {
                    "market.parquet",
                    "benchmark.parquet",
                    "calendar.parquet",
                }:
                    continue
                if any(
                    not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    for digest in files.values()
                ):
                    continue
                for name in files:
                    _regular_file(leaf / name)
            except (FileNotFoundError, ValueError, RecursionError):
                continue
            yield manifest


def registration_score_options(connection: Connection, data_root: Path, identifier: str) -> dict:
    saved = (
        connection.execute(select(forward_registration).where(forward_registration.c.id == identifier))
        .mappings()
        .one_or_none()
    )
    if saved is None:
        raise HTTPException(404, "前瞻登记不存在")
    now = connection.scalar(select(func.clock_timestamp()))
    assert isinstance(now, datetime)
    row = dict(saved)
    try:
        if not registration_row_confirmed(row) or row["confirmed_at"] > now:
            return {"bound": None, "items": []}
        _, deadline = _frozen_schedule(row)
    except (KeyError, TypeError, ValueError, AttributeError):
        return {"bound": None, "items": []}
    bound = row["score_snapshot"]
    if isinstance(bound, dict):
        version = bound.get("data_version")
        if isinstance(version, str) and _SNAPSHOT_ID.fullmatch(version):
            return {"bound": version, "items": []}
        return {"bound": None, "items": []}
    if now >= deadline:
        return {"bound": None, "items": []}

    items = []
    for manifest in _published_score_manifests(data_root):
        capture = manifest["score_capture"]
        if capture.get("source_dataset_id") != row["dataset_id"] or manifest.get("codes") != row["pool_codes"]:
            continue
        try:
            captured = _capture_time(row, manifest, deadline, now)
        except ScoreInputError:
            continue
        items.append({"id": manifest["id"], "data_end": row["signal_date"], "captured_at": captured})
    return {"bound": None, "items": sorted(items, key=lambda item: (item["captured_at"], item["id"]), reverse=True)}


def submit_forward_score(engine: Engine, data_root: Path, identifier: str, payload: object, key: str) -> dict:
    if not isinstance(payload, dict) or payload.keys() != {"score_snapshot_id"}:
        raise ValueError("评分提交必须指定一个已发布的评分快照标识")
    snapshot_id = payload["score_snapshot_id"]
    if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError("评分快照标识无效")
    submission_fingerprint("research", {}, key)
    with research_write_gate(engine) as connection:
        scoped = _SubmissionConnection(connection)
        with scoped.connect() as read:
            registration = _registration(read, identifier)
            attempt_for_submission_key(read, identifier, "score", key)
        bound = registration["score_snapshot"]
        if isinstance(bound, dict):
            if bound.get("data_version") != snapshot_id:
                raise TaskConflict("score-snapshot-already-bound")
        elif not _PUBLISHED_SCORE_ID.fullmatch(snapshot_id):
            raise ScoreInputError("首次评分须使用已发布的受管评分快照")
        store = PgTaskStore(engine=scoped)
        try:
            return enqueue_forward_score(store, data_root, identifier, snapshot_id, key=key)
        except (ScoreInputError, TaskConflict):
            raise
        except ValueError:
            if bound is None:
                # A concurrent submission can commit while the first snapshot lookup is failing.
                # The original transaction is closed. Only replay a committed pair in a fresh read.
                request = {
                    "name": registration["name"],
                    "registration_id": identifier,
                    "score_snapshot_id": snapshot_id,
                }
                with scoped.connect() as read:
                    replay = find_attempt_replay(store, read, identifier, "score", key, request)
                if replay is not None:
                    return replay
            raise


def submit_forward_verification(
    engine: Engine, data_root: Path, identifier: str, payload: object, key: str
) -> dict | None:
    if payload != {}:
        raise ValueError("到期验证提交不接受额外参数")
    submission_fingerprint("research", {}, key)
    with research_write_gate(engine) as connection:
        scoped = _SubmissionConnection(connection)
        with scoped.connect() as read:
            _registration(read, identifier)
            attempt_for_submission_key(read, identifier, "verify", key)
        return enqueue_forward_verification(PgTaskStore(engine=scoped), data_root, identifier, key=key)
