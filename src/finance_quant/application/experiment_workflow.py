"""Offline historical splits; freeze selection before executing the test account."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

from finance_quant.application.comparison_workflow import run_comparison
from finance_quant.application.evaluation_workflow import evaluate_configured
from finance_quant.application.experiment_inputs import dynamic_preflight
from finance_quant.application.experiment_inputs import preflight as _preflight
from finance_quant.application.factor_workflow import fingerprint, load_configured_snapshot, source_provenance
from finance_quant.application.historical_workflow import run_historical_research
from finance_quant.application.real_workflow import run_configured
from finance_quant.research.experiments import ExperimentPlan, experiment_markdown, select_candidate
from finance_quant.research.fundamentals import financial_provenance
from finance_quant.storage import ResultStore

PERIOD_LABELS = {"development": "开发", "validation": "验证", "test": "测试"}
LIMITATIONS = [
    "历史数据此前已用于研究；本次为回溯分段检验，不代表全新未见样本。改动方案或切分后重跑会增加选择偏差。",
    "当前使用手工设定的因子与权重，没有模型训练。开发区间仅供观察；只按验证区间收益差选方案，测试结果不参与选择。",
    "每个方案、每个区间从相同初始资金和空仓开始；区间之间不承接持仓，不拼接净值，期末按收盘估值且未强制清仓。",
    "收益差是累计净收益减基准累计收益，未做风险调整，不是 alpha；未做多重试验校正或显著性检验。",
    "因子只使用信号日及以前数据，可使用上一区间的历史预热；标签仅保留本区间内的入场和离场，尾部不足持有期的样本剔除。",
    "固定股票池由用户事后选择，不能代表全市场或历史指数成分，存在选股和幸存者偏差。",
    "Qlib 模拟含当前配置费用、固定比例冲击成本、整手约束和涨跌停限制；尚无成交量容量、排队成交或独立现金分红台账。",
    "策略按复权收益口径计算，基准是价格指数，分红口径不同；收益差不能直接解释为选股能力。",
    "BaoStock 回退仅覆盖行情与复权；估值仍来自 Tushare，未独立核验估值披露时点。",
]


def run_experiment(
    config,
    plan: ExperimentPlan,
    *,
    result_store=None,
    progress=None,
    dynamic_request=None,
    expected_sha=None,
    factor_selection=None,
    comparison_sample=None,
    comparison_source: tuple[str, str] | None = None,
) -> dict:
    """Run development/validation for every candidate, lock the choice, then test it once.

    ``progress(completed, total, message)`` is called after each backtest-and-evaluation pair.
    """
    # Round-trip even for in-process callers; dataclass construction cannot bypass validation.
    plan = ExperimentPlan.from_dict(plan.as_dict())
    if plan.comparison:
        return run_comparison(
            config,
            plan,
            result_store=result_store,
            progress=progress,
            dynamic_request=dynamic_request,
            expected_sha=expected_sha,
            prepared_sample=comparison_sample,
            execution_source=comparison_source,
        )
    candidate_coverage = None
    if dynamic_request is None:
        market, _, calendar, manifest = load_configured_snapshot(config)
        counts = _preflight(config, plan, market, calendar)
    else:
        manifest, counts, candidate_coverage = dynamic_preflight(
            config, plan, dynamic_request, expected_sha=expected_sha
        )
    # Pin the physical immutable directory once: another download may advance
    # the managed root's latest pointer while this multi-run experiment executes.
    frozen_config = replace(config, data=replace(config.data, root=Path(manifest["snapshot_dir"])))
    shared = {
        "backtest": {k: v for k, v in asdict(config.backtest).items() if k not in {"start", "end"}},
        "codes": sorted(config.data.codes),
        "benchmark": config.backtest.benchmark or config.data.benchmark,
        "evaluation": config.evaluation.as_dict(),
    }
    shared["neutralization"] = config.neutralization.as_dict()
    if dynamic_request is not None:
        shared["universe_label"] = manifest.get("universe_label", "沪深300历史成分（动态股票池）")
        shared["universe"] = dynamic_request["data"]
        shared["membership_policy"] = manifest["membership_policy"]
    provenance = {
        **source_provenance(),
        "data_version": manifest["id"],
        "data_checksums": manifest.get("files", {"manifest_sha256": manifest.get("manifest_sha256")}),
        "plan": plan.as_dict(),
        "shared_parameters": shared,
        **({"financial_version": config.financials.version} if config.financials else {}),
    }
    if factor_selection is not None:
        provenance["factor_selection"] = factor_selection
    for candidate in plan.candidates:
        provenance.setdefault("candidate_financials", {})[candidate.id] = financial_provenance(
            config.financials, candidate.factors
        )
    run_id = "split-" + fingerprint(provenance)
    store = result_store or ResultStore(config.runs_dir)
    total_runs = 2 * len(plan.candidates) + 1
    notify = progress or (lambda completed, total, message: None)
    locks = config.runs_dir / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(locks / (run_id + ".lock"), timeout=0):
            result_path = store.path("experiments", run_id)
            reused = store.exists("experiments", run_id)
            if reused:
                result = store.get("experiments", run_id)
                if result["metadata"]["provenance"] != provenance:
                    raise ValueError("同编号实验的来源记录不一致")
                for row in result["rows"]:
                    for category, field in (
                        ("backtests", "backtest_run_id"),
                        ("factors", "factor_run_id"),
                        ("evaluations", "evaluation_run_id"),
                    ):
                        if store.digest(category, row[field]) != row["artifact_sha256"][category]:
                            raise ValueError("实验引用的结果已被改动，拒绝复用")
            else:
                store.publish_experiment_record(run_id, "plan", provenance)
                rows = []

                def execute(candidate, period):
                    research = replace(
                        frozen_config,
                        factors=candidate.factors,
                        backtest=replace(config.backtest, start=period.start, end=period.end),
                    )
                    if dynamic_request is None:
                        backtest = run_configured(research, result_store=store)
                        diagnostic = evaluate_configured(research, result_store=store)
                    else:
                        document = {
                            **dynamic_request,
                            "factors": candidate.factors.as_list(),
                            "backtest": asdict(research.backtest),
                        }
                        completed = run_historical_research(
                            document,
                            frozen_config.data.root,
                            config.runs_dir,
                            expected_sha=expected_sha,
                            result_store=store,
                            financials=config.financials,
                            references_frozen=not config.neutralization.allow_latest,
                        )
                        backtest = {"run_id": completed["backtest_run_id"], "factor_run_id": completed["factor_run_id"]}
                        diagnostic = {
                            "run_id": completed["evaluation_run_id"],
                            "factor_run_id": completed["factor_run_id"],
                        }
                    saved = store.get("backtests", backtest["run_id"])
                    metrics = {
                        key: saved["summary"]["metrics"][key]
                        for key in (
                            "total_return",
                            "benchmark_return",
                            "max_drawdown",
                            "total_cost_amount",
                            "avg_turnover",
                        )
                    }
                    if any(
                        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                        for v in metrics.values()
                    ):
                        raise ValueError("实验指标须为有限数值")
                    metrics["excess_return"] = metrics["total_return"] - metrics["benchmark_return"]
                    if backtest["factor_run_id"] != diagnostic["factor_run_id"]:
                        raise ValueError("回测与评估使用的因子批次不一致")
                    row = {
                        "candidate_id": candidate.id,
                        "period": period.name,
                        "trading_days": counts[period.name],
                        "backtest_run_id": backtest["run_id"],
                        "factor_run_id": backtest["factor_run_id"],
                        "evaluation_run_id": diagnostic["run_id"],
                        "metrics": metrics,
                    }
                    if candidate_coverage is not None:
                        row["coverage"] = candidate_coverage[candidate.id][period.name]
                    row["artifact_sha256"] = {
                        category: store.digest(category, row[field])
                        for category, field in (
                            ("backtests", "backtest_run_id"),
                            ("factors", "factor_run_id"),
                            ("evaluations", "evaluation_run_id"),
                        )
                    }
                    rows.append(row)
                    notify(len(rows), total_runs, f"{candidate.name} · {PERIOD_LABELS[period.name]}区间完成")

                for period in plan.periods[:2]:
                    for candidate in plan.candidates:
                        execute(candidate, period)
                decision = select_candidate(rows, [c.id for c in plan.candidates])
                evidence = [row for row in rows if row["period"] == "validation"]
                selection_path = result_path.with_name("selection.json")
                if selection_path.exists():
                    record = json.loads(selection_path.read_text(encoding="utf-8"))
                    if record["decision"] != decision or record["validation_evidence"] != evidence:
                        raise ValueError("已锁定的方案选择与当前验证结果不一致")
                    selection = {**decision, "locked_at": record["locked_at"]}
                else:
                    selection = {**decision, "locked_at": datetime.now(timezone.utc).isoformat()}
                    store.publish_experiment_record(
                        run_id,
                        "selection",
                        {"decision": decision, "locked_at": selection["locked_at"], "validation_evidence": evidence},
                    )
                # No test call can occur before the immutable selection record exists.
                chosen = next(c for c in plan.candidates if c.id == selection["candidate_id"])
                execute(chosen, plan.periods[2])
                result = {
                    "metadata": {
                        "id": run_id,
                        "name": plan.name,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "data_version": manifest["id"],
                        "is_synthetic": False,
                        "selected_candidate": chosen.id,
                        "validation_kind": "retrospective_split",
                        **({"factor_selection": factor_selection} if factor_selection is not None else {}),
                        **(
                            {"point_in_time_certified": False, "membership_policy": manifest["membership_policy"]}
                            if dynamic_request is not None
                            else {}
                        ),
                        "provenance": provenance,
                    },
                    "plan": plan.as_dict(),
                    "shared_parameters": shared,
                    "selection": selection,
                    "rows": rows,
                    "limitations": [
                        "策略为复权等效持股，全收益基准计入分红再投资；仍未逐笔模拟现金红利与红利税。"
                        if config.backtest.benchmark == "H00300.CSI"
                        and text.startswith("策略按复权收益口径计算，基准是价格指数")
                        else "股票池逐日使用当时可见的成分与条件；预热和停牌会造成候选有效覆盖差异，见各段覆盖明细。"
                        if dynamic_request is not None and text.startswith("固定股票池由用户事后选择")
                        else "推荐组合按截止日前已实现标签的 Rank IC/IR 预筛并由 MILP 选取，使用等权；其他候选可手工配置。只按验证段选方案，测试结果不参与选择。"
                        if factor_selection is not None and text.startswith("当前使用手工设定")
                        else text
                        for text in LIMITATIONS
                    ],
                }
                if dynamic_request is not None:
                    result["limitations"].append(
                        "历史指数成分按月度快照的下一交易日生效，真实公告与调仓时点未认证。"
                        if dynamic_request["data"]["mode"] == "index_warehouse"
                        else manifest.get("st_policy", "历史上市和ST状态来自所选覆盖范围，供应商历史修订时点未认证。")
                    )
                store.publish("experiments", run_id, result)
            report = store.publish_report(run_id, experiment_markdown(result), category="experiments")
            return {
                "reused": reused,
                "run_id": run_id,
                "selected_candidate": result["selection"]["candidate_id"],
                "data_version": manifest["id"],
                "result": str(result_path),
                "report": str(report),
            }
    except Timeout:
        raise ValueError("该实验正在运行，请等待完成") from None
