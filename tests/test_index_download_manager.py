from fastapi.testclient import TestClient
from filelock import FileLock

from finance_quant.application import index_download_manager as manager
from support.fakes import make_app


def test_status_reflects_worker_lock_and_pause_keeps_data(tmp_path):
    root = tmp_path / "warehouse"
    root.mkdir()
    assert not manager.status(root)["worker_running"]
    with FileLock(root / "warehouse.lock", timeout=0):
        assert manager.status(root)["worker_running"]
    (root / "cached.parquet").write_bytes(b"capture")
    paused = manager.pause(root)
    assert paused["pause_requested"] and (root / "cached.parquet").read_bytes() == b"capture"


def test_controls_require_local_origin_and_queue_worker_task(tmp_path, monkeypatch):
    root = tmp_path / "warehouse"
    monkeypatch.setattr(manager, "warehouse_root", lambda: root)
    paused = []
    monkeypatch.setattr(manager, "pause", lambda: paused.append(True) or {"status": "paused"})
    app = make_app(tmp_path / "runs", tmp_path / "data")
    store = app.state.research_tasks.store
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.get("/api/v1/research/index-download").json()["worker_running"] is False
    assert (
        client.post(
            "/api/v1/research/index-download/start", json={}, headers={"Origin": "https://outside.test"}
        ).status_code
        == 403
    )
    assert client.post("/api/v1/research/index-download/start", json={"token": "never-accept-this"}).status_code == 422
    assert store.list()["total"] == 0
    response = client.post("/api/v1/research/index-download/start", json={})
    assert response.status_code == 200 and response.json()["status"] == "queued"
    queued = store.list()["items"]
    assert [task["kind"] for task in queued] == ["index_download"]
    assert store.get(queued[0]["id"], private=True)["execution"] == {"root": str(root.resolve())}
    assert client.get("/api/v1/research/tasks").json()["total"] == 0, "download tasks are not research tasks"
    assert client.post("/api/v1/research/index-download/pause", json={}).status_code == 200
    assert paused == [True]
