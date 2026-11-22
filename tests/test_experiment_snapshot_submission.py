"""Test the experiment POST boundary without loading market data or running a worker."""

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_experiments import experiment_request
from test_managed_data import Provider

from finance_quant.application import research_requests
from support.fakes import make_app, make_service
from support.fixed_pool import publish_fixture_snapshot

SNAPSHOT_ID = "market-version-a"
FILES = {f"{name}.parquet": digit * 64 for name, digit in zip(("market", "benchmark", "calendar"), "abc", strict=True)}


def publish_leaf(root, relative, *, snapshot_id=SNAPSHOT_ID, files=FILES):
    directory = root / relative
    directory.mkdir(parents=True)
    payload = json.dumps({"id": snapshot_id, "files": files}, indent=3).encode() + b"\n"
    (directory / "manifest.json").write_bytes(payload)
    return directory, payload


@pytest.fixture
def submit(tmp_path):
    data, runs = tmp_path / "data", tmp_path / "runs"
    service = make_service(runs, data)
    client = TestClient(make_app(runs, data, service=service), base_url="http://127.0.0.1")

    def post(snapshot_id=SNAPSHOT_ID):
        return client.post(
            "/api/v1/research/experiments",
            json=experiment_request(snapshot_id),
            headers={"Idempotency-Key": "experiment-pinned-test"},
        )

    return data, service.store, post


@pytest.mark.parametrize("relative", ["research/downloads/batch/snapshots/leaf", "research/subsets/leaf"])
def test_submit_pins_relative_leaf_and_hashes_exactly_the_parsed_bytes(submit, monkeypatch, relative):
    data, store, post = submit
    directory, payload = publish_leaf(data, relative)
    manifest_path = directory / "manifest.json"
    original_open = Path.open
    reads = []

    def track_open(path, *args, **kwargs):
        if path.parent == directory:
            assert path == manifest_path, "POST must not open market or reference data"
            reads.append(path)
        return original_open(path, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("POST must not load snapshots or select a source")

    monkeypatch.setattr(Path, "open", track_open)
    monkeypatch.setattr(research_requests, "load_market", forbidden)
    monkeypatch.setattr(research_requests, "snapshot_registry", forbidden)
    response = post()
    assert response.status_code == 202, response.text
    execution = store.get(response.json()["id"], private=True)["execution"]
    assert execution == {
        "prepare_state": "pending",
        "candidates": ["momentum", "value"],
        "snapshot_dir": relative,
        "data_version": SNAPSHOT_ID,
        "files": FILES,
        "manifest_sha256": hashlib.sha256(payload).hexdigest(),
    }
    assert reads == [manifest_path]
    assert store.list()["total"] == 1


def test_submit_accepts_published_version_next_to_incomplete_leaf(submit):
    data, store, post = submit
    manifest = publish_fixture_snapshot(
        data, ("600000.SH", "000001.SZ", "600519.SH"), "20241202", "20250228", provider=Provider()
    )
    (data / "research/subsets/without-published-manifest").mkdir(parents=True)
    response = post(manifest["id"])
    assert response.status_code == 202, response.text
    execution = store.get(response.json()["id"], private=True)["execution"]
    leaf = data / execution["snapshot_dir"]
    assert leaf.name == Path(manifest["snapshot_path"]).name
    assert execution["manifest_sha256"] == hashlib.sha256((leaf / "manifest.json").read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("scenario", "detail"),
    [
        ("missing", "未找到"),
        ("duplicate", "标识重复"),
        ("quarantine", "已被隔离"),
        ("unreadable", "无法读取"),
        ("corrupt", "无法读取"),
        ("ambiguous_json", "无法读取"),
        ("oversized", "大小上限"),
        ("invalid_files", "文件摘要"),
        ("redirect", "指向其他路径"),
        ("staging", "未找到"),
    ],
)
def test_submit_rejects_without_enqueuing(submit, monkeypatch, scenario, detail):
    data, store, post = submit
    if scenario == "staging":
        publish_leaf(data, "research/subsets/.staging-projection")
        publish_leaf(data, "research/downloads/batch/snapshots/.pending-fixture")
    elif scenario != "missing":
        directory, _ = publish_leaf(
            data, "research/subsets/leaf", files={"market.parquet": "invalid"} if scenario == "invalid_files" else FILES
        )
        if scenario == "duplicate":
            publish_leaf(data, "research/downloads/batch/snapshots/another-leaf")
        elif scenario == "quarantine":
            (directory / "rejected.json").write_text("{}", encoding="utf-8")
        elif scenario == "redirect":
            manifest = directory / "manifest.json"
            fields = json.loads(manifest.read_bytes())
            manifest.write_text(json.dumps({**fields, "snapshot_path": "other/leaf"}), encoding="utf-8")
        elif scenario in {"unreadable", "corrupt", "ambiguous_json", "oversized"}:
            other, _ = publish_leaf(data, "research/subsets/another-leaf")
            manifest = other / "manifest.json"
            if scenario == "corrupt":
                manifest.write_bytes(b"[")
            elif scenario == "ambiguous_json":
                manifest.write_bytes(b'{"id":"market-version-a","id":"market-version-b"}')
            elif scenario == "oversized":
                manifest.write_bytes(manifest.read_bytes() + b" " * (1024 * 1024))
            else:
                original_open = Path.open

                def fail_open(path, *args, **kwargs):
                    if path == manifest:
                        raise PermissionError("injected unreadable candidate")
                    return original_open(path, *args, **kwargs)

                monkeypatch.setattr(Path, "open", fail_open)
    response = post()
    assert response.status_code == 422, response.text
    assert detail in response.json()["detail"]
    assert store.list()["total"] == 0


def test_submit_does_not_claim_unique_after_inventory_budget_is_exhausted(submit):
    data, store, post = submit
    directory, _ = publish_leaf(data, "research/subsets/leaf")
    for n in range(4096):
        (directory.parent / f"entry-{n}").touch()
    response = post()
    assert response.status_code == 422, response.text
    assert "扫描上限" in response.json()["detail"]
    assert store.list()["total"] == 0


@pytest.mark.parametrize("entry", ["ancestor", "leaf", "manifest"])
def test_submit_rejects_aliases_outside_managed_leaf(submit, tmp_path, entry):
    data, store, post = submit
    elsewhere, _ = publish_leaf(tmp_path, "elsewhere/leaf")
    alias = (
        data
        / {
            "ancestor": "research/subsets",
            "leaf": "research/subsets/a",
            "manifest": "research/subsets/a/manifest.json",
        }[entry]
    )
    alias.parent.mkdir(parents=True)
    target = {"ancestor": elsewhere.parent, "leaf": elsewhere, "manifest": elsewhere / "manifest.json"}[entry]
    try:
        alias.symlink_to(target, target_is_directory=entry != "manifest")
    except (NotImplementedError, OSError) as error:
        if os.name != "nt" or entry == "manifest":
            pytest.skip(f"symbolic links unavailable: {error}")
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(target)], capture_output=True)
        if result.returncode:
            pytest.skip("directory junction unavailable")
    response = post()
    assert response.status_code == 422, response.text
    assert "路径" in response.json()["detail"]
    assert store.list()["total"] == 0
