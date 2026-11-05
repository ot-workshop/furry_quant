"""Backend selection stays at composition roots instead of constructors."""

import pytest

from finance_quant.data import postgres_market
from finance_quant.storage import ResultStore


def test_file_result_store_does_not_switch_from_postgres_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "database.example")

    store = ResultStore(tmp_path)

    assert type(store) is ResultStore
    assert store.root == tmp_path.resolve()


def test_market_store_requires_explicit_configuration_and_is_visible_to_threads(monkeypatch):
    import threading

    from finance_quant.data import shared_market

    monkeypatch.setenv("POSTGRES_HOST", "database.example")
    shared_market.configure_store(None)
    with pytest.raises(RuntimeError, match="未配置"):
        shared_market.market_store()
    store = postgres_market.PostgresMarketStore()
    shared_market.configure_store(store)
    seen = []
    worker = threading.Thread(target=lambda: seen.append(shared_market.market_store()))
    worker.start()
    worker.join()
    assert seen == [store]


def test_initial_migration_is_frozen_and_does_not_import_runtime_schema():
    migration = (
        __import__("pathlib").Path(__file__).parents[1] / "alembic" / "versions" / "0001_postgres.py"
    ).read_text(encoding="utf-8")

    assert "finance_quant.persistence.schema" not in migration
    assert "metadata.create_all" not in migration
    assert 'op.create_table(\n        "research_task"' in migration
