"""Web-submitted historical split experiments: strict request validation and worker execution."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from finance_quant.application.comparison_inputs import validate_comparison_inputs
from finance_quant.application.experiment_inputs import dynamic_preflight
from finance_quant.application.experiment_workflow import _preflight, run_experiment
from finance_quant.application.research_requests import (
    MAX_CODES,
    _fields,
    data_version_ids,
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
from finance_quant.data.market import FrozenMarketSnapshot, load_frozen_snapshot
from finance_quant.data.symbols import to_qlib
from finance_quant.data.validation_config import CrosscheckConfig
from finance_quant.research.experiments import SELECTION_METRIC, ExperimentPlan
from finance_quant.research.factor_selection import SelectionParameters
from finance_quant.research.neutralization import industry_context, parse_neutralization

SHARED_BACKTEST_FIELDS = tuple(name for name in BacktestConfig.__dataclass_fields__ if name not in {"start", "end"})


def _plan(document: dict) -> ExperimentPlan:
    plan = document["plan"]
    _fields(plan, ("periods", "candidates"), ("comparison",), label="实验计划")
    return ExperimentPlan.from_dict(
        {
            "name": document["name"],
            "selection_metric": "none" if "comparison" in plan else SELECTION_METRIC,
            "periods": plan["periods"],
            "candidates": plan["candidates"],
            **({"comparison": plan["comparison"]} if "comparison" in plan else {}),
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


def validate_experiment_document(document: dict):
    """Cheap, request-path checks only: shape, plan, and the comparison/factor rule.

    Everything expensive (pinning, selection inputs, preflight, industry context) stays in
    `validate_experiment_request`, which the worker runs in its `prepare` stage before any
    artefact is written.
    """

    _fields(
        document,
        ("name", "data", "backtest", "plan", "evaluation"),
        ("neutralization", "financial_policy", "factor_selection"),
        label="实验",
    )
    _fields(document["backtest"], (), SHARED_BACKTEST_FIELDS, label="共用回测参数")
    plan = _plan(document)
    if plan.comparison and document.get("factor_selection"):
        raise ValueError("收益来源对照不参与MILP样本外方案选择，请使用分段比较")
    return plan


SELECTION_ID = re.compile(r"factor-selection-[0-9a-f]{12}")


def validate_selection_reference(document: dict, plan: ExperimentPlan, data_root) -> None:
    """Checks a selection handoff settles from the request alone.

    Only rules that need neither the trading calendar nor the source evaluation are decided
    here, so a wrong reference or a pre-cutoff period still fails with its own reason instead
    of reaching the worker. Window, weight, neutralization and factor edits stay in
    ``selection_validation``, which the worker runs once it has the source result.
    """

    reference = document.get("factor_selection")
    if reference is None:
        return
    _fields(reference, ("evaluation_id", "parameters", "selection_id"), (), label="因子筛选引用")
    identifier = reference["selection_id"]
    if not isinstance(identifier, str) or SELECTION_ID.fullmatch(identifier) is None:
        raise ValueError("因子筛选结果标识格式无效，请回到相关性页面重新筛选")
    parameters = reference["parameters"]
    if not isinstance(parameters, dict):
        raise ValueError("筛选参数须为对象")
    try:
        # The same parameter object the worker builds, which validates the cutoff format without
        # reading any file; a hand-rolled parse here would drift from it.
        selection_parameters = SelectionParameters(**parameters)
    except TypeError:
        raise ValueError("因子筛选引用字段与筛选参数不匹配，请回到相关性页面重新筛选") from None
    cutoff = date.fromisoformat(selection_parameters.cutoff)
    if any(date.fromisoformat(period.start) <= cutoff for period in plan.periods):
        raise ValueError("因子筛选后的开发、验证、测试区间必须全部晚于推荐截止日")
    if not any(candidate.id == "milp" for candidate in plan.candidates):
        raise ValueError("比较计划缺少来源筛选的 MILP 候选方案")
    data = document["data"]
    mode = data.get("mode") if isinstance(data, dict) else None
    if mode in {"index_warehouse", "market_warehouse"}:
        if data.get("warehouse_id") not in data_version_ids(data_root, mode):
            raise ValueError("沪深300历史成分版本不存在或已变化，请刷新数据列表")


def validate_experiment_inputs(
    document: dict, data_root, runs_root, *, result_store=None, frozen_snapshot: FrozenMarketSnapshot | None = None
) -> tuple[dict, dict]:
    """Validate the experiment without publishing any research artefacts."""

    plan = validate_experiment_document(document)
    bound = selection_validation(result_store, document, plan, data_root, runs_root, frozen_snapshot=frozen_snapshot)
    research, execution = validate_request(
        _research_document(document, plan),
        data_root,
        runs_root,
        financials=bound["context"]["financials"] if bound else None,
        frozen_snapshot=frozen_snapshot,
    )
    validate_selection_inputs(bound, research)
    _pin_candidates(research, execution, plan, data_root)

    def snapshot_inputs():
        if frozen_snapshot is not None:
            return frozen_snapshot.inputs
        return snapshot_registry(data_root)[research["data"]["snapshot_id"]]

    if plan.comparison:
        base = parse_neutralization(research.get("neutralization"))
        industry = industry_context(base)
        for candidate in plan.candidates:
            neutral = candidate.neutralization or base
            if neutral.industry_version and (industry is None or neutral.industry_version != industry.manifest["id"]):
                raise ValueError("各方案必须共用同一历史行业版本")
            if neutral.mode == "industry":
                if industry is None:
                    raise ValueError("行业中性化需要先下载历史行业数据")
                industry.validate_level(neutral.industry_level)
                industry.validate_range(research["backtest"]["end"])
        if industry:
            research["neutralization"] = {**base.as_dict(), "industry_version": industry.manifest["id"]}
        root = Path(execution.get("warehouse_dir", execution.get("snapshot_dir")))
        data_config = (
            dynamic_data(root, research)
            if research["data"]["mode"] != "snapshot"
            else _data_config(snapshot_inputs()[3], research["data"]["codes"])
        )
        config = request_config(research, data_config, runs_root, data_root)
        validate_comparison_inputs(
            config, plan, research, expected_sha=execution.get("manifest_sha256"), frozen_snapshot=frozen_snapshot
        )
    elif research["data"]["mode"] != "snapshot" or "filters" in research["data"]:
        root = Path(execution.get("warehouse_dir", execution.get("snapshot_dir")))
        data_config = (
            _data_config(frozen_snapshot.inputs[3], research["data"]["codes"])
            if frozen_snapshot is not None
            else dynamic_data(root, research)
        )
        config = request_config(research, data_config, runs_root, data_root)
        dynamic_preflight(
            config, plan, research, expected_sha=execution.get("manifest_sha256"), frozen_snapshot=frozen_snapshot
        )
    else:
        market, _, calendar, manifest = snapshot_inputs()
        config = request_config(research, _data_config(manifest, research["data"]["codes"]), runs_root, data_root)
        if len(config.data.codes) > MAX_CODES:
            raise ValueError(f"单次实验最多选择{MAX_CODES}只股票")
        selected = market.loc[market.instrument.isin({to_qlib(code) for code in config.data.codes})]
        _preflight(config, plan, selected, calendar)
    normalized = {
        "name": research["name"],
        "data": research["data"],
        "backtest": {k: v for k, v in research["backtest"].items() if k not in {"start", "end"}},
        "plan": {key: value for key, value in plan.as_dict().items() if key in {"periods", "candidates", "comparison"}},
        "evaluation": research["evaluation"],
        **({"neutralization": research["neutralization"]} if "neutralization" in research else {}),
        **({"financials": research["financials"]} if "financials" in research else {}),
    }
    if bound is not None:
        normalized["factor_selection"] = bound["selection"]["reference"]
        execution["factor_selection"] = bound["selection"]
    return normalized, execution


def validate_experiment_request(document: dict, data_root, runs_root, *, result_store=None) -> tuple[dict, dict]:
    """Cheap checks plus the heavy ones, for callers that must validate before queueing.

    `submit_experiment` no longer uses this: it queues after `validate_experiment_document`
    and the worker runs `validate_experiment_inputs` at its `prepare` stage.
    """

    validate_experiment_document(document)
    return validate_experiment_inputs(document, data_root, runs_root, result_store=result_store)


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
    comparison = document["plan"].get("comparison")
    if comparison:
        return 1 + len(document["plan"]["candidates"]) * (2 if comparison["sample_mode"] == "common" else 1)
    return 1 + 2 * len(document["plan"]["candidates"]) + 1


def run_experiment_task(context, request: dict, execution: dict) -> dict:
    pending = execution.get("prepare_state") == "pending"
    if pending:
        # Legacy fully prepared tasks skip this branch. New fixed-pool tasks must
        # provide a verified leaf; a missing pin must not fall back to the registry.
        context.stage("prepare", "准备股票池并检查各候选方案的历史预热", 0)

        data = request.get("data")
        frozen_snapshot = (
            load_frozen_snapshot(context.data_root, execution)
            if isinstance(data, dict) and data.get("mode") == "snapshot"
            else None
        )
        request, execution = validate_experiment_inputs(
            request,
            context.data_root,
            context.runs_root,
            result_store=context.result_store_factory(None),
            frozen_snapshot=frozen_snapshot,
        )
        # Do not retain the preflight frames while the experiment runs.
        del frozen_snapshot

    if execution.get("prepare_state") == "prepared":
        # A prepared payload belongs to this worker's own single write; seeing it again means
        # the task is being re-run after preparation, which must not project or register again.
        raise ValueError("准备结果已记录，任务不应重复执行准备阶段")

    plan = _plan(request)
    evidence = execution.get("factor_selection")
    verify_queued_selection(context.result_store_factory(None), evidence)
    if pending:
        # A is verified and the queued selection checked: record the result once, and only
        # for this still-pending running task. A second write is a conflict, so projection
        # and registration can never run twice for one task.
        saved = {**execution, "prepare_state": "prepared"}
        context.store.save_prepared(context.identifier, saved)
        execution = saved
    if not pending:
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

    context.stage("experiment", "运行各方案及共同样本收益对照" if plan.comparison else "运行开发与验证区间", 1)
    completed = run_experiment(
        config,
        plan,
        result_store=store,
        progress=progress,
        dynamic_request=document if dynamic or plan.comparison else None,
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
