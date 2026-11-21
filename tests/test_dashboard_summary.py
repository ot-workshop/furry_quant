import datetime as dt
import json
import time
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

    _write_valid_fixed_snapshot(data / "research/downloads/b_corrupt/snapshots/s1", "snap-corrupt")
    (data / "research/downloads/b_corrupt/snapshots/s1/market.parquet").write_bytes(b"corrupted!")

    # Bad historical and market manifests (non-object JSON and wrong calendar checksum).
    wh_bad_arr = data / "historical/bad_arr"
    _write_json(wh_bad_arr / "manifest.json", ["bad"])

    m_bad_sha = data / "shared-market/versions/v_bad/market-view"
    (m_bad_sha / "reference").mkdir(parents=True, exist_ok=True)
    (m_bad_sha / "reference/calendar.parquet").write_bytes(b"actual-bytes")
    _write_json(
        m_bad_sha / "manifest.json",
        {
            "schema_version": "historical-market-warehouse-v1",
            "assets": {"000001.SZ": {"file": "a.parquet", "sha256": "x"}},
            "reference_files": {"calendar.parquet": "0" * 64},
        },
    )
    (m_bad_sha.parent / "version.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(market_download, "warehouse_roots", lambda _root: [wh_bad_arr.resolve()])
    monkeypatch.setattr("finance_quant.web.system.market_roots", lambda _root: [m_bad_sha.resolve()])

    app = make_app(
        runs, data_root=data, market_summary=lambda: {"securities": 0, "coverage_start": None, "coverage_end": None}
    )
    client = TestClient(app, base_url="http://127.0.0.1")

    res = client.get("/api/v1/dashboard/summary")
    assert res.status_code == 200
    assert res.json()["counts"] == {"fixed": 1, "historical": 0, "market": 0, "invalid": 7}


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


class _DummyCtx:
    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *_args):
        return False
