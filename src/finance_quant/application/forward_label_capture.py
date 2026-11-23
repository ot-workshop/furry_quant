"""Acquire and atomically publish labels for a mature forward registration."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pandas as pd
from filelock import FileLock
from sqlalchemy import Engine, select

from finance_quant.application.forward_score import _frozen_schedule
from finance_quant.application.forward_score_capture import (
    _check_window,
    _database_now,
    _ForwardCapture,
    _publication_root,
    _read_published,
    _registered_source,
)
from finance_quant.application.forward_verification import (
    VerificationInputError,
    _capture_at,
    _label_requests,
    _maturity,
)
from finance_quant.checksums import sha256_bytes
from finance_quant.data.catalog import Catalog
from finance_quant.data.managed import atomic_json, provider_identifier
from finance_quant.data.market import (
    LABEL_SNAPSHOT_LOCATION,
    LABEL_SNAPSHOT_TYPE,
    _load_leaf,
    label_snapshot_leaf_name,
    managed_label_snapshot_id,
    validate_label_snapshot_identity,
    write_market_snapshot_files,
)
from finance_quant.persistence.forward import canonical_sha256, registration_row_confirmed
from finance_quant.persistence.schema import forward_registration
from finance_quant.task_contracts import submission_fingerprint


def _capture_labels(fetch: _ForwardCapture, row: dict, sessions: tuple, requests: list[tuple[str, dict]]) -> tuple:
    (_, calendar_params), (_, benchmark_params), *quotes = requests
    calendar = fetch.capture("trade_cal", **calendar_params)
    signal, entry, exit_ = (row[f"{stage}_date"] for stage in ("signal", "entry", "exit"))
    expected = pd.DatetimeIndex([day for day in sessions if signal <= day <= exit_])
    if not pd.DatetimeIndex(calendar.date).equals(pd.date_range(signal, exit_)) or not pd.DatetimeIndex(
        calendar.loc[calendar.is_open, "date"]
    ).equals(expected):
        raise VerificationInputError("label-capture-calendar-mismatch")

    benchmark = fetch.capture("index_daily", **benchmark_params)
    label_dates = pd.DatetimeIndex([entry, exit_])
    if not pd.DatetimeIndex(benchmark.date).equals(label_dates):
        raise VerificationInputError("label-capture-benchmark-calendar-mismatch")

    frames = []
    for (_, daily_params), (_, adjustment_params) in zip(quotes[::2], quotes[1::2], strict=True):
        daily = fetch.capture("daily", **daily_params)
        adjusted = fetch.capture("adj_factor", **adjustment_params)
        if not daily.date.isin(label_dates).all() or not adjusted.date.isin(label_dates).all():
            raise VerificationInputError("label-capture-quote-calendar-mismatch")
        active = daily.loc[daily.volume.gt(0) & daily.amount.gt(0), ["instrument", "date", "open", "close"]]
        # A genuinely empty response or a missing adjustment leaves that stock/date absent.
        # Positive turnover is required before recording an active trading state.
        frames.append(active.merge(adjusted, on=["instrument", "date"], validate="one_to_one"))
    nonempty = [frame for frame in frames if not frame.empty]
    market = pd.concat(nonempty, ignore_index=True) if nonempty else frames[0]
    return market.assign(is_suspended=False), benchmark, calendar


def _published_labels(leaf: Path, name: str, row: dict, provider_id: str, mature_at: datetime, now: datetime) -> dict:
    manifest, payload = _read_published(leaf, "label")
    validate_label_snapshot_identity(Path(*LABEL_SNAPSHOT_LOCATION, name), manifest)
    if manifest.get("provider_id") != provider_id:
        raise VerificationInputError("label-publication-conflict")
    captured = _capture_at(row, manifest, mature_at, now)
    market, _, _, _ = _load_leaf(leaf, manifest)
    return {
        "id": manifest["id"],
        "captured_at": captured.isoformat(),
        "manifest_sha256": sha256_bytes(payload),
        "files": manifest["files"],
        "rows": len(market),
    }


def publish_forward_label_snapshot(
    engine: Engine,
    data_root: Path,
    registration_id: str,
    *,
    storage_root: Path,
    provider: Any,
    key: str,
) -> dict:
    """Use fresh observations from the registered source to publish the frozen entry/exit labels."""
    operation = submission_fingerprint("forward-label-capture", {"registration_id": registration_id}, key)
    publication_id = "flc-" + canonical_sha256({"operation": operation, "key": key})[:24]
    table = forward_registration
    with engine.connect() as connection:
        saved = connection.execute(select(table).where(table.c.id == registration_id)).mappings().one_or_none()
    if saved is None or not registration_row_confirmed(saved):
        raise VerificationInputError("registration-unsealed")
    row = dict(saved)
    sessions, _ = _frozen_schedule(row)
    mature_at = _maturity(row)
    provider_id = provider_identifier(provider)
    root = _publication_root(data_root, LABEL_SNAPSHOT_LOCATION, "label")
    with FileLock(root / f".{publication_id}.lock"):
        completed = list(root.glob(f"{publication_id}-*"))
        if len(completed) > 1:
            raise VerificationInputError("label-publication-conflict")
        if completed:
            target = completed[0]
            return _published_labels(target, target.name, row, provider_id, mature_at, _database_now(engine))
        _check_window(_database_now(engine), mature_at, None)
        synthetic = _registered_source(engine, storage_root.resolve(), row, provider_id, phase="label")
        requests = _label_requests(row)
        with TemporaryDirectory(prefix=".label-", dir=root) as temporary:
            stage = Path(temporary)
            raw = stage / "capture"
            catalog = Catalog(raw)
            try:
                catalog.begin_job(publication_id, {"registration_id": registration_id}, len(requests))
                fetch = _ForwardCapture(provider, raw, catalog, publication_id, engine, mature_at, None)
                market, benchmark, calendar = _capture_labels(fetch, row, sessions, requests)
                captured = max(datetime.fromisoformat(item["response_received_at"]) for item in fetch.receipts)
                catalog.finish(publication_id, "complete", {"captured_at": captured.isoformat()})
            finally:
                catalog.close()

            leaf = stage / "payload"
            leaf.mkdir()
            files = write_market_snapshot_files(leaf, market, benchmark, calendar)
            manifest = {
                "schema_version": "market-v2",
                "publication_type": LABEL_SNAPSHOT_TYPE,
                "provider_id": provider_id,
                "codes": row["pool_codes"],
                "is_synthetic": synthetic or getattr(provider, "settings", None) is None,
                "files": files,
                "label_capture": {
                    "captured_at": captured.isoformat(),
                    "data_end": row["exit_date"].isoformat(),
                    "pool_codes": row["pool_codes"],
                    "source_dataset_id": row["dataset_id"],
                    "registration_id": registration_id,
                    "registration_sha256": row["confirmed_evidence_sha256"],
                    "calendar_sha256": row["calendar_fingerprint"],
                    "publication_id": publication_id,
                    "requests": fetch.receipts,
                },
            }
            manifest["id"] = managed_label_snapshot_id(manifest)
            target = root / label_snapshot_leaf_name(manifest)
            atomic_json(leaf / "manifest.json", manifest)
            proof = _published_labels(leaf, target.name, row, provider_id, mature_at, _database_now(engine))
            leaf.rename(target)
    return proof
