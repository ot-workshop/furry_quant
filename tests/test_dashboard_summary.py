import datetime as dt
import errno
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from finance_quant.api import create_app
from finance_quant.application import market_download
from finance_quant.checksums import sha256_bytes
from finance_quant.data import market_universe, postgres_market
from finance_quant.storage import ResultStore
from finance_quant.web import system as web_system
from support.fakes import make_app, make_service


def _write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_valid_fixed_snapshot(snap_dir: Path, identifier: str) -> None:
    snap_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in ("market.parquet", "benchmark.parquet", "calendar.parquet"):
        raw = f"{identifier}:{name}".encode("utf-8")
        (snap_dir / name).write_bytes(raw)
        files[name] = sha256_bytes(raw)
    _write_json(
        snap_dir / "manifest.json",
        {
            "id": identifier,
            "schema_version": "market-v4-shared",
            "start": "2024-01-02",
            "end": "2024-06-28",
            "codes": ["000001.SZ"],
            "files": files,
        },
    )


def test_dashboard_summary_counts_headers_without_reading_parquet_or_heavy_tables(tmp_path, monkeypatch):
    runs, data = tmp_path / "runs", tmp_path / "data"

    # Fixed pool snapshot (counted) and subset projection (excluded from fixed/invalid count).
    _write_valid_fixed_snapshot(data / "research/downloads/b1/snapshots/s1", "snap-1")
    _write_json(
        data / "research/downloads/b1/snapshots/sub1/manifest.json",
        {"id": "sub-1", "projection": {"parent_data_version": "snap-1"}},
    )

    # Historical index warehouse and market warehouse (dict assets + verified calendar checksum).
    wh_root = data / "historical/wh1"
    (wh_root / "reference").mkdir(parents=True, exist_ok=True)
    cal_bytes = b"calendar-bytes"
    (wh_root / "reference/calendar.parquet").write_bytes(cal_bytes)
    _write_json(
        wh_root / "manifest.json",
        {
            "schema_version": "historical-index-warehouse-v1",
            "assets": {"000001.SZ": {"file": "assets/000001.parquet", "sha256": "abc"}},
            "reference_files": {"calendar.parquet": sha256_bytes(cal_bytes)},
        },
    )

    m_root = data / "shared-market/versions/v1/market-view"
    (m_root / "reference").mkdir(parents=True, exist_ok=True)
    (m_root / "reference/calendar.parquet").write_bytes(cal_bytes)
    _write_json(
        m_root / "manifest.json",
        {
            "schema_version": "historical-market-warehouse-v1",
            "assets": {"000001.SZ": {"file": "assets/000001.parquet", "sha256": "abc"}},
            "reference_files": {"calendar.parquet": sha256_bytes(cal_bytes)},
        },
    )
    (m_root.parent / "version.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(market_download, "warehouse_roots", lambda _root: [wh_root.resolve()])
    monkeypatch.setattr(market_universe, "market_roots", lambda _root: [m_root.resolve()])
    monkeypatch.setattr("finance_quant.web.system.market_roots", lambda _root: [m_root.resolve()])

    def fail_heavy(*_args, **_kwargs):
        raise AssertionError("dashboard summary must not invoke heavy parquet/table readers")

    monkeypatch.setattr("finance_quant.web.system.snapshot_registry", fail_heavy)
    monkeypatch.setattr("finance_quant.web.system.warehouse_option", fail_heavy)
    monkeypatch.setattr("finance_quant.web.system.market_option", fail_heavy)

    real_open = Path.open

    def forbid_payload_open(self, *args, **kwargs):
        if self.suffix == ".parquet":
            raise AssertionError(f"dashboard summary must not open payload file: {self}")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", forbid_payload_open)

    service = make_service(runs, data)
    calls = {"summary": 0}

    def fake_market_summary():
        calls["summary"] += 1
        return {"securities": 5453, "coverage_start": "2015-12-04", "coverage_end": "2026-11-19"}

    app = create_app(
        result_store=ResultStore(runs),
        task_service=service,
        market_status=fail_heavy,
        market_summary=fake_market_summary,
    )
    client = TestClient(app, base_url="http://127.0.0.1")

    first = client.get("/api/v1/dashboard/summary")
    assert first.status_code == 200
    assert len(first.content) < 1024
    body = first.json()
    assert body["database"] == {
        "securities": 5453,
        "coverage_start": "2015-12-04",
        "coverage_end": "2026-11-19",
    }
    assert body["counts"] == {"fixed": 1, "historical": 1, "market": 1, "invalid": 0}
    assert isinstance(body["as_of"], str) and len(body["as_of"]) > 10

    # Cached database summary does not re-query DB within TTL and preserves exact sampled as_of.
    time.sleep(0.02)
    second = client.get("/api/v1/dashboard/summary")
    assert second.status_code == 200
    assert calls["summary"] == 1
    assert second.json()["as_of"] == body["as_of"]


def test_dashboard_summary_rejects_non_object_corrupt_quarantined_and_checksum_mismatched_manifests(
    tmp_path, monkeypatch
):
    runs, data = tmp_path / "runs", tmp_path / "data"

    # 1 valid fixed snapshot.
    _write_valid_fixed_snapshot(data / "research/downloads/b_ok/snapshots/s_ok", "snap-ok")

    # Bad fixed manifests: JSON array, JSON scalar, invalid syntax, quarantined, missing file, checksum mismatch.
    _write_json(data / "research/downloads/b_arr/snapshots/s1/manifest.json", ["not", "an", "object"])
    _write_json(data / "research/downloads/b_num/snapshots/s1/manifest.json", 12345)
    bad_syntax = data / "research/downloads/b_badjson/snapshots/s1/manifest.json"
    bad_syntax.parent.mkdir(parents=True, exist_ok=True)
    bad_syntax.write_text("{broken json", encoding="utf-8")

    _write_valid_fixed_snapshot(data / "research/downloads/b_rej/snapshots/s1", "snap-rej")
    (data / "research/downloads/b_rej/snapshots/s1/rejected.json").write_text("{}", encoding="utf-8")

    _write_valid_fixed_snapshot(data / "research/downloads/b_missing_file/snapshots/s1", "snap-missing-file")
    (data / "research/downloads/b_missing_file/snapshots/s1/market.parquet").unlink()

    # Structured bad field types (unhashable list schema_version, bad files/codes/assets types).
    _write_json(
        data / "research/downloads/b_bad_schema/snapshots/s1/manifest.json",
        {"id": "snap-bad-schema", "schema_version": ["market-v4-shared"], "codes": ["000001.SZ"], "files": {}},
    )
    _write_json(
        data / "research/downloads/b_bad_files/snapshots/s1/manifest.json",
        {"id": "snap-bad-files", "schema_version": "market-v4-shared", "codes": ["000001.SZ"], "files": []},
    )

    # Bad historical and market manifests (non-object JSON, list schema_version, missing calendar file).
    wh_bad_arr = data / "historical/bad_arr"
    _write_json(wh_bad_arr / "manifest.json", ["bad"])

    wh_bad_schema = data / "historical/bad_schema"
    _write_json(
        wh_bad_schema / "manifest.json",
        {"schema_version": [], "assets": ["000001.SZ"], "reference_files": {"calendar.parquet": "abc"}},
    )

    m_missing_ref = data / "shared-market/versions/v_bad/market-view"
    _write_json(
        m_missing_ref / "manifest.json",
        {
            "schema_version": "historical-market-warehouse-v1",
            "assets": {"000001.SZ": {"file": "a.parquet", "sha256": "x"}},
            "reference_files": {"calendar.parquet": "abc"},
        },
    )
    (m_missing_ref.parent / "version.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        market_download, "warehouse_roots", lambda _root: [wh_bad_arr.resolve(), wh_bad_schema.resolve()]
    )
    monkeypatch.setattr("finance_quant.web.system.market_roots", lambda _root: [m_missing_ref.resolve()])

    app = make_app(
        runs, data_root=data, market_summary=lambda: {"securities": 0, "coverage_start": None, "coverage_end": None}
    )
    client = TestClient(app, base_url="http://127.0.0.1")

    res = client.get("/api/v1/dashboard/summary")
    assert res.status_code == 200
    assert res.json()["counts"] == {"fixed": 1, "historical": 0, "market": 0, "invalid": 10}


def test_dashboard_summary_concurrent_cold_requests_execute_loader_once(tmp_path):
    runs, data = tmp_path / "runs", tmp_path / "data"
    calls = {"count": 0}

    def slow_summary():
        calls["count"] += 1
        time.sleep(0.05)
        return {"securities": 42, "coverage_start": "2024-01-02", "coverage_end": "2024-12-31"}

    app = make_app(runs, data_root=data, market_summary=slow_summary)
    client = TestClient(app, base_url="http://127.0.0.1")

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: client.get("/api/v1/dashboard/summary"), range(6)))

    assert all(res.status_code == 200 for res in responses)
    assert calls["count"] == 1
    sampled_times = {res.json()["as_of"] for res in responses}
    assert len(sampled_times) == 1


def test_market_summary_handles_empty_daily_bar_table(monkeypatch):
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE daily_bar ("
                "ts_code VARCHAR(16) NOT NULL, "
                "trade_date DATE NOT NULL, "
                "PRIMARY KEY (ts_code, trade_date))"
            )
        )
        conn.execute(sa.text("CREATE INDEX ix_daily_bar_date ON daily_bar (trade_date)"))

    with engine.connect() as conn:
        monkeypatch.setattr(postgres_market, "connection", lambda: _DummyCtx(conn))
        summary = postgres_market.market_summary()

    assert summary == {"securities": 0, "coverage_start": None, "coverage_end": None}


def test_dashboard_summary_returns_503_on_failure_and_recovers_without_stale_error_cache(tmp_path):
    runs, data = tmp_path / "runs", tmp_path / "data"
    state = {"down": True}

    def flaky_summary():
        if state["down"]:
            raise OperationalError("stmt", {}, RuntimeError("connection refused"))
        return {"securities": 100, "coverage_start": "2024-01-02", "coverage_end": "2024-12-31"}

    app = make_app(runs, data_root=data, market_summary=flaky_summary)
    client = TestClient(app, base_url="http://127.0.0.1")

    failed = client.get("/api/v1/dashboard/summary")
    assert failed.status_code == 503

    state["down"] = False
    recovered = client.get("/api/v1/dashboard/summary")
    assert recovered.status_code == 200
    assert recovered.json()["database"]["securities"] == 100


def test_market_summary_avoids_full_table_scans_on_large_daily_bar_dataset(monkeypatch):
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE daily_bar ("
                "ts_code VARCHAR(16) NOT NULL, "
                "trade_date DATE NOT NULL, "
                "PRIMARY KEY (ts_code, trade_date))"
            )
        )
        conn.execute(sa.text("CREATE INDEX ix_daily_bar_date ON daily_bar (trade_date)"))
        base = dt.date(2020, 1, 2)
        conn.execute(
            sa.text("INSERT INTO daily_bar (ts_code, trade_date) VALUES (:c, :d)"),
            [{"c": f"{i:06d}.SZ", "d": base + dt.timedelta(days=d)} for i in range(5000) for d in range(20)],
        )

    executed: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        executed.append(" ".join(statement.split()))

    with engine.connect() as conn:
        monkeypatch.setattr(postgres_market, "connection", lambda: _DummyCtx(conn))
        t0 = time.perf_counter()
        summary = postgres_market.market_summary()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert summary == {
        "securities": 5000,
        "coverage_start": "2020-01-02",
        "coverage_end": "2020-01-21",
    }
    assert elapsed_ms < 250.0, f"Unexpectedly slow SQLite summary on 100k rows: {elapsed_ms:.2f}ms"
    assert len(executed) == 2
    assert "ORDER BY daily_bar.trade_date ASC LIMIT ?" in executed[0]
    assert "ORDER BY daily_bar.trade_date DESC LIMIT ?" in executed[0]
    for stmt in executed:
        assert "daily_bar_revision" not in stmt
        assert "factor_score" not in stmt
        assert "valuation" not in stmt
        assert "adjustment" not in stmt
        assert "trading_status" not in stmt
        assert "SELECT count(*) FROM daily_bar" not in stmt.lower()

    # PostgreSQL distinct-code path must use index skip-scan CTE, never full-table count(distinct).
    skip_sql = " ".join(str(postgres_market._DISTINCT_CODES_SKIP_SCAN).lower().split())
    assert "with recursive distinct_codes as" in skip_sql
    assert "order by ts_code limit 1" in skip_sql
    assert "order by d.ts_code limit 1" in skip_sql
    assert "count(distinct" not in skip_sql


def test_dashboard_summary_caches_inventory_with_single_flight_and_explicit_invalidation(tmp_path, monkeypatch):
    runs, data = tmp_path / "runs", tmp_path / "data"
    _write_valid_fixed_snapshot(data / "research/downloads/b1/snapshots/s1", "snap-1")

    scan_calls = {"count": 0}
    real_scan = web_system._dataset_counts

    def tracked_scan(root):
        scan_calls["count"] += 1
        time.sleep(0.03)
        return real_scan(root)

    monkeypatch.setattr(web_system, "_dataset_counts", tracked_scan)

    app = make_app(
        runs,
        data_root=data,
        market_summary=lambda: {"securities": 10, "coverage_start": "2024-01-02", "coverage_end": "2024-01-05"},
    )
    client = TestClient(app, base_url="http://127.0.0.1")

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: client.get("/api/v1/dashboard/summary"), range(6)))

    assert all(res.status_code == 200 for res in responses)
    assert scan_calls["count"] == 1
    first_body = responses[0].json()
    assert first_body["counts_basis"] == "publisher_manifests"
    assert first_body["cache_ttl_seconds"] == 30.0
    assert len({res.json()["counts_as_of"] for res in responses}) == 1
    assert first_body["counts"] == {"fixed": 1, "historical": 0, "market": 0, "invalid": 0}

    # Hot repeated request within TTL reuses cached inventory without rescanning.
    hot = client.get("/api/v1/dashboard/summary")
    assert hot.status_code == 200
    assert scan_calls["count"] == 1

    # Adding a new published snapshot directory updates container mtime and auto-invalidates cache.
    _write_valid_fixed_snapshot(data / "research/downloads/b1/snapshots/s2", "snap-2")
    added = client.get("/api/v1/dashboard/summary")
    assert added.status_code == 200
    assert scan_calls["count"] == 2
    assert added.json()["counts"]["fixed"] == 2

    # In-place marker change (rejected.json inside existing dir) waits for TTL unless invalidated explicitly.
    (data / "research/downloads/b1/snapshots/s2/rejected.json").write_text("{}", encoding="utf-8")
    assert client.get("/api/v1/dashboard/summary").json()["counts"]["fixed"] == 2
    assert scan_calls["count"] == 2

    # Unauthenticated ?refresh=true does not bypass cache/single-flight across concurrent requests.
    with ThreadPoolExecutor(max_workers=6) as pool:
        bypass_attempts = list(pool.map(lambda _: client.get("/api/v1/dashboard/summary?refresh=true"), range(6)))
    assert all(res.status_code == 200 for res in bypass_attempts)
    assert scan_calls["count"] == 2

    # Direct invalidation hook resets caches and sees updated rejected.json immediately.
    app.state.invalidate_dashboard_summary()
    refreshed = client.get("/api/v1/dashboard/summary")
    assert refreshed.status_code == 200
    assert scan_calls["count"] == 3
    assert refreshed.json()["counts"] == {"fixed": 1, "historical": 0, "market": 0, "invalid": 1}


def test_dashboard_summary_fails_on_inaccessible_inventory_and_blocks_inflight_repopulation(tmp_path, monkeypatch):
    runs, data = tmp_path / "runs", tmp_path / "data"
    _write_valid_fixed_snapshot(data / "research/downloads/b1/snapshots/s1", "snap-1")

    # 1. Top-level EACCES on inventory directory must return HTTP 503, never HTTP 200 with fixed=0.
    real_scandir = os.scandir

    def deny_downloads_scandir(target):
        if Path(target).resolve() == (data / "research/downloads").resolve():
            raise PermissionError(errno.EACCES, "Permission denied", str(target))
        return real_scandir(target)

    monkeypatch.setattr(os, "scandir", deny_downloads_scandir)
    app_err = make_app(
        runs,
        data_root=data,
        market_summary=lambda: {"securities": 10, "coverage_start": "2024-01-02", "coverage_end": "2024-01-05"},
    )
    assert TestClient(app_err, base_url="http://127.0.0.1").get("/api/v1/dashboard/summary").status_code == 503
    monkeypatch.undo()

    # 2. Explicit invalidation while an inventory scan is in-flight must prevent stale cache repopulation.
    entered = threading.Event()
    release = threading.Event()
    real_scan = web_system._dataset_counts
    scan_calls = {"count": 0}

    def gated_scan(root):
        scan_calls["count"] += 1
        result = real_scan(root)
        if scan_calls["count"] == 1:
            entered.set()
            assert release.wait(timeout=5)
        return result

    monkeypatch.setattr(web_system, "_dataset_counts", gated_scan)
    app = make_app(
        runs,
        data_root=data,
        market_summary=lambda: {"securities": 10, "coverage_start": "2024-01-02", "coverage_end": "2024-01-05"},
    )
    client = TestClient(app, base_url="http://127.0.0.1")

    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(lambda: client.get("/api/v1/dashboard/summary"))
        assert entered.wait(timeout=5)
        # In-place rejected.json keeps container sig identical; epoch must block stale repopulation.
        (data / "research/downloads/b1/snapshots/s1/rejected.json").write_text("{}", encoding="utf-8")
        app.state.invalidate_dashboard_summary()
        release.set()
        first = fut.result(timeout=5)

    assert first.status_code == 200
    assert first.json()["counts"]["fixed"] == 1
    # Next request must NOT serve the invalidated stale count (1); it rescans and sees 0 valid / 1 invalid.
    second = client.get("/api/v1/dashboard/summary")
    assert second.status_code == 200
    assert scan_calls["count"] == 2
    assert second.json()["counts"] == {"fixed": 0, "historical": 0, "market": 0, "invalid": 1}


def test_dashboard_summary_rejects_ancestor_symlink_redirected_outside_root(tmp_path, monkeypatch):
    runs, data, outside = tmp_path / "runs", tmp_path / "data", tmp_path / "outside"
    _write_valid_fixed_snapshot(outside / "snapshots/ext-1", "snap-outside")

    snapshots_link = data / "research/downloads/b1/snapshots"
    snapshots_link.parent.mkdir(parents=True, exist_ok=True)
    # Simulate an ancestor (`snapshots` or `downloads`) whose resolved path escapes `data_root`.
    real_confined = web_system._confined_dir_stat

    def outside_ancestor(path, root):
        if Path(path) == snapshots_link:
            return None
        return real_confined(path, root)

    # Also test real symlink when host permits, falling back to monkeypatched resolution escape check.
    try:
        os.symlink(outside / "snapshots", snapshots_link, target_is_directory=True)
    except OSError:
        snapshots_link.mkdir(parents=True, exist_ok=True)
        _write_valid_fixed_snapshot(snapshots_link / "ext-1", "snap-outside")
        monkeypatch.setattr(web_system, "_confined_dir_stat", outside_ancestor)

    app = make_app(
        runs, data_root=data, market_summary=lambda: {"securities": 0, "coverage_start": None, "coverage_end": None}
    )
    res = TestClient(app, base_url="http://127.0.0.1").get("/api/v1/dashboard/summary")
    assert res.status_code == 200
    assert res.json()["counts"] == {"fixed": 0, "historical": 0, "market": 0, "invalid": 0}


class _DummyCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *_args):
        return False
