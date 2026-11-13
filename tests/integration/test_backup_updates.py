"""Full update -> backup -> empty-database restore -> incremental reuse, no live APIs."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from test_data_updates import CombinedProvider, spec
from test_joint_data import Secondary

from finance_quant import backup
from finance_quant.application import (
    backup_jobs,
    data_updates,
    financial_download,
    industry_download,
    market_download,
    task_worker,
)
from finance_quant.data import benchmarks, industries, postgres_market, shared_market
from finance_quant.data.financials import FinancialStore
from finance_quant.persistence import database
from finance_quant.persistence.market import MarketRepository
from finance_quant.persistence.results import PgResultStore
from finance_quant.persistence.tasks import PgTaskStore, worker_lock


def configure(monkeypatch, root, engine):
    for name, value in {
        "STORAGE_ROOT": root,
        "FINANCE_QUANT_DATA": root / "data",
        "FINANCE_QUANT_RUNS": root / "runs",
        "FINANCE_QUANT_INDUSTRY_ROOT": root / "data/industries/official-tushare",
        "FINANCE_QUANT_BENCHMARK_ROOT": root / "data/benchmarks/official-tushare",
        "FINANCE_QUANT_QUOTA_DB": root / "network/quota.sqlite3",
    }.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.setattr(database, "make_engine", lambda: engine)
    monkeypatch.setattr(postgres_market, "repository", lambda: MarketRepository(engine))
    shared_market.configure_store(postgres_market.PostgresMarketStore())


def test_update_backup_restore_and_incremental_reuse(empty_databases, tmp_path, monkeypatch):
    source, target = empty_databases
    source_root, restored_root = tmp_path / "source", tmp_path / "restored"
    configure(monkeypatch, source_root, source)
    command.upgrade(Config("alembic.ini"), "head")
    provider = CombinedProvider()
    monkeypatch.setattr(market_download, "official_provider", lambda: provider)
    monkeypatch.setattr(market_download, "BaoStockProvider", lambda *a, **kw: Secondary())
    monkeypatch.setattr(financial_download, "default_provider", lambda: provider)
    monkeypatch.setattr(industry_download, "default_provider", lambda: provider)
    monkeypatch.setattr(benchmarks, "TushareProvider", lambda settings: provider)
    monkeypatch.setenv("TUSHARE_TOKEN", "isolated-test-no-network")
    data = source_root / "data"
    document = spec()
    plan = data_updates.preview(data, document, provider=provider)
    queue = PgTaskStore(source_root / "runs", engine=source)
    task = data_updates.start_task(data, queue, plan["id"])
    assert queue.claim() == task["id"]
    task_worker.run_task(
        source_root / "runs",
        data,
        task["id"],
        task_store=queue,
        result_store_factory=lambda _: None,
        storage_root=source_root,
    )
    saved = queue.get(task["id"])
    assert saved["status"] == "succeeded", saved
    result = saved["result"]
    assert set(result["steps"]) == {"market", "financial", "industry", "benchmarks"}
    stored_result = {
        "metadata": {
            "id": "backup-evaluation",
            "name": "preserved evaluation",
            "is_synthetic": True,
            "created_at": "2025-01-01T00:00:00Z",
            "config": {},
        },
        "methodology": {"label": "backup test"},
    }
    PgResultStore(source_root / "runs", engine=source, storage_root=source_root).publish(
        "evaluations", "backup-evaluation", stored_result
    )
    financial_version = FinancialStore(data / "financials").current()["id"]
    industry_version = industries.load_industries().manifest["id"]
    benchmark_digest = benchmarks.load_series("H00300.CSI")[1]["series_sha256"]
    # Exercise restore's queue and schedule handling without starting a worker.
    pending = queue.create_download("data_update", {"name": "pending"}, {"spec": document}, "backup-pending")
    data_updates.save_schedule(data, {"enabled": True, "hour": 19, "spec": document})
    marker = data / "financials/jobs/preserved-checkpoint.json"
    marker.write_text('{"streams":["income:1"]}', encoding="utf-8")
    destination = tmp_path / "backup"
    manifest = backup.create(destination, root=source_root, engine=source)
    assert "data/financials/jobs/preserved-checkpoint.json" in manifest["files"]
    backup.verify(destination)
    with worker_lock(source):
        with pytest.raises(RuntimeError):
            backup.create(tmp_path / "while-worker-running", root=source_root, engine=source)
    backup.restore(destination, root=restored_root, engine=target)
    configure(monkeypatch, restored_root, target)
    assert FinancialStore(restored_root / "data/financials").current()["id"] == financial_version
    assert industries.load_industries().manifest["id"] == industry_version
    assert benchmarks.load_series("H00300.CSI")[1]["series_sha256"] == benchmark_digest
    assert (restored_root / "data/financials/jobs/preserved-checkpoint.json").read_text(
        encoding="utf-8"
    ) == marker.read_text(encoding="utf-8")
    assert PgTaskStore(restored_root / "runs", engine=target).get(pending["id"])["status"] == "interrupted"
    assert PgResultStore(restored_root / "runs", engine=target, storage_root=restored_root).get(
        "evaluations", "backup-evaluation"
    )["methodology"] == {"label": "backup test"}
    assert data_updates.status(restored_root / "data")["schedule"]["enabled"] is False
    with source.connect() as left, target.connect() as right:
        for name in ("daily_bar", "valuation", "adjustment", "index_member", "data_release", "data_revision"):
            assert (
                left.execute(text(f"SELECT count(*) FROM {name}")).scalar_one()
                == right.execute(text(f"SELECT count(*) FROM {name}")).scalar_one()
            )
    # A restored full update must reuse stored quotes and financial observations.
    before_quotes = len([c for c in provider.calls if c[0] == "daily"])
    before_financial = len([c for c in provider.financial.calls if c[0] != "trade_cal"])
    document["industry"]["refresh"] = False
    next_plan = data_updates.preview(restored_root / "data", document, provider=provider)
    next_result = data_updates.run(restored_root / "data", next_plan["id"], provider=provider, secondary=Secondary())
    assert next_result["status"] == "complete", next_result
    assert len([c for c in provider.calls if c[0] == "daily"]) == before_quotes
    assert len([c for c in provider.financial.calls if c[0] != "trade_cal"]) == before_financial
    # A nonempty database can never be overwritten, even when the file target is empty.
    with pytest.raises(ValueError, match="空数据库"):
        backup.restore(destination, root=tmp_path / "other-empty-target", engine=target)


def test_web_maintenance_queue_creates_verifies_and_restores_separate_database(empty_databases, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from finance_quant.api import create_app
    from finance_quant.application.tasks import TaskService

    source, target = empty_databases
    storage = tmp_path / "storage"
    configure(monkeypatch, storage, source)
    monkeypatch.setenv("BACKUP_ROOT", str(tmp_path / "backups"))
    monkeypatch.setenv("RESTORE_ROOT", str(tmp_path / "restores"))
    command.upgrade(Config("alembic.ini"), "head")
    (storage / "data").mkdir(parents=True)
    (storage / "data/preserved.txt").write_text("keep financial and industry", encoding="utf-8")
    queue = PgTaskStore(storage / "runs", engine=source)
    service = TaskService(storage / "runs", storage / "data", task_store=queue)
    client = TestClient(
        create_app(
            result_store=PgResultStore(storage / "runs", engine=source, storage_root=storage), task_service=service
        ),
        base_url="http://127.0.0.1",
    )

    def perform(document):
        response = client.post("/api/v1/research/backups", json=document)
        assert response.status_code == 200, response.text
        task = response.json()
        assert queue.claim() == task["id"]
        with worker_lock(source):
            task_worker.run_task(
                storage / "runs",
                storage / "data",
                task["id"],
                task_store=queue,
                result_store_factory=lambda _: None,
                storage_root=storage,
            )
        saved = queue.get(task["id"])
        assert saved["status"] == "succeeded", saved
        return saved["result"]

    from finance_quant.persistence.maintenance import write_gate

    with write_gate(source, exclusive=True):
        assert client.post("/api/v1/research/backups", json={"action": "create"}).status_code == 409

    created = perform({"action": "create"})
    identifier = created["backup_id"]
    assert client.get("/api/v1/research/backups").json()["backups"][0]["id"] == identifier
    perform({"action": "verify", "backup_id": identifier})
    refused = client.post(
        "/api/v1/research/backups", json={"action": "restore", "backup_id": identifier, "database": source.url.database}
    )
    assert refused.status_code == 422
    # Name validation is mandatory even though the underlying empty test fixture uses fq_backup_*.
    with pytest.raises(ValueError):
        backup_jobs.validate_request(
            {"action": "restore", "backup_id": identifier, "database": "finance_quant_dev"}, source.url.database
        )
    # A third database has a web-approved name and is created only on this isolated server.
    name = "fq_restore_" + identifier.removeprefix("backup-")
    try:
        restored = perform({"action": "restore", "backup_id": identifier, "database": name})
        assert restored["database"] == name
        assert (tmp_path / "restores" / name / "data/preserved.txt").read_text(
            encoding="utf-8"
        ) == "keep financial and industry"
        assert (storage / "data/preserved.txt").read_text(encoding="utf-8") == "keep financial and industry"
        again = client.post(
            "/api/v1/research/backups", json={"action": "restore", "backup_id": identifier, "database": name}
        )
        assert again.status_code == 422
    finally:
        with source.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            assert name.startswith("fq_restore_")
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
