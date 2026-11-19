"""Download -> shared publication -> research -> resume on an isolated PostgreSQL schema."""

import os

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from test_download_recovery import CdrProvider
from test_joint_data import Secondary
from test_postgres_runtime import engine as engine
from test_shared_market import SharedProvider, quote_calls, spec

from finance_quant.api import create_app
from finance_quant.application import market_download, quote_capture
from finance_quant.application.task_worker import run_task
from finance_quant.application.tasks import TaskService
from finance_quant.data import postgres_market, shared_market
from finance_quant.data.market import load_market
from finance_quant.data.market_universe import load_market_input, market_option, market_roots
from finance_quant.persistence.market import MarketRepository
from finance_quant.persistence.results import PgResultStore
from finance_quant.persistence.tasks import PgTaskStore
from finance_quant.research.universe import UniverseFilters

pytestmark = pytest.mark.skipif(
    os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1", reason="requires isolated PostgreSQL"
)


class EventProvider(SharedProvider):
    def query(self, api, **params):
        frame = super().query(api, **params)
        if params.get("ts_code") != "600519.SH":
            return frame
        if api == "namechange":
            return pd.concat(
                [
                    frame,
                    pd.DataFrame(
                        [
                            {
                                "ts_code": "600519.SH",
                                "name": "退市测试",
                                "start_date": "20250212",
                                "end_date": None,
                                "ann_date": "20250205",
                                "change_reason": "退市整理",
                            }
                        ]
                    ),
                ],
                ignore_index=True,
            )
        if api == "daily":
            frame.loc[frame.trade_date.ge("20250212"), ["open", "high", "low", "close"]] *= 0.2
            frame.loc[frame.trade_date.gt("20250212"), "pre_close"] *= 0.2
            frame.loc[frame.trade_date.eq("20250212"), "pct_chg"] = -80
        return frame


def test_repaired_event_download_snapshot_backtest_and_cached_resume(engine, tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    data, runs = storage / "data", storage / "runs"
    repository = MarketRepository(engine)
    monkeypatch.setattr(postgres_market, "repository", lambda: repository)
    shared_market.configure_store(postgres_market.PostgresMarketStore())
    monkeypatch.setattr(quote_capture, "sample_code", lambda code: False)
    provider = EventProvider()
    root = market_download.shared_root(data)
    plan = market_download.prepare(root, spec("all_market", "2025-03-10", codes=[]), provider=provider)
    result = market_download.run(root, plan["id"], provider=provider, secondary=Secondary(error=True))
    assert result["status"] == "complete", result
    repaired = [record for record in result["repairs"] if record["action"] == "verified_unbounded_session"]
    assert len(repaired) == 1 and repaired[0]["dates"] == ["2025-02-12"]
    snapshot = data / "research/downloads" / ("shared-" + result["version_id"])
    market, _, _, _ = load_market(snapshot)
    event_row = market.loc[market.instrument.eq("SH600519") & market.date.eq("2025-02-12")].iloc[0]
    assert event_row.reviewed_unbounded_session and event_row.close == 2 and event_row.change == -0.8
    option = market_option(market_roots(data)[0])
    queue = PgTaskStore(runs, engine=engine)
    service = TaskService(runs, data, task_store=queue)
    store = PgResultStore(runs, engine=engine, storage_root=storage)
    client = TestClient(create_app(result_store=store, task_service=service), base_url="http://127.0.0.1")
    assert client.get("/api/v1/research/market-download").json()["jobs"][0]["repairs"] == result["repairs"]
    request = {
        "name": "修复行情研究",
        "data": {"mode": "market_warehouse", "warehouse_id": option["id"], "filters": {"min_listing_days": 0}},
        "backtest": {"start": "2025-01-10", "end": "2025-03-10", "topk": 1},
        "factors": [{"name": "momentum", "window": 2, "weight": 1}],
        "evaluation": {"horizons": [1], "groups": 2, "min_cross_section": 3},
    }
    response = client.post("/api/v1/research/tasks", json=request, headers={"Idempotency-Key": "recovery-research"})
    assert response.status_code == 202, response.text
    identifier = response.json()["id"]
    assert queue.claim() == identifier
    run_task(
        runs,
        data,
        identifier,
        task_store=queue,
        result_store_factory=lambda dataset_id: PgResultStore(
            runs, engine=engine, storage_root=storage, dataset_id=dataset_id
        ),
        market_repository=repository,
        storage_root=storage,
    )
    done = queue.get(identifier)
    assert done["status"] == "succeeded", done
    backtest_id = done["result"]["backtest_run_id"]
    digest = store.digest("backtests", backtest_id)
    assert client.get("/api/v1/backtests/" + backtest_id).status_code == 200
    assert store.get("backtests", backtest_id)["equity"]
    before = quote_calls(provider).copy()
    again = market_download.run(root, plan["id"], provider=provider, secondary=Secondary(error=True))
    assert again["status"] == "complete" and quote_calls(provider) == before
    assert again["repairs"] == result["repairs"]
    assert store.digest("backtests", backtest_id) == digest


def test_cdr_archive_publication_and_resume_keep_equity_research_supported(engine, tmp_path, monkeypatch):
    data = tmp_path / "data"
    repository = MarketRepository(engine)
    monkeypatch.setattr(postgres_market, "repository", lambda: repository)
    shared_market.configure_store(postgres_market.PostgresMarketStore())
    monkeypatch.setattr(quote_capture, "sample_code", lambda code: False)
    provider = CdrProvider()
    root = market_download.shared_root(data)
    plan = market_download.prepare(root, spec("all_market", codes=[]), provider=provider)
    result = market_download.run(root, plan["id"], provider=provider, secondary=Secondary(error=True))
    assert result["status"] == "complete" and result["completed"] == 4, result
    archived, _ = shared_market.read_asset(
        root, "689009.SH", pd.bdate_range("2025-01-06", "2025-02-28"), plan["provider_id"]
    )
    assert archived.reviewed_unbounded_session.iloc[:5].all()
    assert archived.validation_level.eq("single_source_passed").all()
    [view] = market_roots(data)
    quotes, _, _, _ = load_market_input(
        view, "2025-01-10", "2025-02-28", 2, filters=UniverseFilters(min_listing_days=0)
    )
    assert "SH689009" not in set(quotes.instrument) and quotes.instrument.nunique() == 3
    runs = tmp_path / "runs"
    queue = PgTaskStore(runs, engine=engine)
    service = TaskService(runs, data, task_store=queue)
    store = PgResultStore(runs, engine=engine, storage_root=tmp_path)
    client = TestClient(create_app(result_store=store, task_service=service), base_url="http://127.0.0.1")
    request = {
        "name": "含CDR归档的普通股票研究",
        "data": {
            "mode": "market_warehouse",
            "warehouse_id": market_option(view)["id"],
            "filters": {"min_listing_days": 0},
        },
        "backtest": {"start": "2025-01-10", "end": "2025-02-28", "topk": 1},
        "factors": [{"name": "momentum", "window": 2, "weight": 1}],
        "evaluation": {"horizons": [1], "groups": 2, "min_cross_section": 3},
    }
    submitted = client.post("/api/v1/research/tasks", json=request, headers={"Idempotency-Key": "cdr-archive-research"})
    assert submitted.status_code == 202, submitted.text
    identifier = submitted.json()["id"]
    assert queue.claim() == identifier
    run_task(
        runs,
        data,
        identifier,
        task_store=queue,
        result_store_factory=lambda dataset_id: PgResultStore(
            runs, engine=engine, storage_root=tmp_path, dataset_id=dataset_id
        ),
        market_repository=repository,
        storage_root=tmp_path,
    )
    done = queue.get(identifier)
    assert done["status"] == "succeeded", done
    backtest_id = done["result"]["backtest_run_id"]
    backtest = store.get("backtests", backtest_id)
    assert backtest["equity"] and backtest["trades"]
    assert all(trade["instrument"] != "SH689009" for trade in backtest["trades"])
    factors = store.get("factors", done["result"]["factor_run_id"])
    assert {row["instrument"] for row in factors["history"]} == {"SH600000", "SH600519", "SZ000001"}
    assert client.get("/api/v1/backtests/" + backtest_id).status_code == 200
    digest = store.digest("backtests", backtest_id)
    calls = quote_calls(provider)
    again = market_download.run(root, plan["id"], provider=provider, secondary=Secondary(error=True))
    assert again["status"] == "complete" and quote_calls(provider) == calls
    assert store.digest("backtests", backtest_id) == digest
