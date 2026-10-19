"""Offline historical splits; freeze selection before executing the test account."""

from __future__ import annotations

import hashlib
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
from finance_quant.research.market_factors import market_factors
from finance_quant.storage import ResultStore

LIMITATIONS = [
    "历史数据此前已用于研究；本次为回溯分段检验，不代表全新未见样本。改动方案或切分后重跑会增加选择偏差。",
    "当前使用手工设定的因子与权重，没有模型训练。开发区间仅供观察；只按验证区间收益差选方案，测试结果不参与选择。",
    "每个方案、每个区间从相同初始资金和空仓开始；区间之间不承接持仓，不拼接净值，期末按收盘估值且未强制清仓。",
    "收益差是累计净收益减基准累计收益，未做风险调整，不是 alpha；未做多重试验校正或显著性检验。",
    "因子只使用信号日及以前数据，可使用上一区间的历史预热；标签仅保留本区间内的入场和离场，尾部不足持有期的样本剔除。",
    "固定的小股票池不能代表全市场；尚未接入历史成分、退市样本、财报修订与完整的停复牌建模，存在选股和幸存者偏差。",
    "Qlib 模拟含当前配置费用、整手约束和涨跌停限制；尚无滑点、成交量容量、排队成交或独立现金分红台账。",
    "策略按复权收益口径计算，基准是价格指数，分红口径不同；收益差不能直接解释为选股能力。",
    "BaoStock 回退仅覆盖行情与复权；估值仍来自第三方 Tushare 兼容服务，未独立核验估值披露时点。",
]


def _preflight(config, plan: ExperimentPlan, market, calendar) -> dict[str, int]:
    sessions = pd.DatetimeIndex(calendar.loc[calendar["is_open"], "date"])
    counts = {}
    previous_index = None
    all_decisions = set()
    for period in plan.periods:
        if period.start < config.backtest.start or period.end > config.backtest.end:
            raise ValueError("Experiment periods must be inside the configured backtest range")
        start, end = pd.Timestamp(period.start), pd.Timestamp(period.end)
        if start not in sessions or end not in sessions:
            raise ValueError("Experiment boundaries must be actual trading sessions")
        left, right = sessions.get_loc(start), sessions.get_loc(end)
        if left == 0:
            raise ValueError("Experiment requires a prior session for the first decision")
        if previous_index is not None and left != previous_index + 1:
            raise ValueError("Experiment periods must cover consecutive trading sessions without gaps")
        previous_index = right
        counts[period.name] = right - left + 1
        if counts[period.name] <= max(config.evaluation.horizons):
            raise ValueError("Every period needs more sessions than the longest evaluation horizon")
        all_decisions.update(sessions[left - 1:right].strftime("%Y-%m-%d"))
    # Fair comparison: every scheme must score the entire declared universe on
    # every decision day. Reject silent filtering or insufficient warm-up early.
    universe = set(market["instrument"].unique())
    for candidate in plan.candidates:
        try:
            factors = market_factors(market, "preflight", candidate.factors)
        except ValueError as exc:
            raise ValueError(f"Candidate {candidate.id} lacks scores: {exc}") from None
        coverage = {}
        for row in factors["history"]:
            if row["date"] in all_decisions:
                coverage.setdefault(row["date"], set()).add(row["instrument"])
        if any(coverage.get(day) != universe for day in all_decisions):
            raise ValueError(f"Candidate {candidate.id} lacks scores for the common universe; check warm-up and missing values")
    return counts


def _artifact_hash(store, category, identifier):
    return hashlib.sha256(store.path(category, identifier).read_bytes()).hexdigest()


def run_experiment(config, plan: ExperimentPlan) -> dict:
    # Round-trip even for in-process callers; dataclass construction cannot bypass validation.
    plan = ExperimentPlan.from_dict(plan.as_dict())
    market, _, calendar, manifest = load_configured_snapshot(config)
    counts = _preflight(config, plan, market, calendar)
    # Pin the physical immutable directory once: another download may advance
    # the managed root's latest pointer while this multi-run experiment executes.
    frozen_config = replace(config, data=replace(config.data, root=Path(manifest["snapshot_dir"])))
    base = Path(__file__).parents[1]
    source_files = [Path(__file__), base / "research/experiments.py", base / "configuration.py", base / "storage.py",
                    base / "application/real_workflow.py", base / "application/factor_workflow.py",
                    base / "application/evaluation_workflow.py", base / "integrations/qlib_runner.py",
                    base / "research/market_factors.py", base / "research/registry.py",
                    base / "research/evaluation.py", base / "research/report.py",
                    base / "data/market.py", base / "data/quality.py"]
    shared = {"backtest": {k: v for k, v in asdict(config.backtest).items() if k not in {"start", "end"}},
              "codes": sorted(config.data.codes), "benchmark": config.data.benchmark, "evaluation": config.evaluation.as_dict()}
    provenance = {**source_provenance(source_files), "data_version": manifest["id"],
                  "data_checksums": manifest["files"], "plan": plan.as_dict(), "shared_parameters": shared}
    run_id = "split-" + fingerprint(provenance)
    store = ResultStore(config.runs_dir)
    locks = config.runs_dir / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(locks / (run_id + ".lock"), timeout=0):
            result_path = store.path("experiments", run_id)
            reused = result_path.exists()
            if reused:
                result = store.get("experiments", run_id)
                if result["metadata"]["provenance"] != provenance:
                    raise ValueError("Experiment provenance conflict")
                for row in result["rows"]:
                    for category, field in (("backtests", "backtest_run_id"), ("factors", "factor_run_id"), ("evaluations", "evaluation_run_id")):
                        if _artifact_hash(store, category, row[field]) != row["artifact_sha256"][category]:
                            raise ValueError("Referenced experiment artifact has changed")
            else:
                store.publish_experiment_record(run_id, "plan", provenance)
                rows = []

                def execute(candidate, period):
                    research = replace(frozen_config, factors=candidate.factors,
                                       backtest=replace(config.backtest, start=period.start, end=period.end))
                    backtest = run_configured(research)
                    diagnostic = evaluate_configured(research)
                    saved = store.get("backtests", backtest["run_id"])
                    metrics = {key: saved["summary"]["metrics"][key] for key in (
                        "total_return", "benchmark_return", "max_drawdown", "total_cost_amount", "avg_turnover")}
                    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in metrics.values()):
                        raise ValueError("Experiment metrics must be finite")
                    metrics["excess_return"] = metrics["total_return"] - metrics["benchmark_return"]
                    if backtest["factor_run_id"] != diagnostic["factor_run_id"]:
                        raise ValueError("Backtest and evaluation factor artifacts differ")
                    row = {"candidate_id": candidate.id, "period": period.name, "trading_days": counts[period.name],
                           "backtest_run_id": backtest["run_id"], "factor_run_id": backtest["factor_run_id"],
                           "evaluation_run_id": diagnostic["run_id"], "metrics": metrics}
                    row["artifact_sha256"] = {category: _artifact_hash(store, category, row[field]) for category, field in (
                        ("backtests", "backtest_run_id"), ("factors", "factor_run_id"), ("evaluations", "evaluation_run_id"))}
                    rows.append(row)

                for period in plan.periods[:2]:
                    for candidate in plan.candidates:
                        execute(candidate, period)
                decision = select_candidate(rows, [c.id for c in plan.candidates])
                evidence = [row for row in rows if row["period"] == "validation"]
                selection_path = result_path.with_name("selection.json")
                if selection_path.exists():
                    record = json.loads(selection_path.read_text(encoding="utf-8"))
                    if record["decision"] != decision or record["validation_evidence"] != evidence:
                        raise ValueError("Locked selection differs from current validation evidence")
                    selection = {**decision, "locked_at": record["locked_at"]}
                else:
                    selection = {**decision, "locked_at": datetime.now(timezone.utc).isoformat()}
                    store.publish_experiment_record(run_id, "selection", {
                        "decision": decision, "locked_at": selection["locked_at"], "validation_evidence": evidence})
                # No test call can occur before the immutable selection record exists.
                chosen = next(c for c in plan.candidates if c.id == selection["candidate_id"])
                execute(chosen, plan.periods[2])
                result = {"metadata": {"id": run_id, "name": plan.name, "created_at": datetime.now(timezone.utc).isoformat(),
                                       "data_version": manifest["id"], "is_synthetic": False,
                                       "selected_candidate": chosen.id, "validation_kind": "retrospective_split",
                                       "provenance": provenance},
                          "plan": plan.as_dict(), "shared_parameters": shared, "selection": selection,
                          "rows": rows, "limitations": LIMITATIONS}
                store.publish("experiments", run_id, result)
            report = store.publish_report(run_id, experiment_markdown(result), category="experiments")
            return {"reused": reused, "run_id": run_id, "selected_candidate": result["selection"]["candidate_id"],
                    "data_version": manifest["id"], "result": str(result_path), "report": str(report)}
    except Timeout:
        raise ValueError("This experiment is already running") from None
