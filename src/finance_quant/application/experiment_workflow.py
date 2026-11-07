"""Offline historical splits; freeze selection before executing the test account."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from filelock import FileLock, Timeout

from finance_quant.application.evaluation_workflow import evaluate_configured
from finance_quant.application.factor_workflow import fingerprint, load_configured_snapshot, source_provenance
from finance_quant.application.real_workflow import run_configured
from finance_quant.research.experiments import ExperimentPlan, experiment_markdown, select_candidate
from finance_quant.research.fundamentals import financial_provenance, fundamentals_for
from finance_quant.research.market_factors import market_factors
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


def _preflight(config, plan: ExperimentPlan, market, calendar) -> dict[str, int]:
    sessions = pd.DatetimeIndex(calendar.loc[calendar["is_open"], "date"])
    counts = {}
    previous_index = None
    all_decisions = set()
    for period in plan.periods:
        if period.start < config.backtest.start or period.end > config.backtest.end:
            raise ValueError("实验区间须位于回测区间之内")
        start, end = pd.Timestamp(period.start), pd.Timestamp(period.end)
        if start not in sessions or end not in sessions:
            raise ValueError("实验区间起止日期须为实际交易日")
        left, right = sessions.get_loc(start), sessions.get_loc(end)
        if left == 0:
            raise ValueError("开发区间开始前至少需要一个交易日用于生成首个信号")
        if previous_index is not None and left != previous_index + 1:
            raise ValueError("开发、验证、测试区间须首尾相接，中间不能留出交易日")
        previous_index = right
        counts[period.name] = right - left + 1
        if counts[period.name] <= max(config.evaluation.horizons):
            raise ValueError("每个区间的交易日数须超过最长评估持有期")
        all_decisions.update(sessions[left - 1 : right].strftime("%Y-%m-%d"))
    # Fair comparison: every scheme must score the entire declared universe on
    # every decision day. Reject silent filtering or insufficient warm-up early.
    universe = set(market["instrument"].unique())
    for candidate in plan.candidates:
        try:
            factors = market_factors(
                market,
                "preflight",
                candidate.factors,
                financials=fundamentals_for(config.financials, candidate.factors, market),
            )
        except ValueError as exc:
            raise ValueError(f"候选方案 {candidate.id} 无法计算评分：{exc}") from None
        coverage = {}
        for row in factors["history"]:
            if row["date"] in all_decisions:
                coverage.setdefault(row["date"], set()).add(row["instrument"])
        if any(coverage.get(day) != universe for day in all_decisions):
            raise ValueError(f"候选方案 {candidate.id} 在部分决策日缺少共同股票池评分；请检查历史预热与缺失值")
    return counts


def run_experiment(config, plan: ExperimentPlan, *, result_store=None, progress=None) -> dict:
    """Run development/validation for every candidate, lock the choice, then test it once.

    ``progress(completed, total, message)`` is called after each backtest-and-evaluation pair.
    """
    # Round-trip even for in-process callers; dataclass construction cannot bypass validation.
    plan = ExperimentPlan.from_dict(plan.as_dict())
    market, _, calendar, manifest = load_configured_snapshot(config)
    counts = _preflight(config, plan, market, calendar)
    # Pin the physical immutable directory once: another download may advance
    # the managed root's latest pointer while this multi-run experiment executes.
    frozen_config = replace(config, data=replace(config.data, root=Path(manifest["snapshot_dir"])))
    shared = {
        "backtest": {k: v for k, v in asdict(config.backtest).items() if k not in {"start", "end"}},
        "codes": sorted(config.data.codes),
        "benchmark": config.backtest.benchmark or config.data.benchmark,
        "evaluation": config.evaluation.as_dict(),
    }
    provenance = {
        **source_provenance(),
        "data_version": manifest["id"],
        "data_checksums": manifest["files"],
        "plan": plan.as_dict(),
        "shared_parameters": shared,
        **({"financial_version": config.financials.version} if config.financials else {}),
    }
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
                    backtest = run_configured(research, result_store=store)
                    diagnostic = evaluate_configured(research, result_store=store)
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
                        else text
                        for text in LIMITATIONS
                    ],
                }
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
