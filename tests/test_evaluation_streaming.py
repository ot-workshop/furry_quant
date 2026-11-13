import json

import pandas as pd
import pytest
from scipy.stats import spearmanr
from test_factor_evaluation import example
from test_historical_workflow import request

from finance_quant.application import historical_workflow, market_download
from finance_quant.research.evaluation import evaluate_factors
from finance_quant.research.registry import EvaluationConfig
from finance_quant.storage import ResultStore
from finance_quant.storage_tables import StreamingTables, factor_predictions, publish_tables
from support.fakes import make_service, run_claimed


def test_numeric_batch_input_and_streamed_horizons_keep_all_labels_and_diagnostics(tmp_path):
    market, factors, calendar = example()
    factors["metadata"].update(id="stream-input", weights={"momentum": 0.5, "value": 0.5})
    for i, row in enumerate(factors["history"]):
        row["raw"]["value"] = float((i // 12) % 3) if row["date"] != "2025-01-03" else 1.0
    market["is_suspended"] = market.date.eq("2025-01-07") & market.instrument.eq("SH600000")
    config = EvaluationConfig(horizons=(1, 3, 5))
    expected = evaluate_factors(market, factors, calendar, start="2025-01-03", end="2025-01-15", config=config)
    store = ResultStore(tmp_path)
    publish_tables(store, "factors", "stream-input", factors, ["history"])
    compact = store.get("factors", "stream-input", hydrate=False)
    numeric = factor_predictions(store, "stream-input", compact)
    assert "raw" not in numeric and not any(isinstance(v, dict) for v in numeric.to_numpy().ravel())
    with StreamingTables(store, "evaluations", "stream-output") as tables:
        actual = evaluate_factors(
            market,
            compact,
            calendar,
            start="2025-01-03",
            end="2025-01-15",
            config=config,
            raw_predictions=numeric,
            row_sink=tables.append,
        )
        assert all(not label["observations"] for label in actual["labels"].values())
        actual["tables"] = tables.finish()
    store.publish("evaluations", "stream-output", actual)
    saved = store.get("evaluations", "stream-output")
    saved.pop("tables")
    for label in saved["labels"].values():
        label.pop("observations_storage", None)
    assert saved == expected
    assert all(len(label["observations"]) == label["valid_observations"] for label in saved["labels"].values())
    # Independently verify Spearman with ties and constant-day exclusions.
    history = pd.DataFrame(factors["history"])
    history = history.loc[history.date.lt("2025-01-15")]
    for pair in actual["correlations"]["pairs"]:
        reference = {}
        for day, rows in history.groupby("date"):
            a = rows.raw.map(lambda r: r[pair["first"]])
            b = rows.raw.map(lambda r: r[pair["second"]])
            if len(rows) >= 3 and a.nunique() > 1 and b.nunique() > 1:
                reference[day] = float(spearmanr(a, b).statistic)
        assert {r["date"] for r in pair["daily"]} == set(reference)
        for row in pair["daily"]:
            assert row["rank_correlation"] == pytest.approx(reference[row["date"]], abs=1e-12)
    changed = market.copy()
    changed.loc[changed.date.gt("2025-01-15"), "open"] *= 100
    with StreamingTables(store, "evaluations", "future-output") as tables:
        future = evaluate_factors(
            changed,
            compact,
            calendar,
            start="2025-01-03",
            end="2025-01-15",
            config=config,
            raw_predictions=numeric,
            row_sink=tables.append,
        )
        future["tables"] = tables.finish()
    store.publish("evaluations", "future-output", future)
    hydrated = store.get("evaluations", "future-output")
    hydrated.pop("tables")
    for label in hydrated["labels"].values():
        label.pop("observations_storage", None)
    assert hydrated == expected


@pytest.mark.parametrize("exception", [MemoryError, KeyError, OSError])
def test_worker_persists_internal_error_stage_without_values_or_credentials(tmp_path, monkeypatch, exception):

    monkeypatch.setattr(market_download, "warehouse_roots", lambda data_root: [(tmp_path / "warehouse").resolve()])
    service = make_service(tmp_path / "runs", tmp_path / "data")
    store = service.store
    task = store.create(
        request(),
        {
            "mode": "index_warehouse",
            "warehouse_dir": str((tmp_path / "warehouse").resolve()),
            "manifest_sha256": "pinned",
        },
        "runtime-error-12345",
    )
    assert store.claim() == task["id"]

    def fail(*args, **kwargs):
        kwargs["stage"]("evaluation", "评估中", 3)
        if exception is OSError:
            raise OSError(28, "token=do-not-expose password=also-private")
        raise exception("token=do-not-expose password=also-private")

    monkeypatch.setattr(historical_workflow, "run_historical_research", fail)
    failed = run_claimed(service, task["id"])
    details = failed["error_details"]
    assert failed["status"] == "failed" and failed["progress"] == {"completed": 3, "total": 4}
    assert details["kind"] == "runtime" and details["stage"] == "evaluation"
    assert details["exception_type"] == exception.__name__ and details["trace"]
    assert "do-not-expose" not in json.dumps(failed) and "also-private" not in json.dumps(failed)
    if exception is MemoryError:
        assert "因子评估内存不足" in failed["error"]
    elif exception is KeyError:
        assert "KeyError" in failed["error"] and "凭据" not in failed["error"]
    else:
        assert "存储空间不足" in failed["error"]
