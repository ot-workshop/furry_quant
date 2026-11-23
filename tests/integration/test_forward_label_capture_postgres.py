"""Publish fresh synthetic labels through the managed ledger and verify them in the real queue."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import event
from test_forward_score_capture_postgres import OfflineQuotes
from test_forward_score_postgres import CODES, _execute
from test_forward_verification_postgres import _registered, _stored_registration
from test_postgres_runtime import engine as engine

from finance_quant.application import forward_label_capture, forward_score_capture
from finance_quant.application.forward_verification import enqueue_forward_verification
from finance_quant.application.research_requests import (
    lookup_label_snapshot_metas,
    lookup_snapshot_meta,
    snapshot_registry,
)
from finance_quant.checksums import file_sha256
from finance_quant.data.managed import ManagedFetcher, provider_identifier
from finance_quant.data.market import (
    LABEL_SNAPSHOT_LOCATION,
    label_snapshot_leaf_name,
    managed_label_snapshot_id,
    write_market_snapshot_files,
)
from finance_quant.persistence.market import MarketRepository

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL"
)
KEY = "label-publish-fixture"


class OfflineLabels(OfflineQuotes):
    missing: tuple[str, ...] = ()
    zero = False

    def query(self, api, **params):
        result = super().query(api, **params)
        if api not in {"daily", "adj_factor"}:
            return result
        code = params["ts_code"]
        if code in self.missing:
            return result.iloc[:0]
        if api == "adj_factor":
            result.loc[1, "adj_factor"] = 2.0 if code == CODES[0] else 1.0
            return result
        target = dict(zip(CODES, (12.0, 11.0, 9.0), strict=True))[code]
        opening = (10.0 if self.zero else target) / (2.0 if code == CODES[0] else 1.0)
        result.loc[:, ["open", "close"]] = [[10.0, 10.0], [opening, opening]]
        return result


@pytest.fixture
def label_setup(engine, tmp_path):
    provider = OfflineLabels([])
    source_root = tmp_path / "source"
    source_root.mkdir()
    reference = pd.DataFrame({"fixture": [1]})
    files = write_market_snapshot_files(source_root, reference, reference, reference)
    (source_root / "manifest.json").write_text(
        json.dumps(
            {
                "id": "label-source-fixture",
                "schema_version": "market-v2",
                "provider_id": provider_identifier(provider),
                "is_synthetic": True,
                "files": files,
            }
        ),
        encoding="utf-8",
    )
    source = MarketRepository(engine=engine).register_snapshot(source_root, tmp_path, request={})
    store, row, _ = _registered(engine, tmp_path, source=source["id"])
    provider.sessions = pd.DatetimeIndex([row[f"{stage}_date"] for stage in ("signal", "entry", "exit")])

    def publish(*, supplier=provider, key=KEY):
        return forward_label_capture.publish_forward_label_snapshot(
            engine, tmp_path, row["id"], storage_root=tmp_path, provider=supplier, key=key
        )

    return tmp_path, store, row, provider, publish


@pytest.mark.parametrize("case", ["complete", "zero", "missing", "all-missing"])
def test_capture_seals_through_the_real_verification_worker(engine, label_setup, case):
    root, store, row, provider, publish = label_setup
    if case == "missing":
        provider.missing = (CODES[-1],)
    elif case == "all-missing":
        provider.missing = tuple(CODES)
    provider.zero = case == "zero"
    assert enqueue_forward_verification(store, root, row["id"], key="verify-published-labels") is None
    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = workers.submit(publish), workers.submit(publish)
        receipt = first.result(timeout=30)
        assert receipt == second.result(timeout=30)
    assert len(provider.calls) == 8
    ((path, manifest, digest),) = lookup_label_snapshot_metas(root)
    assert path.parts[:2] == LABEL_SNAPSHOT_LOCATION
    assert (receipt["id"], receipt["manifest_sha256"]) == (manifest["id"], digest)
    capture = manifest["label_capture"]
    assert capture["source_dataset_id"] == row["dataset_id"]
    assert capture["calendar_sha256"] == row["calendar_fingerprint"]
    assert capture["data_end"] == row["exit_date"].isoformat()
    responses = [datetime.fromisoformat(item["response_received_at"]) for item in capture["requests"]]
    mature = datetime.fromisoformat(row["data_rules"]["label_capture_after"])
    assert all(
        mature <= datetime.fromisoformat(request["request_started_at"]) <= received
        for request, received in zip(capture["requests"], responses, strict=True)
    )
    assert datetime.fromisoformat(capture["captured_at"]) == max(responses)
    assert all(file_sha256(root / path / name) == expected for name, expected in receipt["files"].items())
    assert publish() == receipt
    assert len(provider.calls) == 8
    with pytest.raises(ValueError):
        lookup_snapshot_meta(root, receipt["id"], allow_score=True)
    assert receipt["id"] not in snapshot_registry(root)
    later = publish(key="independent-second-label-version")
    assert later["id"] != receipt["id"]
    queued = enqueue_forward_verification(store, root, row["id"], key="verify-published-labels")
    assert _stored_registration(engine)["label_snapshot"]["data_version"] == receipt["id"]
    if case == "complete":
        assert publish(key="third-version-after-queueing")["id"] != receipt["id"]
    assert enqueue_forward_verification(store, root, row["id"], key="verify-published-labels") == queued
    attempt, task = _execute(store, root, queued)
    assert attempt["status"] == task["status"] == "succeeded"
    result = _stored_registration(engine)["result"]
    assert (result["pool_size"], result["top_k"]) == (3, 1)
    if case == "missing":
        assert receipt["rows"] == 4
        assert result["pool_valid"] == 2 and result["excess_return_pp"] is None
        assert result["missing_codes"] == [CODES[-1]]
        assert result["labels"][-1]["reasons"] == ["entry-missing-quote", "exit-missing-quote"]
    elif case == "all-missing":
        assert receipt["rows"] == 0
        assert result["pool_valid"] == 0 and result["excess_return_pp"] is None
        assert result["missing_codes"] == CODES
    else:
        assert receipt["rows"] == 6
        assert result["excess_return_pp"] == pytest.approx(0.0 if case == "zero" else 13.333333333)
    print(f"capture={capture['captured_at']}, rows={receipt['rows']}, excess_pp={result['excess_return_pp']}")


@pytest.mark.parametrize("mode", ["early", "exact", "backward-response"])
def test_maturity_uses_database_clock_for_each_request_and_response(engine, label_setup, mode):
    root, _, row, provider, publish = label_setup
    mature = datetime.fromisoformat(row["data_rules"]["label_capture_after"])
    sample = mature - timedelta(microseconds=1) if mode == "early" else mature

    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement == "select clock_timestamp()":
            return "select cast(%s as timestamptz)", (sample.isoformat(),)
        return statement, parameters

    def move_clock(api):
        nonlocal sample
        sample = mature - timedelta(microseconds=1)

    if mode == "backward-response":
        provider.on_query = move_clock
    event.listen(engine, "before_cursor_execute", observe, retval=True)
    try:
        if mode == "exact":
            assert datetime.fromisoformat(publish()["captured_at"]) == mature
        else:
            with pytest.raises(ValueError, match="before-label-capture-maturity"):
                publish()
            assert not lookup_label_snapshot_metas(root)
            assert len(provider.calls) == int(mode == "backward-response")
    finally:
        event.remove(engine, "before_cursor_execute", observe)


def test_source_and_calendar_mismatch_leave_nothing_visible(label_setup):
    root, _, _, provider, publish = label_setup
    with pytest.raises(ValueError, match="label-capture-source-mismatch"):
        publish(supplier=OfflineQuotes(provider.sessions))
    assert not lookup_label_snapshot_metas(root)
    provider.closed_signal = True
    with pytest.raises(ValueError, match="label-capture-calendar-mismatch"):
        publish()
    assert not lookup_label_snapshot_metas(root)
    provider.closed_signal = False
    provider.wrong_date = True
    with pytest.raises(ValueError, match="label-capture-request-failed"):
        publish()
    assert not lookup_label_snapshot_metas(root)


def test_old_cache_cannot_satisfy_new_capture(label_setup, monkeypatch):
    root, _, row, provider, publish = label_setup
    original = forward_score_capture._ForwardCapture.__init__

    def seed_cache(self, source, raw, catalog, job_id, *args):
        cached = ManagedFetcher(source, raw, catalog, job_id, refresh=False, scope={})
        api, params = forward_label_capture._label_requests(row)[0]
        assert cached.get(api, **params) is not None
        provider.calls.clear()
        original(self, source, raw, catalog, job_id, *args)

    monkeypatch.setattr(forward_score_capture._ForwardCapture, "__init__", seed_cache)
    with pytest.raises(ValueError, match="label-capture-reused-cache"):
        publish()
    assert not lookup_label_snapshot_metas(root)
    assert provider.calls == []


def test_interrupted_or_damaged_files_never_bind_and_same_key_can_retry(label_setup, monkeypatch):
    root, store, row, _, publish = label_setup
    original = forward_label_capture.atomic_json

    def interrupt(path, manifest):
        original(path, manifest)
        assert not lookup_label_snapshot_metas(root)
        assert enqueue_forward_verification(store, root, row["id"], key="verify-with-interrupted-labels") is None
        raise OSError("simulated interruption before rename")

    with monkeypatch.context() as patch:
        patch.setattr(forward_label_capture, "atomic_json", interrupt)
        with pytest.raises(OSError, match="simulated interruption"):
            publish()
    assert not lookup_label_snapshot_metas(root)
    receipt = publish()
    ((path, manifest, _),) = lookup_label_snapshot_metas(root)
    bad_time = datetime.fromisoformat(receipt["captured_at"]) - timedelta(seconds=1)
    manifest["label_capture"]["captured_at"] = bad_time.isoformat()
    manifest["id"] = managed_label_snapshot_id(manifest)
    manifest_path = root / path / "manifest.json"
    original_manifest = manifest_path.read_bytes()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert not lookup_label_snapshot_metas(root)
    assert enqueue_forward_verification(store, root, row["id"], key="verify-with-interrupted-labels") is None

    # Renaming to the altered content ID cannot make the timestamp agree with the receipts.
    renamed = manifest_path.parent.rename(manifest_path.parent.with_name(label_snapshot_leaf_name(manifest)))
    assert lookup_label_snapshot_metas(root)
    assert enqueue_forward_verification(store, root, row["id"], key="verify-with-interrupted-labels") is None
    renamed.rename(manifest_path.parent)

    manifest_path.write_bytes(original_manifest)
    with (root / path / "market.parquet").open("ab") as stream:
        stream.write(b"corrupt")
    assert enqueue_forward_verification(store, root, row["id"], key="verify-with-interrupted-labels") is None
    with pytest.raises(ValueError, match="checksum mismatch"):
        publish()


def test_raw_capture_checksum_mismatch_cannot_publish(label_setup, monkeypatch):
    root, _, _, _, publish = label_setup
    original = ManagedFetcher.get

    def corrupt(self, api, **params):
        frame = original(self, api, **params)
        with (self.root / self.requests[-1]["raw_path"]).open("ab") as stream:
            stream.write(b"corrupt")
        return frame

    monkeypatch.setattr(ManagedFetcher, "get", corrupt)
    with pytest.raises(ValueError, match="label-capture-receipt-mismatch"):
        publish()
    assert not lookup_label_snapshot_metas(root)
