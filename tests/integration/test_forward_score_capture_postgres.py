"""Use the real managed publisher and scoring queue with an offline exchange fixture."""

from __future__ import annotations

import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta

import pandas as pd
import pytest
from sqlalchemy import event, select, text
from test_forward_score_postgres import CODES, _execute
from test_postgres_runtime import engine as engine

from finance_quant.application import forward_score_capture
from finance_quant.application.forward_registration import ForwardCalendar, _freeze_registration
from finance_quant.application.forward_score import ScoreInputError, enqueue_forward_score
from finance_quant.application.research_requests import lookup_snapshot_meta, snapshot_registry
from finance_quant.checksums import file_sha256
from finance_quant.data import managed
from finance_quant.data.managed import ManagedFetcher, provider_identifier
from finance_quant.data.market import (
    SCORE_SNAPSHOT_LOCATION,
    SCORE_SNAPSHOT_TYPE,
    managed_score_snapshot_id,
    write_market_snapshot_files,
)
from finance_quant.persistence.forward import SHANGHAI, canonical_sha256, registration_evidence_digest
from finance_quant.persistence.market import MarketRepository
from finance_quant.persistence.schema import forward_registration, research_task
from finance_quant.persistence.tasks import PgTaskStore
from finance_quant.task_contracts import submission_fingerprint

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL"
)
KEY = "score-publication-fixture"


class OfflineQuotes:
    def __init__(self, sessions):
        self.sessions = pd.DatetimeIndex(sessions)
        self.calls = []
        self.on_query = None
        self.wrong_date = False
        self.closed_signal = False

    def query(self, api, **params):
        self.calls.append(api)
        if self.on_query is not None:
            self.on_query(api)
        code = params.get("ts_code")
        if api == "stock_basic":
            return pd.DataFrame({"ts_code": [code], "name": [code], "industry": ["offline"]})
        dates = pd.date_range(params["start_date"], params["end_date"])
        if api == "trade_cal":
            opened = dates.isin(self.sessions)
            if self.closed_signal:
                opened[-1] = False
            return pd.DataFrame({"cal_date": dates.strftime("%Y%m%d"), "is_open": opened.astype(int)})
        dates = dates[dates.isin(self.sessions)]
        frame = pd.DataFrame({"ts_code": code, "trade_date": dates.strftime("%Y%m%d")})
        if api in {"daily", "index_daily"}:
            increment = {CODES[0]: 0.5, CODES[1]: 0.4, CODES[2]: -0.1}.get(code, 0.0)
            closes = 10.0 + pd.Series(range(len(dates)), dtype=float) * increment
            previous = closes.shift(fill_value=10.0)
            frame["open"], frame["close"], frame["pre_close"] = closes, closes, previous
            frame["high"], frame["low"] = closes + 0.15, closes - 0.15
            frame["vol"], frame["amount"] = 1000, 100
            frame["pct_chg"] = (closes / previous - 1) * 100
            if api == "daily" and self.wrong_date:
                frame.loc[frame.index[-1], "trade_date"] = (dates[-1] + timedelta(days=1)).strftime("%Y%m%d")
        elif api == "adj_factor":
            frame["adj_factor"] = 1.0
        elif api == "daily_basic":
            frame["pe_ttm"], frame["pb"], frame["total_mv"] = 10.0, 1.0, 1000
        elif api == "stk_limit":
            frame["up_limit"], frame["down_limit"] = 12.0, 8.0
        else:
            raise AssertionError(api)
        return frame


@pytest.fixture
def setup_capture(engine, tmp_path):
    with engine.connect() as connection:
        today = connection.scalar(text("select (clock_timestamp() at time zone 'Asia/Shanghai')::date"))
    signal = today - timedelta(days=1)
    history = (signal - timedelta(days=4), signal - timedelta(days=1), signal)
    entry, exit_ = today + timedelta(days=1), today + timedelta(days=2)
    provider = OfflineQuotes(history)
    data_root = tmp_path / "data"
    source_root = data_root / "research" / "subsets" / "source-fixture"
    source_root.mkdir(parents=True)
    reference = pd.DataFrame({"fixture": [1]})
    old_files = write_market_snapshot_files(source_root, reference, reference, reference)
    (source_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "source-fixture",
                "schema_version": "market-v2",
                "provider_id": provider_identifier(provider),
                "files": old_files,
                "is_synthetic": True,
            }
        ),
        encoding="utf-8",
    )
    source = MarketRepository(engine=engine).register_snapshot(source_root, tmp_path, request={})
    document = {
        "name": "Offline score capture",
        "signal_date": signal.isoformat(),
        "pool_codes": CODES,
        "top_k": 1,
        "factor": "momentum",
        "factor_window": 2,
        "factor_direction": "high",
        "dataset_id": source["id"],
    }
    key = "score-registration-fixture"
    proof = datetime.combine(signal - timedelta(days=1), time(18), tzinfo=SHANGHAI)
    row = {
        **_freeze_registration(document, signal, ForwardCalendar("score-fixture", (*history, entry, exit_))),
        "id": "fwd-score-capture",
        "idempotency_key": key,
        "fingerprint": submission_fingerprint("forward-registration", document, key),
        "request": document,
        "status": "registered",
        "written_at": proof,
        "staging_at": proof,
        "confirmed_at": proof,
    }
    row["confirmed_evidence_sha256"] = registration_evidence_digest(row)
    with engine.begin() as connection:
        connection.execute(forward_registration.insert().values(**row))

    def publish(supplier=None):
        return forward_score_capture.publish_forward_score_snapshot(
            engine, data_root, row["id"], storage_root=tmp_path, provider=supplier or provider, key=KEY
        )

    return data_root, row, provider, publish


def _unpublished(root):
    assert not list(root.joinpath(*SCORE_SNAPSHOT_LOCATION).glob("fsc-*"))


def test_publication_uses_live_receipts_and_is_readable_by_the_real_scoring_worker(engine, setup_capture, monkeypatch):
    root, row, provider, publish = setup_capture
    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = workers.submit(publish), workers.submit(publish)
        receipt = first.result(timeout=15)
        assert second.result(timeout=15) == receipt
    path, manifest, digest = lookup_snapshot_meta(root, receipt["id"], allow_score=True)
    assert path.parts[:2] == SCORE_SNAPSHOT_LOCATION
    assert manifest["publication_type"] == SCORE_SNAPSHOT_TYPE
    with pytest.raises(ValueError):
        lookup_snapshot_meta(root, receipt["id"])
    assert receipt["id"] not in snapshot_registry(root)
    assert digest == receipt["manifest_sha256"]
    capture = manifest["score_capture"]
    assert capture["source_dataset_id"] == row["dataset_id"]
    assert capture["registration_sha256"] == row["confirmed_evidence_sha256"]
    assert capture["data_end"] == row["signal_date"].isoformat()
    assert capture["pool_codes"] == CODES
    assert manifest["is_synthetic"] is True
    received = [datetime.fromisoformat(item["response_received_at"]) for item in capture["requests"]]
    started = [datetime.fromisoformat(item["request_started_at"]) for item in capture["requests"]]
    close = datetime.combine(row["signal_date"], time(15), tzinfo=SHANGHAI)
    deadline = datetime.combine(row["entry_date"], time(9, 15), tzinfo=SHANGHAI)
    assert len(started) == len(provider.calls) == 17
    assert all(close < a <= b < deadline for a, b in zip(started, received, strict=True))
    assert datetime.fromisoformat(capture["captured_at"]) == max(received)
    assert all(file_sha256(root / path / name) == expected for name, expected in receipt["files"].items())
    assert publish() == receipt
    assert len(provider.calls) == 17

    store = PgTaskStore(engine=engine)
    binding = enqueue_forward_score(store, root, row["id"], receipt["id"], key="score-after-publication")
    attempt, task = _execute(store, root, binding)
    assert task["status"] == attempt["status"] == "succeeded"
    assert attempt["evidence"]["candidate_codes"] == [CODES[0]]
    assert [item["momentum"] for item in attempt["evidence"]["scores"]] == pytest.approx([0.1, 0.08, -0.02])
    assert attempt["evidence"]["score_snapshot"]["manifest_sha256"] == digest
    monkeypatch.setattr(forward_score_capture, "_database_now", lambda engine: deadline + timedelta(seconds=1))
    assert publish() == receipt
    assert len(provider.calls) == 17
    print(f"received={capture['captured_at']}, responses={len(received)}, score={attempt['status']}")


@pytest.mark.parametrize("boundary", ["close", "deadline", "response-at-deadline"])
def test_capture_rejects_both_exact_bounds_and_a_call_that_crosses_the_deadline(engine, setup_capture, boundary):
    root, row, provider, publish = setup_capture
    close = datetime.combine(row["signal_date"], time(15), tzinfo=SHANGHAI)
    deadline = datetime.combine(row["entry_date"], time(9, 15), tzinfo=SHANGHAI)
    sample = {
        "close": close,
        "deadline": deadline,
        "response-at-deadline": deadline - timedelta(microseconds=1),
    }[boundary]

    def observed(connection, cursor, statement, parameters, context, executemany):
        if statement == "select clock_timestamp()":
            return f"select cast('{sample.isoformat()}' as timestamptz)", ()
        return statement, parameters

    def cross_deadline(api):
        nonlocal sample
        sample = deadline

    if boundary == "response-at-deadline":
        provider.on_query = cross_deadline
    event.listen(engine, "before_cursor_execute", observed, retval=True)
    try:
        with pytest.raises(ScoreInputError, match="outside-score-capture-window"):
            publish()
    finally:
        event.remove(engine, "before_cursor_execute", observed)
    assert len(provider.calls) == int(boundary == "response-at-deadline")
    _unpublished(root)


def test_old_managed_cache_cannot_supply_fresh_capture_evidence(setup_capture, monkeypatch):
    root, row, provider, publish = setup_capture
    original = forward_score_capture._ScoreCapture.__init__
    old = datetime.combine(row["signal_date"], time(14), tzinfo=SHANGHAI)

    def seed_cache(self, source, raw, catalog, job_id, *args):
        with monkeypatch.context() as patch:
            patch.setattr(managed, "utc_now", lambda: old.isoformat())
            cached = ManagedFetcher(source, raw, catalog, job_id, refresh=False, scope={})
            assert (
                cached.get(
                    "trade_cal",
                    exchange="SSE",
                    start_date=provider.sessions[0].strftime("%Y%m%d"),
                    end_date=provider.sessions[-1].strftime("%Y%m%d"),
                )
                is not None
            )
        original(self, source, raw, catalog, job_id, *args)

    monkeypatch.setattr(forward_score_capture._ScoreCapture, "__init__", seed_cache)
    with pytest.raises(ScoreInputError, match="score-capture-reused-cache"):
        publish()
    assert provider.calls == ["trade_cal"]
    _unpublished(root)


@pytest.mark.parametrize(
    ("damage", "error"),
    [
        ("wrong-provider", "score-capture-source-mismatch"),
        ("wrong-date", "score-capture-request-failed"),
        ("calendar-drift", "score-capture-calendar-mismatch"),
        ("raw-digest", "score-capture-receipt-mismatch"),
        ("published-digest", "checksum mismatch"),
        ("interrupted-write", None),
    ],
)
def test_damaged_or_interrupted_capture_cannot_publish(setup_capture, monkeypatch, damage, error):
    root, _, provider, publish = setup_capture
    supplier = provider
    if damage == "wrong-provider":

        class OtherQuotes(OfflineQuotes):
            pass

        supplier = OtherQuotes(provider.sessions)
    if damage == "wrong-date":
        provider.wrong_date = True
    if damage == "calendar-drift":
        provider.closed_signal = True
    if damage == "raw-digest":
        get = ManagedFetcher.get

        def corrupt(self, api, **params):
            frame = get(self, api, **params)
            path = self.root / self.requests[-1]["raw_path"]
            with path.open("ab") as stream:
                stream.write(b"invalid")
            return frame

        monkeypatch.setattr(ManagedFetcher, "get", corrupt)
    if damage in {"published-digest", "interrupted-write"}:

        def break_write(leaf, market, benchmark, calendar):
            _unpublished(root)
            files = write_market_snapshot_files(leaf, market, benchmark, calendar)
            if damage == "interrupted-write":
                raise KeyboardInterrupt
            with (leaf / "market.parquet").open("ab") as stream:
                stream.write(b"invalid")
            return files

        monkeypatch.setattr(forward_score_capture, "write_market_snapshot_files", break_write)
    exception = pytest.raises(ValueError, match=error) if error is not None else pytest.raises(KeyboardInterrupt)
    with exception:
        publish(supplier)
    _unpublished(root)
    if damage == "wrong-provider":
        assert supplier.calls == []
    if damage == "interrupted-write":
        monkeypatch.setattr(forward_score_capture, "write_market_snapshot_files", write_market_snapshot_files)
        assert publish()["rows"] == 9


def test_manifest_edit_cannot_rebind_the_published_content_version(engine, setup_capture):
    root, row, _, publish = setup_capture
    receipt = publish()
    path, manifest, _ = lookup_snapshot_meta(root, receipt["id"], allow_score=True)
    manifest["score_capture"]["requests"][0]["rows"] += 1
    changed = root / path / "manifest.json"
    changed.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="score-snapshot-identity-mismatch"):
        enqueue_forward_score(PgTaskStore(engine=engine), root, row["id"], receipt["id"], key="score-after-edit")
    corrupted_digest = file_sha256(changed)
    with pytest.raises(ScoreInputError, match="score-publication-conflict"):
        publish()
    assert file_sha256(changed) == corrupted_digest
    with engine.connect() as connection:
        assert connection.scalar(select(forward_registration.c.score_snapshot)) is None


def _rename_and_retime(root, path, manifest, row, *, rehash=False):
    alias = "ordinary-renamed"
    captured_at = datetime.combine(row["signal_date"], time(15, 1), tzinfo=SHANGHAI).isoformat()
    manifest["score_capture"]["captured_at"] = captured_at
    if rehash:
        alias = managed_score_snapshot_id(manifest)
    manifest["id"] = alias
    target = root / path / "manifest.json"
    target.write_text(json.dumps(manifest), encoding="utf-8")
    return alias, captured_at, file_sha256(target)


@pytest.mark.parametrize("rehash", [False, True], ids=["ordinary-id", "rehashed-id"])
def test_renamed_and_retimed_internal_directory_cannot_be_bound(engine, setup_capture, rehash):
    root, row, _, publish = setup_capture
    path, manifest, _ = lookup_snapshot_meta(root, publish()["id"], allow_score=True)
    alias, _, _ = _rename_and_retime(root, path, manifest, row, rehash=rehash)
    store = PgTaskStore(engine=engine)
    with pytest.raises(ValueError, match="score-snapshot-identity-mismatch"):
        enqueue_forward_score(store, root, row["id"], alias, key="score-renamed-capture")
    assert store.list()["total"] == 0


def test_worker_rejects_a_renamed_internal_directory_even_with_consistent_queued_pins(engine, setup_capture):
    root, row, _, publish = setup_capture
    store = PgTaskStore(engine=engine)
    identifier = publish()["id"]
    path, manifest, _ = lookup_snapshot_meta(root, identifier, allow_score=True)
    pair = enqueue_forward_score(store, root, row["id"], identifier, key="score-renamed-capture")
    alias, captured_at, digest = _rename_and_retime(root, path, manifest, row)
    with engine.begin() as connection:
        pin = connection.scalar(select(forward_registration.c.score_snapshot))
        pin.update(data_version=alias, manifest_sha256=digest, captured_at=captured_at)
        connection.execute(forward_registration.update().values(score_snapshot=pin))
        query = research_task.c.id == pair["task_id"]
        request, execution = connection.execute(
            select(research_task.c.request, research_task.c.execution).where(query)
        ).one()
        request["score_snapshot_id"] = alias
        execution["binding_sha256"] = canonical_sha256(pin)
        connection.execute(research_task.update().where(query).values(request=request, execution=execution))
    attempt, task = _execute(store, root, pair)
    assert task["status"] == attempt["status"] == "failed"
    assert attempt["error"] == "invalid-score-snapshot"
    assert task["result"] is None and attempt["sealed_at"] is None


@pytest.mark.parametrize("rename_internal", [False, True], ids=["original-id", "ordinary-id"])
def test_ordinary_registry_never_reads_published_score_parquet(setup_capture, monkeypatch, rename_internal):
    root, row, _, publish = setup_capture
    identifier = publish()["id"]
    path, manifest, _ = lookup_snapshot_meta(root, identifier, allow_score=True)
    internal = root / path
    ordinary = root / "research" / "subsets" / "ordinary-history"
    ordinary.mkdir()
    for name in manifest["files"]:
        shutil.copyfile(internal / name, ordinary / name)
    (ordinary / "manifest.json").write_text(
        json.dumps(
            {
                "id": "ordinary-history",
                "schema_version": "market-v2",
                "files": manifest["files"],
            }
        ),
        encoding="utf-8",
    )
    if rename_internal:
        identifier, _, _ = _rename_and_retime(root, path, manifest, row)
    moved = root / "research" / "subsets" / "relocated-internal"
    shutil.copytree(internal, moved)

    calls = []
    read = pd.read_parquet

    def track(path, *args, **kwargs):
        calls.append(path)
        return read(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", track)
    assert "ordinary-history" in snapshot_registry(root)
    assert lookup_snapshot_meta(root, "ordinary-history")[0] == ordinary.relative_to(root)
    assert not any(path.parent in (internal, moved) for path in calls)
    with pytest.raises(ValueError):
        lookup_snapshot_meta(root, identifier)
