"""Capture and publish one immutable managed input for a sealed forward registration."""

from __future__ import annotations

import json
from datetime import datetime, time
from pathlib import Path
from stat import S_ISREG
from tempfile import TemporaryDirectory
from typing import Any

import numpy as np
import pandas as pd
from filelock import FileLock
from sqlalchemy import Engine, select, text

from finance_quant.application.forward_score import (
    ScoreInputError,
    _capture_time,
    _frozen_schedule,
    managed_score_snapshot_id,
)
from finance_quant.application.quote_capture import _assemble_primary_frame
from finance_quant.application.quote_quality import primary_valid
from finance_quant.checksums import file_sha256, sha256_bytes
from finance_quant.data.catalog import Catalog
from finance_quant.data.managed import ManagedFetcher, atomic_json, coverage_report, provider_identifier
from finance_quant.data.market import _load_leaf, write_market_snapshot_files
from finance_quant.data.symbols import to_qlib
from finance_quant.persistence.forward import SHANGHAI, canonical_sha256, registration_row_confirmed
from finance_quant.persistence.market import snapshot_dataset_record
from finance_quant.persistence.schema import dataset, forward_registration
from finance_quant.task_contracts import submission_fingerprint

_MARKET_APIS = ("daily", "adj_factor", "daily_basic", "stk_limit")
_BENCHMARK = "000300.SH"


def _database_now(engine: Engine) -> datetime:
    with engine.connect() as connection:
        return connection.execute(text("select clock_timestamp()")).scalar_one()


def _check_window(sample: datetime, close: datetime, deadline: datetime) -> None:
    if not close < sample < deadline:
        raise ScoreInputError("outside-score-capture-window")


class _ScoreCapture(ManagedFetcher):
    """Add database-clock observations around actual provider calls to the managed raw ledger."""

    def __init__(self, provider, root: Path, catalog: Catalog, job_id: str, engine: Engine, close, deadline):
        super().__init__(provider, root, catalog, job_id, refresh=False, scope={})
        self.engine, self.close, self.deadline = engine, close, deadline
        self.observed: dict[str, tuple[datetime, datetime]] = {}
        self.receipts: list[dict] = []

    def request(self, api: str, params: dict) -> pd.DataFrame:
        started = _database_now(self.engine)
        _check_window(started, self.close, self.deadline)
        raw = super().request(api, params)
        received = _database_now(self.engine)
        _, key = self.key(api, params)
        self.observed[key] = started, received
        return raw

    def capture(self, api: str, **params) -> pd.DataFrame:
        specification, key = self.key(api, params)
        _check_window(_database_now(self.engine), self.close, self.deadline)
        previous, _ = self.cached_partition(key)
        if previous is not None and previous["status"] == "succeeded":
            raise ScoreInputError("score-capture-reused-cache")
        frame = self.get(api, **params)
        interval = self.observed.pop(key, None)
        if interval is None or frame is None:
            raise ScoreInputError("score-capture-request-failed")
        started, received = interval
        _check_window(received, self.close, self.deadline)
        if received < started:
            raise ScoreInputError("invalid-score-capture-clock")
        ledger = self.catalog.partition(key)
        request = self.requests[-1]
        if (
            ledger is None
            or ledger["status"] != "succeeded"
            or ledger["provider_id"] != self.provider_id
            or ledger["api"] != api
            or json.loads(ledger["params"]) != params
            or any(request.get(field) != ledger[field] for field in ("sha256", "raw_path", "rows", "fetched_at"))
            or not all(request.get(field) == value for field, value in specification.items())
        ):
            raise ScoreInputError("score-capture-ledger-mismatch")
        raw = self.root / ledger["raw_path"]
        receipt = raw.with_suffix(".json")
        try:
            if (
                not raw.is_relative_to(self.root / "raw")
                or any(
                    path.resolve(strict=True) != path or not S_ISREG(path.lstat().st_mode) for path in (raw, receipt)
                )
                or file_sha256(raw) != ledger["sha256"]
                or json.loads(receipt.read_text(encoding="utf-8")) != request
            ):
                raise ScoreInputError("score-capture-receipt-mismatch")
        except (OSError, ValueError) as error:
            raise ScoreInputError("score-capture-receipt-mismatch") from error
        self.receipts.append(
            {
                **specification,
                "raw_sha256": ledger["sha256"],
                "rows": ledger["rows"],
                "request_started_at": started.isoformat(),
                "response_received_at": received.isoformat(),
            }
        )
        return frame


def _registered_source(engine: Engine, storage_root: Path, registration: dict, provider_id: str) -> bool:
    """Resolve the frozen dataset through the ready dataset record, never a provider-supplied label."""
    identifier = registration["dataset_id"]
    if identifier is None:
        return False
    with engine.connect() as connection:
        saved = connection.execute(select(dataset).where(dataset.c.id == identifier)).mappings().one_or_none()
    if saved is None or saved["status"] != "ready" or saved["manifest"].get("kind") != "market_snapshot":
        raise ScoreInputError("score-capture-source-unavailable")
    source = storage_root / saved["relative_path"]
    manifest_path = source / "manifest.json"
    try:
        if (
            not source.is_relative_to(storage_root)
            or source.resolve(strict=True) != source
            or manifest_path.resolve(strict=True) != manifest_path
        ):
            raise ScoreInputError("score-capture-source-unavailable")
        record = snapshot_dataset_record(source, storage_root, request={})
        payload = manifest_path.read_bytes()
        if sha256_bytes(payload) != record["manifest"]["source_manifest_sha256"]:
            raise ScoreInputError("score-capture-source-mismatch")
        manifest = json.loads(payload)
    except (OSError, ValueError) as error:
        raise ScoreInputError("score-capture-source-unavailable") from error
    if (
        record["id"] != identifier
        or record["manifest"] != saved["manifest"]
        or manifest.get("provider_id") != provider_id
    ):
        raise ScoreInputError("score-capture-source-mismatch")
    return manifest.get("is_synthetic") is True


def _capture_frames(fetch: _ScoreCapture, row: dict, sessions: pd.DatetimeIndex):
    start, end = sessions[0].strftime("%Y%m%d"), sessions[-1].strftime("%Y%m%d")
    dates = {"start_date": start, "end_date": end}
    calendar = fetch.capture("trade_cal", exchange="SSE", **dates).sort_values("date").reset_index(drop=True)
    if not pd.DatetimeIndex(calendar.date).equals(pd.date_range(start, end)) or not pd.DatetimeIndex(
        calendar.loc[calendar.is_open, "date"]
    ).equals(sessions):
        raise ScoreInputError("score-capture-calendar-mismatch")
    benchmark = fetch.capture("index_daily", ts_code=_BENCHMARK, **dates)
    parts = []
    for code in row["pool_codes"]:
        identity = fetch.capture("stock_basic", ts_code=code).iloc[0]
        instrument = to_qlib(code)
        quotes = [fetch.capture(api, ts_code=code, **dates) for api in _MARKET_APIS]
        if any(
            not quote.instrument.eq(instrument).all() or not pd.DatetimeIndex(quote.date.sort_values()).equals(sessions)
            for quote in quotes
        ):
            raise ScoreInputError("score-capture-incomplete-quote")
        task = {"code": code, "name": str(identity["name"]), "industry": str(identity.get("industry", ""))}
        frame = _assemble_primary_frame(task, sessions, quotes)
        # An active row has a complete primary quote and positive volume/amount. A quote
        # gap or an unproven halt cannot be interpreted as active trading.
        if (
            not frame.volume.gt(0).all()
            or not frame.amount.gt(0).all()
            or not primary_valid(frame, sessions, np.ones(len(sessions), dtype=bool), [])
        ):
            raise ScoreInputError("score-capture-quote-quality")
        parts.append(frame)
    market = pd.concat(parts, ignore_index=True)
    coverage = coverage_report(market, benchmark, sessions, tuple(row["pool_codes"]), _BENCHMARK)
    if coverage["status"] != "complete":
        raise ScoreInputError("score-capture-incomplete-coverage")
    return market, benchmark, calendar, coverage


def _published(target: Path, row: dict, provider_id: str, deadline: datetime, now: datetime) -> dict:
    if target.resolve(strict=True) != target:
        raise ScoreInputError("score-publication-path-mismatch")
    for name in ("manifest.json", "market.parquet", "benchmark.parquet", "calendar.parquet"):
        path = target / name
        if path.resolve(strict=True) != path or not S_ISREG(path.lstat().st_mode):
            raise ScoreInputError("score-publication-path-mismatch")
    with (target / "manifest.json").open("rb") as stream:
        payload = stream.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise ScoreInputError("score-publication-manifest-too-large")
    manifest = json.loads(payload)
    if (
        manifest["id"] != managed_score_snapshot_id(manifest)
        or manifest.get("provider_id") != provider_id
        or "snapshot_path" in manifest
        or manifest.get("score_capture", {}).get("registration_sha256") != row["confirmed_evidence_sha256"]
        or manifest.get("score_capture", {}).get("publication_id") != target.name
    ):
        raise ScoreInputError("score-publication-conflict")
    captured = _capture_time(row, manifest, deadline, now)
    market, _, _, _ = _load_leaf(target, manifest)
    return {
        "id": manifest["id"],
        "captured_at": captured.isoformat(),
        "manifest_sha256": sha256_bytes(payload),
        "files": manifest["files"],
        "rows": len(market),
    }


def publish_forward_score_snapshot(
    engine: Engine,
    data_root: Path,
    registration_id: str,
    *,
    storage_root: Path,
    provider: Any,
    key: str,
) -> dict:
    """Acquire the frozen pool from the injected trusted provider and atomically publish its score input."""
    operation = submission_fingerprint("forward-score-capture", {"registration_id": registration_id}, key)
    # The operation includes the key separately since the fingerprint checks its format
    # and covers the request document only.
    publication_id = "fsc-" + canonical_sha256({"operation": operation, "key": key})[:24]
    with engine.connect() as connection:
        saved = (
            connection.execute(select(forward_registration).where(forward_registration.c.id == registration_id))
            .mappings()
            .one_or_none()
        )
    if saved is None or not registration_row_confirmed(saved):
        raise ScoreInputError("registration-unsealed")
    row = dict(saved)
    frozen, deadline = _frozen_schedule(row)
    signal = row["signal_date"]
    close = datetime.combine(signal, time(15), tzinfo=SHANGHAI)
    sessions = pd.DatetimeIndex([day for day in frozen if day <= signal][-(row["factor_window"] + 1) :])
    if len(sessions) != row["factor_window"] + 1:
        raise ScoreInputError("score-capture-missing-warmup")

    provider_id = provider_identifier(provider)
    root = data_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name in ("research", "subsets"):
        root = root / name
        root.mkdir(exist_ok=True)
        if root.resolve(strict=True) != root:
            raise ScoreInputError("score-publication-path-mismatch")
    target = root / publication_id
    with FileLock(root / f".{publication_id}.lock"):
        if target.exists() or target.is_symlink():
            return _published(target, row, provider_id, deadline, _database_now(engine))
        _check_window(_database_now(engine), close, deadline)
        synthetic = _registered_source(engine, storage_root.resolve(), row, provider_id)
        with TemporaryDirectory(prefix=".score-", dir=root) as temporary:
            stage = Path(temporary)
            raw = stage / "capture"
            catalog = Catalog(raw)
            try:
                catalog.begin_job(publication_id, {"registration_id": registration_id}, 2 + 5 * len(row["pool_codes"]))
                fetch = _ScoreCapture(provider, raw, catalog, publication_id, engine, close, deadline)
                market, benchmark, calendar, coverage = _capture_frames(fetch, row, sessions)
                captured = max(datetime.fromisoformat(item["response_received_at"]) for item in fetch.receipts)
                catalog.finish(publication_id, "complete", {"captured_at": captured.isoformat()})
            finally:
                catalog.close()

            leaf = stage / publication_id
            leaf.mkdir()
            files = write_market_snapshot_files(leaf, market, benchmark, calendar)
            manifest = {
                "schema_version": "market-v2",
                "provider_id": provider_id,
                "codes": row["pool_codes"],
                "start": sessions[0].strftime("%Y%m%d"),
                "end": signal.strftime("%Y%m%d"),
                "benchmark": _BENCHMARK,
                "trading_days": len(sessions),
                "rows": len(market),
                "is_synthetic": synthetic or getattr(provider, "settings", None) is None,
                "coverage": coverage,
                "files": files,
                "score_capture": {
                    "captured_at": captured.isoformat(),
                    "data_end": signal.isoformat(),
                    "pool_codes": row["pool_codes"],
                    "source_dataset_id": row["dataset_id"],
                    "registration_id": registration_id,
                    "registration_sha256": row["confirmed_evidence_sha256"],
                    "publication_id": publication_id,
                    "requests": fetch.receipts,
                },
            }
            manifest["id"] = managed_score_snapshot_id(manifest)
            manifest_path = leaf / "manifest.json"
            atomic_json(manifest_path, manifest)
            proof = _published(leaf, row, provider_id, deadline, _database_now(engine))
            leaf.rename(target)
    return proof
