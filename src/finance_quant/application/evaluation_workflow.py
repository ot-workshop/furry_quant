"""Explicit offline diagnostic runs, with immutable data/config/code provenance."""

from __future__ import annotations

from datetime import datetime, timezone

from filelock import FileLock, Timeout

from finance_quant.application.factor_workflow import (
    fingerprint,
    load_configured_snapshot,
    publish_factor_run,
    source_provenance,
)
from finance_quant.provenance import report_code_sha256
from finance_quant.research.evaluation import evaluate_factors
from finance_quant.research.fundamentals import financial_provenance
from finance_quant.research.report import evaluation_markdown
from finance_quant.storage import ResultStore


def evaluate_configured(config, *, result_store=None, frozen_snapshot=None, replay_recipe=None) -> dict:
    market, _, calendar, manifest = load_configured_snapshot(config, frozen_snapshot=frozen_snapshot)
    factors = publish_factor_run(
        market,
        manifest,
        config.runs_dir,
        config.factors,
        config.neutralization,
        result_store=result_store,
        financials=config.financials,
        frozen_snapshot=frozen_snapshot,
    )
    provenance = {
        **source_provenance(),
        "report_code_sha256": report_code_sha256(),
        "data_version": manifest["id"],
        "factor_run_id": factors["metadata"]["id"],
        "evaluation": config.evaluation.as_dict(),
        "start": config.backtest.start,
        "end": config.backtest.end,
        **financial_provenance(config.financials, config.factors),
    }
    if replay_recipe is not None:
        provenance["manual_formula_replay"] = replay_recipe
    run_id = "factor-eval-" + fingerprint(provenance)
    store = result_store or ResultStore(config.runs_dir)
    lock = config.runs_dir / ".locks" / (run_id + ".lock")
    try:
        with FileLock(lock, timeout=0):
            reused = store.exists("evaluations", run_id)
            if reused:
                result = store.get("evaluations", run_id)
                if result["metadata"]["provenance"] != provenance:
                    raise ValueError("Evaluation artifact provenance conflict")
            else:
                result = evaluate_factors(
                    market,
                    factors,
                    calendar.loc[calendar["is_open"], "date"],
                    start=config.backtest.start,
                    end=config.backtest.end,
                    config=config.evaluation,
                )
                result["metadata"] = {
                    "id": run_id,
                    "name": "真实行情因子评估",
                    "is_synthetic": False,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "data_version": manifest["id"],
                    "factor_run_id": factors["metadata"]["id"],
                    "factor_config": config.factors.as_list(),
                    "evaluation": config.evaluation.as_dict(),
                    "start": config.backtest.start,
                    "end": config.backtest.end,
                    "sample_size": int(market["instrument"].nunique()),
                    "provenance": provenance,
                }
                result["metadata"]["factor_definitions"] = factors["metadata"].get("factor_definitions", {})
                for key in ["neutralization", "industry_data", "temporal_audit", "financial_data"]:
                    if key in factors["metadata"]:
                        result["metadata"][key] = factors["metadata"][key]
                result["metadata"]["source"] = manifest.get("source", "third_party_tushare_compatible")
                if manifest.get("cross_validation"):
                    result["metadata"]["cross_validation"] = manifest["cross_validation"]
                store.publish("evaluations", run_id, result)
            report = store.publish_report(run_id, evaluation_markdown(result))
            return {
                "reused": reused,
                "run_id": run_id,
                "factor_run_id": factors["metadata"]["id"],
                "data_version": manifest["id"],
                "result": str(store.path("evaluations", run_id)),
                "report": str(report),
            }
    except Timeout:
        raise ValueError("This factor evaluation is already running") from None
