import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from finance_quant.storage import ResultStore
from finance_quant.storage_tables import publish_tables
from support.fakes import make_app
from support.synthetic import sample_factors, sample_prices


@pytest.mark.parametrize("frequency", ["daily", "weekly", "monthly"])
def test_terminal_calendar_bar_needs_no_fabricated_next_session(tmp_path, frequency):
    from finance_quant.integrations.qlib_runner import run_qlib

    days = pd.to_datetime(["2024-01-04", "2024-01-05", "2024-01-08"])
    quotes = pd.concat(
        [
            pd.DataFrame(
                {
                    "date": days,
                    "instrument": code,
                    "open": 10.0,
                    "close": 10.0,
                    "high": 10.0,
                    "low": 10.0,
                    "volume": 100000.0,
                    "factor": 1.0,
                    "change": 0.0,
                    "up_limit": 11.0,
                    "down_limit": 9.0,
                }
            )
            for code in ["SH600000", "SZ000001", "SH000300"]
        ],
        ignore_index=True,
    )
    factors = {
        "metadata": {"id": "edge"},
        "history": [
            {"date": "2024-01-04", "instrument": "SH600000", "composite": 100.0},
            {"date": "2024-01-05", "instrument": "SH600000", "composite": 100.0},
        ],
    }
    values = dict(
        start="2024-01-05",
        end="2024-01-08",
        initial_date="2024-01-04",
        names={"SH600000": "A", "SZ000001": "B"},
        topk=1,
        rebalance_frequency=frequency,
    )
    short = run_qlib(quotes, factors, tmp_path / "short", calendar=days, **values)
    full = run_qlib(
        quotes, factors, tmp_path / "long", calendar=days.append(pd.DatetimeIndex(["2024-01-09"])), **values
    )
    assert len(short["equity"]) == 3 and short["equity"] == full["equity"]
    assert short["trades"] == full["trades"] and short["trades"]
    assert (tmp_path / "short/calendars/day_future.txt").read_text(encoding="utf-8").splitlines() == days.strftime(
        "%Y-%m-%d"
    ).tolist()
    with pytest.raises(ValueError, match="实际交易日历"):
        run_qlib(quotes, factors, tmp_path / "bad", calendar=days[1:], **values)


def test_exchange_direct_impact_parameter_is_applied_without_final_volume_lookahead(tmp_path):
    import qlib
    from qlib.backtest.decision import Order
    from qlib.backtest.position import Position

    from finance_quant.integrations.execution import CostAwareExchange
    from finance_quant.integrations.qlib_runner import write_features

    days = pd.to_datetime(["2024-01-04", "2024-01-05"])
    quotes = pd.DataFrame(
        {
            "date": days,
            "instrument": "SH600000",
            "open": 10.0,
            "close": 10.0,
            "high": 10.0,
            "low": 10.0,
            "volume": [100.0, 1e9],
            "factor": 1.0,
            "change": 0.0,
            "up_limit": 11.0,
            "down_limit": 9.0,
        }
    )
    write_features(quotes, days, tmp_path / "data")
    qlib.init(provider_uri=str(tmp_path / "data"), region="cn", kernels=1, expression_cache=None, dataset_cache=None)
    exchange = CostAwareExchange(
        start_time=days[0],
        end_time=days[-1],
        codes=["SH600000"],
        deal_price="$open",
        limit_threshold=("$open < 0", "$open < 0"),
        open_cost=0.0,
        close_cost=0.0,
        min_cost=0.0,
        impact_cost=0.001,
    )
    assert exchange.execution_costs.impact_cost == 0.001
    for day in days:
        position = Position(cash=2000.0)
        order = Order(stock_id="SH600000", amount=100, direction=Order.BUY, start_time=day, end_time=day)
        value, cost, _ = exchange.deal_order(order, position=position)
        assert value == 1000.0 and cost == pytest.approx(1.0) and position.get_cash() == pytest.approx(999.0)


def test_metadata_listing_is_small_cached_and_invalidates_on_changes(tmp_path, monkeypatch):
    store = ResultStore(tmp_path)
    factors = sample_factors(sample_prices())
    identifier = factors["metadata"]["id"]
    path = store.publish("factors", identifier, factors)
    original_read = Path.read_text
    calls = []

    def read(self, *args, **kwargs):
        if self == path:
            calls.append(self)
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    first, bad = store.list_metadata("factors")
    assert not bad and len(first) == 1
    assert "history" not in first[0] and first[0]["metadata"]["available_dates"]
    first[0]["metadata"]["weights"]["momentum"] = 999
    second, bad = store.list_metadata("factors")
    assert not bad and len(calls) == 1
    assert second[0]["metadata"]["weights"]["momentum"] != 999
    factors["metadata"]["name"] = "changed metadata"
    path.write_text(json.dumps(factors), encoding="utf-8")
    third, bad = store.list_metadata("factors")
    assert not bad and len(calls) == 2
    assert third[0]["metadata"]["name"] == "changed metadata"
    path.write_text("{broken", encoding="utf-8")
    healthy, bad = store.list_metadata("factors")
    assert healthy == [] and bad[0]["id"] == identifier


def test_bad_result_does_not_break_healthy_api_and_broken_tables_are_explicit(tmp_path):
    store = ResultStore(tmp_path)
    factors = sample_factors(sample_prices())
    identifier = factors["metadata"]["id"]
    publish_tables(store, "factors", identifier, factors, ["history"])
    bad_path = store.path("factors", "broken-result")
    bad_path.parent.mkdir()
    bad_path.write_text("{broken", encoding="utf-8")
    bad_metadata = json.loads(json.dumps(factors))
    bad_metadata["metadata"]["created_at"] = 123
    store.publish("factors", "bad-created-time", bad_metadata)
    client = TestClient(make_app(tmp_path))
    listing = client.get("/api/v1/factor-runs")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1 and {r["id"] for r in listing.json()["unavailable"]} == {
        "broken-result",
        "bad-created-time",
    }
    overview = client.get("/api/v1/overview").json()
    assert overview["factor_runs"] == 1 and "隔离" in overview["notice"]
    assert client.get("/api/v1/factor-runs/broken-result/scores").status_code == 409
    assert client.get(f"/api/v1/factor-runs/{identifier}/scores").status_code == 200
    compact = store.get("factors", identifier, hydrate=False)
    table = store.path("factors", identifier).parent / compact["tables"]["history"]["file"]
    table.write_bytes(b"corrupted data")
    response = client.get(f"/api/v1/factor-runs/{identifier}/scores")
    assert response.status_code == 409 and "损坏" in response.json()["detail"]
    assert client.get(f"/api/v1/factor-runs/{identifier}/tables/history").status_code == 409
    table.unlink()
    assert client.get(f"/api/v1/factor-runs/{identifier}/scores").status_code == 409


def test_historical_report_versions_do_not_change_factors_or_backtest(tmp_path, monkeypatch):
    from test_historical_workflow import request, warehouse_fixture

    from finance_quant.application import historical_workflow as workflow

    root = tmp_path / "warehouse"
    warehouse_fixture(root)
    first = workflow.run_historical_research(request(), root, tmp_path / "runs")
    store = ResultStore(tmp_path / "runs")
    report = store.path("evaluations", first["evaluation_run_id"]).with_name("report.md").read_text(encoding="utf-8")
    assert "历史动态成分池" in report and "不是每天全部参与排名" in report and "Parquet" in report
    monkeypatch.setattr(workflow, "report_code_sha256", lambda: "report-revision")
    second = workflow.run_historical_research(request(), root, tmp_path / "runs")
    assert second["factor_run_id"] == first["factor_run_id"] and second["backtest_run_id"] == first["backtest_run_id"]
    assert second["backtest_reused"] and second["evaluation_run_id"] != first["evaluation_run_id"]
