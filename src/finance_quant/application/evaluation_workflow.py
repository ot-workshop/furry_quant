"""Explicit offline diagnostic runs, with immutable data/config/code provenance."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

from finance_quant.application.factor_workflow import fingerprint, load_configured_snapshot, publish_factor_run, source_provenance
from finance_quant.research.evaluation import evaluate_factors
from finance_quant.research.report import evaluation_markdown
from finance_quant.storage import ResultStore


def evaluate_configured(config) -> dict:
    market, _, calendar, manifest = load_configured_snapshot(config)
    factors = publish_factor_run(market, manifest, config.runs_dir, config.factors)
    base = Path(__file__).parents[1]
    provenance = {**source_provenance([Path(__file__), base / "research/evaluation.py", base / "research/report.py"]),
                  "data_version": manifest["id"], "factor_run_id": factors["metadata"]["id"],
                  "evaluation": config.evaluation.as_dict(), "start": config.backtest.start, "end": config.backtest.end}
    run_id = "factor-eval-" + fingerprint(provenance)
    store = ResultStore(config.runs_dir)
    lock = config.runs_dir / ".locks" / (run_id + ".lock")
    try:
        with FileLock(lock, timeout=0):
            reused = store.path("evaluations", run_id).exists()
            if reused:
                result = store.get("evaluations", run_id)
                if result["metadata"]["provenance"] != provenance:
                    raise ValueError("Evaluation artifact provenance conflict")
            else:
                result = evaluate_factors(market, factors, calendar.loc[calendar["is_open"], "date"],
                                          start=config.backtest.start, end=config.backtest.end, config=config.evaluation)
                result["metadata"] = {"id": run_id, "name": "真实行情因子评估", "is_synthetic": False,
                                      "created_at": datetime.now(timezone.utc).isoformat(), "data_version": manifest["id"],
                                      "factor_run_id": factors["metadata"]["id"], "factor_config": config.factors.as_list(),
                                      "evaluation": config.evaluation.as_dict(), "start": config.backtest.start,
                                      "end": config.backtest.end, "sample_size": int(market["instrument"].nunique()),
                                      "provenance": provenance}
                store.publish("evaluations", run_id, result)
            report = store.publish_report(run_id, evaluation_markdown(result))
            return {"reused": reused, "run_id": run_id, "factor_run_id": factors["metadata"]["id"],
                    "data_version": manifest["id"], "result": str(store.path("evaluations", run_id)), "report": str(report)}
    except Timeout:
        raise ValueError("This factor evaluation is already running") from None
