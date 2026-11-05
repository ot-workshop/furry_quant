import hashlib
import json

import pytest

from finance_quant.persistence.market import snapshot_dataset_record


def write_snapshot(root):
    root.mkdir(parents=True)
    files = {}
    for name, content in (
        ("market.parquet", b"market"),
        ("benchmark.parquet", b"benchmark"),
        ("calendar.parquet", b"calendar"),
    ):
        (root / name).write_bytes(content)
        files[name] = hashlib.sha256(content).hexdigest()
    manifest = {"id": "input-v1", "schema_version": "market-v3", "files": files}
    (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest


def test_snapshot_dataset_record_is_content_addressed_and_relative(tmp_path):
    snapshot = tmp_path / "data" / "snapshot"
    write_snapshot(snapshot)

    first = snapshot_dataset_record(snapshot, tmp_path, request={"codes": ["000001.SZ"]})
    second = snapshot_dataset_record(snapshot, tmp_path, request={"codes": ["000001.SZ"]})

    assert first["id"] == second["id"]
    assert first["id"].startswith("snapshot-")
    assert first["relative_path"] == "data/snapshot"
    assert first["manifest"]["source_id"] == "input-v1"
    assert first["status"] == "ready"


def test_snapshot_dataset_record_rejects_changed_content(tmp_path):
    snapshot = tmp_path / "snapshot"
    write_snapshot(snapshot)
    (snapshot / "market.parquet").write_bytes(b"changed")

    with pytest.raises(ValueError, match="checksum mismatch"):
        snapshot_dataset_record(snapshot, tmp_path, request={})
