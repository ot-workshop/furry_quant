"""Web-submitted historical split experiments: strict request validation and worker execution."""

from __future__ import annotations

import json
from pathlib import Path

from finance_quant.application.experiment_inputs import dynamic_preflight
from finance_quant.application.experiment_workflow import _preflight, run_experiment
from finance_quant.application.research_requests import (
    MAX_CODES,
    _fields,
    pin_financials_for,
    request_config,
    snapshot_registry,
    validate_request,
)
from finance_quant.application.selection_experiments import (
    selection_validation,
    validate_selection_inputs,
    verify_queued_selection,
)
from finance_quant.application.task_context import attach_outputs, prepare_snapshot, prepare_warehouse
from finance_quant.configuration import BacktestConfig, DataConfig
from finance_quant.data.symbols import to_qlib
from finance_quant.data.validation_config import CrosscheckConfig
from finance_quant.research.experiments import SELECTION_METRIC, ExperimentPlan

SHARED_BACKTEST_FIELDS = tuple(name for name in BacktestConfig.__dataclass_fields__ if name not in {"start", "end"})


def _plan(document: dict) -> ExperimentPlan:
    plan = document["plan"]
    _fields(plan, ("periods", "candidates"), label="实验计划")
    return ExperimentPlan.from_dict(
        {
            "name": document["name"],
            "selection_metric": SELECTION_METRIC,
            "periods": plan["periods"],
            "candidates": plan["candidates"],
        }
    )


def _research_document(document: dict, plan: ExperimentPlan) -> dict:
    """The equivalent single research request: whole span, first candidate's factors."""
    research = {
        "name": document["name"],
        "data": document["data"],
        "backtest": {**document["backtest"], "start": plan.periods[0].start, "end": plan.periods[-1].end},
        "factors": plan.candidates[0].factors.as_list(),
        "evaluation": document["evaluation"],
    }
    if "neutralization" in document:
        research["neutralization"] = document["neutralization"]
    return research


def _pin_candidates(research: dict, execution: dict, plan: ExperimentPlan, data_root) -> None:
    """Every candidate shares one financial version, including when only a later one needs it."""
    if "financials" in research:
        return
    binding = pin_financials_for([candidate.factors for candidate in plan.candidates], data_root)
    if binding is not None:
        research["financials"] = binding.as_dict()
        execution["financials"] = binding.as_dict()


def validate_experiment_request(document: dict, data_root, runs_root, *, result_store=None) -> tuple[dict, dict]:
    """Validate everything the worker will need before anything is queued or written."""

    _fields(
        document,
        ("name", "data", "backtest", "plan", "evaluation"),
        ("neutralization", "financial_policy", "factor_selection"),
        label="实验",
    )
    _fields(document["backtest"], (), SHARED_BACKTEST_FIELDS, label="共用回测参数")
    plan = _plan(document)
    bound = selection_validation(result_store, document, plan, data_root, runs_root)
    research, execution = validate_request(
        _research_document(document, plan),
        data_root,
        runs_root,
        financials=bound["context"]["financials"] if bound else None,
    )
    validate_selection_inputs(bound, research)
    _pin_candidates(research, execution, plan, data_root)
    if research["data"]["mode"] != "snapshot" or "filters" in research["data"]:
        root = Path(execution.get("warehouse_dir", execution.get("snapshot_dir")))
        config = request_config(research, dynamic_data(root, research), runs_root, data_root)
        dynamic_preflight(config, plan, research, expected_sha=execution.get("manifest_sha256"))
    else:
        market, _, calendar, manifest = snapshot_registry(data_root)[research["data"]["snapshot_id"]]
        config = request_config(research, _data_config(manifest, research["data"]["codes"]), runs_root, data_root)
        if len(config.data.codes) > MAX_CODES:
            raise ValueError(f"单次实验最多选择{MAX_CODES}只股票")
        selected = market.loc[market.instrument.isin({to_qlib(code) for code in config.data.codes})]
        _preflight(config, plan, selected, calendar)
    normalized = {
        "name": research["name"],
        "data": research["data"],
        "backtest": {k: v for k, v in research["backtest"].items() if k not in {"start", "end"}},
        "plan": {key: value for key, value in plan.as_dict().items() if key in {"periods", "candidates"}},
        "evaluation": research["evaluation"],
        **({"neutralization": research["neutralization"]} if "neutralization" in research else {}),
        **({"financials": research["financials"]} if "financials" in research else {}),
    }
    if bound is not None:
        normalized["factor_selection"] = bound["selection"]["reference"]
        execution["factor_selection"] = bound["selection"]
    return normalized, execution


def _data_config(manifest: dict, codes) -> DataConfig:

    return DataConfig(
        Path(manifest["snapshot_dir"]),
        manifest["start"],
        manifest["end"],
        codes,
        manifest.get("benchmark", "000300.SH"),
        CrosscheckConfig(manifest.get("cross_validation", {}).get("mode", "off")),
    )


def dynamic_data(root, request):
    """Describe the already-validated source without projecting or publishing data."""
    manifest = json.loads((Path(root) / "manifest.json").read_bytes())
    codes = request["data"].get("codes") or sorted(manifest.get("assets", manifest.get("codes", [])))
    return DataConfig(Path(root), manifest.get("data_start", manifest["start"]), manifest["end"], codes)


def experiment_total(document: dict) -> int:
    """Progress units: preparation plus one per backtest-and-evaluation pair."""
    return 1 + 2 * len(document["plan"]["candidates"]) + 1


def run_experiment_task(context, request: dict, execution: dict) -> dict:

    plan = _plan(request)
    evidence = execution.get("factor_selection")
    verify_queued_selection(context.result_store_factory(None), evidence)
    context.stage("prepare", "准备股票池并检查各候选方案的历史预热", 0)
    dynamic = request["data"]["mode"] != "snapshot" or "filters" in request["data"]
    if execution["mode"] != "snapshot":
        root, dataset_record = prepare_warehouse(context, request, execution)
        data = dynamic_data(root, request)
    else:
        _, _, _, data, dataset_record = prepare_snapshot(context, request, execution)
    document = {
        **_research_document(request, plan),
        **({"financials": request["financials"]} if "financials" in request else {}),
    }
    config = request_config(document, data, context.runs_root, context.data_root)
    store = context.result_store_factory(dataset_record["id"] if dataset_record else None)

    def progress(completed, total, message):
        context.stage("experiment", message, 1 + completed)

    context.stage("experiment", "运行开发与验证区间", 1)
    completed = run_experiment(
        config,
        plan,
        result_store=store,
        progress=progress,
        dynamic_request=document if dynamic else None,
        expected_sha=execution.get("manifest_sha256") if execution["mode"] != "snapshot" else None,
        factor_selection=evidence,
    )
    result = {
        "experiment_run_id": completed["run_id"],
        "selected_candidate": completed["selected_candidate"],
        "data_version": completed["data_version"],
        "reused": completed["reused"],
    }
    if dataset_record:
        experiment = store.get("experiments", completed["run_id"], hydrate=False)
        for row in experiment["rows"]:
            attach_outputs(store, dataset_record["id"], row)
        attach = getattr(store, "attach_dataset", None)
        if attach is not None:
            attach("experiments", completed["run_id"], dataset_record["id"])
        result["dataset_id"] = dataset_record["id"]
    return result
