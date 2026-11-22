"""Run paired independent/common-sample return controls without choosing a winner."""

from dataclasses import asdict
from datetime import datetime, timezone

from filelock import FileLock, Timeout

from finance_quant.application.comparison_inputs import candidate_config, prepare_comparison
from finance_quant.application.historical_workflow import run_historical_research
from finance_quant.provenance import fingerprint, source_provenance
from finance_quant.research.comparison import comparison_markdown, return_differences
from finance_quant.storage import ResultStore

LIMITATIONS = [
    "这是同区间历史对照，不是样本外选优，也不是因果或行业收益归因模型。",
    "共同样本按每个信号日的已知股票池、历史预热、财报与行业有效性求交集，先限制样本再重新排名；不取全区间交集，不读取未来收益。",
    "共同样本只限制下一交易日的选股评分，不额外强制退出持仓；退出沿用原股票池、调仓和成交规则，实际持仓不保证相同。",
    "样本变化差额 = 独立样本收益差 − 共同样本收益差；它是与基准方案有关的对照差额，包含样本、排名和交易交互影响，不是独立可交易收益。",
    "因子增减同时改变权重时，收益差包含权重变化的影响；以保存的实际配置为准。",
    "所有方案使用相同日期、资金、交易费用和数据版本，费用后收益已包含换手和成交约束。",
    "历史成分、行业、财报及估值修订时点仍未严格PIT认证；复权收益不等于逐笔分红清算，固定冲击成本不等于真实成交容量。",
]


def run_comparison(
    config,
    plan,
    *,
    result_store=None,
    progress=None,
    dynamic_request=None,
    expected_sha=None,
    prepared_sample=None,
    execution_source: tuple[str, str] | None = None,
    **_,
):
    if prepared_sample is None:
        prepared_sample = prepare_comparison(config, plan, dynamic_request, expected_sha=expected_sha)
    if execution_source is None:
        execution_source = (prepared_sample["meta"]["id"], prepared_sample["meta"]["manifest_sha256"])
    data_version, manifest_sha256 = execution_source
    provenance = {
        **source_provenance(),
        "data_version": data_version,
        "manifest_sha256": manifest_sha256,
        "plan": plan.as_dict(),
        "backtest": asdict(config.backtest),
        "data": dynamic_request["data"],
        "evaluation": config.evaluation.as_dict(),
        "neutralization": config.neutralization.as_dict(),
        "financials": config.financials.as_dict() if config.financials else None,
        "sample_sha256": prepared_sample["sample_sha256"],
        "policy": "paired_signal_date_samples_v1",
    }
    identifier = "return-comparison-" + fingerprint(provenance)
    store = result_store or ResultStore(config.runs_dir)
    locks = config.runs_dir / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    notify = progress or (lambda *_: None)
    modes = ("independent", "common") if plan.comparison.sample_mode == "common" else ("independent",)
    try:
        with FileLock(locks / (identifier + ".lock"), timeout=0):
            reused = store.exists("experiments", identifier)
            if reused:
                result = store.get("experiments", identifier)
                for row in result["rows"]:
                    for category, key in (
                        ("backtests", "backtest_run_id"),
                        ("factors", "factor_run_id"),
                        ("evaluations", "evaluation_run_id"),
                    ):
                        if store.digest(category, row[key]) != row["artifact_sha256"][category]:
                            raise ValueError("对照引用的结果已变化，拒绝复用")
            else:
                rows = []
                store.publish_experiment_record(identifier, "plan", provenance)
                for mode in modes:
                    for candidate in plan.candidates:
                        current = candidate_config(config, candidate)
                        document = {
                            **dynamic_request,
                            "factors": candidate.factors.as_list(),
                            "backtest": asdict(current.backtest),
                            "neutralization": current.neutralization.as_dict(),
                        }
                        completed = run_historical_research(
                            document,
                            config.data.root,
                            config.runs_dir,
                            expected_sha=expected_sha,
                            result_store=store,
                            financials=config.financials,
                            references_frozen=not config.neutralization.allow_latest,
                            scoring_universe=prepared_sample["allowed"] if mode == "common" else None,
                        )
                        backtest = store.get("backtests", completed["backtest_run_id"])
                        factors = store.get("factors", completed["factor_run_id"], hydrate=False)
                        coverage = factors["metadata"]["coverage_by_date"]
                        dates = [d["date"] for d in prepared_sample["sample_days"]]
                        eligible = [coverage.get(day, {}).get("eligible", 0) for day in dates]
                        row = {
                            "candidate_id": candidate.id,
                            "sample": mode,
                            "period": "analysis",
                            "trading_days": prepared_sample["trading_days"],
                            **{
                                key: completed[key] for key in ("backtest_run_id", "factor_run_id", "evaluation_run_id")
                            },
                            "metrics": backtest["summary"]["metrics"],
                            "coverage": {
                                "signal_days": len(dates),
                                "scored_days": sum(n >= 2 for n in eligible),
                                "min_scored": min(eligible),
                                "max_scored": max(eligible),
                            },
                        }
                        row["artifact_sha256"] = {
                            category: store.digest(category, row[key])
                            for category, key in (
                                ("backtests", "backtest_run_id"),
                                ("factors", "factor_run_id"),
                                ("evaluations", "evaluation_run_id"),
                            )
                        }
                        rows.append(row)
                        notify(
                            len(rows),
                            len(modes) * len(plan.candidates),
                            f"{candidate.name} · {'共同样本' if mode == 'common' else '独立样本'}完成",
                        )
                result = {
                    "metadata": {
                        "id": identifier,
                        "name": plan.name,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "data_version": data_version,
                        "is_synthetic": False,
                        "validation_kind": "return_comparison",
                        "selected_candidate": None,
                        "provenance": provenance,
                    },
                    "plan": plan.as_dict(),
                    "selection": None,
                    "rows": rows,
                    "shared_parameters": {
                        "backtest": asdict(config.backtest),
                        "codes": sorted(config.data.codes),
                        "benchmark": config.backtest.benchmark or config.data.benchmark,
                        "universe": dynamic_request["data"],
                        "evaluation": config.evaluation.as_dict(),
                        "neutralization": config.neutralization.as_dict(),
                    },
                    "sample_days": prepared_sample["sample_days"],
                    "comparisons": return_differences(rows, plan.comparison),
                    "limitations": LIMITATIONS,
                }
                store.publish("experiments", identifier, result)
            report = store.publish_report(identifier, comparison_markdown(result), category="experiments")
            return {
                "run_id": identifier,
                "selected_candidate": None,
                "data_version": data_version,
                "reused": reused,
                "report": str(report),
                "result": str(store.path("experiments", identifier)),
            }
    except Timeout:
        raise ValueError("该收益对照正在运行，请等待完成") from None
