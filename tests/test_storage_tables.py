import pytest
from test_historical_workflow import warehouse_fixture

from finance_quant.data.historical_warehouse import load_historical_input
from finance_quant.research.market_factors import market_factors
from finance_quant.research.registry import FactorConfig, FactorSpec
from finance_quant.storage import ResultStore
from finance_quant.storage_tables import StreamingTables, factor_frame, publish_tables, read_rows


def test_large_result_tables_roundtrip_exact_and_filter_without_full_hydration(tmp_path):
    store = ResultStore(tmp_path)
    history = [
        {
            "date": f"2024-01-{i % 9 + 1:02d}",
            "instrument": f"SH{600000 + i % 3}",
            "raw": {"momentum": i / 100},
            "scores": {"momentum": 50.0},
            "contributions": {"momentum": 50.0},
            "composite": 50.0,
            "rank": 1,
        }
        for i in range(18000)
    ]
    diagnostics = [
        {"date": "2024-01-01", "instrument": "SH600003", "raw": {"momentum": None}, "reasons": {"momentum": "history"}},
        {
            "date": "2024-01-02",
            "instrument": "SH600003",
            "raw": {"momentum": 1.0},
            "reasons": {"_eligibility": "history"},
        },
    ]
    result = {
        "metadata": {"id": "large-test", "universe_members": [{"instrument": "SH600000", "name": "fixture"}]},
        "history": history,
        "diagnostics": diagnostics,
    }
    publish_tables(store, "factors", "large-test", result, ["history", "diagnostics"])
    compact = store.get("factors", "large-test", hydrate=False)
    assert compact["history"] == [] and compact["tables"]["history"]["rows"] == 18000
    assert len(result["history"]) == 18000  # Publishing cannot consume the working signal.
    assert store.list("factors")[0]["history"] == []
    selected, total = read_rows(
        store, "factors", "large-test", compact, "history", filters={"date": "2024-01-01"}, offset=3, limit=5
    )
    expected = [r for r in history if r["date"] == "2024-01-01"]
    assert total == len(expected) and selected == expected[3:8]
    hydrated = store.get("factors", "large-test")
    assert hydrated["history"] == history and hydrated["diagnostics"] == diagnostics
    publish_tables(store, "factors", "large-test", result, ["history", "diagnostics"])
    path = store.path("factors", "large-test").parent / compact["tables"]["history"]["file"]
    path.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        read_rows(store, "factors", "large-test", compact, "history", limit=5)


def test_nested_label_tables_are_explicitly_indexed_and_strict_json_remains_atomic(tmp_path):
    store = ResultStore(tmp_path)
    rows = [{"date": "2024-01-01", "instrument": "SH600000", "forward_return": 0.2}]
    result = {"metadata": {"id": "eval-test"}, "labels": {"5": {"observations": rows, "valid_observations": 1}}}
    publish_tables(store, "evaluations", "eval-test", result, ["labels/5/observations"])
    saved = store.get("evaluations", "eval-test", hydrate=False)
    assert (
        saved["labels"]["5"]["observations"] == [] and saved["labels"]["5"]["observations_storage"] == "external_table"
    )
    assert store.get("evaluations", "eval-test")["labels"]["5"]["observations"] == rows
    with pytest.raises(ValueError):
        store.publish("backtests", "invalid", {"value": float("nan")})
    assert not store.path("backtests", "invalid").exists()
    store.publish("backtests", "ordered", {"a": 1, "b": 2})
    store.publish("backtests", "ordered", {"b": 2, "a": 1})
    with pytest.raises(FileExistsError):
        store.publish("backtests", "ordered", {"a": 3})


def test_streamed_daily_scores_equal_in_memory_and_partial_failure_is_not_published(tmp_path):

    root = tmp_path / "warehouse"
    warehouse_fixture(root, blocked=True)
    market, _, calendar, meta = load_historical_input(root, "2024-01-08", "2024-01-31", 2)
    config = FactorConfig((FactorSpec("momentum", 1.0, 2),))
    expected = market_factors(market, meta["id"], config, dynamic_context=meta, exchange_calendar=calendar)
    store = ResultStore(tmp_path / "runs")
    with StreamingTables(store, "factors", "streamed") as tables:
        actual = market_factors(
            market, meta["id"], config, dynamic_context=meta, exchange_calendar=calendar, row_sink=tables.append
        )
        assert actual["history"] == actual["diagnostics"] == []
        actual["tables"] = tables.finish()
    store.publish("factors", "streamed", actual)
    saved = store.get("factors", "streamed")
    assert saved["metadata"] == expected["metadata"]
    assert saved["history"] == expected["history"] and saved["diagnostics"] == expected["diagnostics"]
    compact = store.get("factors", "streamed", hydrate=False)
    frame = factor_frame(store, "streamed", compact, include_raw=True)
    assert frame.to_dict("records") == [{k: r[k] for k in frame.columns} for r in expected["history"]]
    assert "raw" not in factor_frame(store, "streamed", compact).columns
    with pytest.raises(ValueError):
        with StreamingTables(store, "factors", "failed") as tables:
            tables.append("history", [{"composite": float("nan")}])
            tables.finish()
    assert not store.path("factors", "failed").exists()
    assert not list(store.path("factors", "failed").parent.glob("*.parquet"))
