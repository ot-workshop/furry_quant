"""Historical pool -> causal factors -> native Qlib -> diagnostic artifacts."""

from __future__ import annotations

import gc
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from finance_quant.application.factor_workflow import fingerprint, source_provenance
from finance_quant.application.real_workflow import adjusted_quotes, financial_limitation
from finance_quant.configuration import BacktestConfig
from finance_quant.data.china_a_rules import RULE_VERSION, apply_execution_rules
from finance_quant.data.historical_warehouse import execution_market, load_historical_input
from finance_quant.integrations.qlib_runner import run_qlib
from finance_quant.provenance import report_code_sha256
from finance_quant.research.evaluation import evaluate_factors
from finance_quant.research.execution_policy import EXECUTION_FIELDS
from finance_quant.research.fundamentals import financial_provenance, fundamentals_for
from finance_quant.research.market_factors import market_factors
from finance_quant.research.registry import EvaluationConfig, FactorConfig, FactorSpec
from finance_quant.research.report import evaluation_markdown
from finance_quant.storage import ResultStore
from finance_quant.storage_tables import StreamingTables, factor_frame, factor_predictions


def run_historical_research(
    request,
    warehouse_root,
    runs_root,
    *,
    expected_sha=None,
    stage=None,
    result_store=None,
    financials=None,
):
    stage = stage or (lambda *args: None)
    runs_root = Path(runs_root)
    config = FactorConfig(tuple(FactorSpec(**s) for s in request["factors"]))
    backtest = BacktestConfig(**request["backtest"])
    evaluation = EvaluationConfig(**request["evaluation"])
    from finance_quant.research.neutralization import industry_context, parse_neutralization

    neutralization = parse_neutralization(request.get("neutralization"))
    industry = industry_context(neutralization)
    if neutralization.mode == "industry":
        industry.validate_range(backtest.end)
    stage("prepare", "校验历史数据仓、每日成分及股票生命周期", 0)
    market, benchmark, sessions, meta = load_historical_input(
        warehouse_root, backtest.start, backtest.end, max(s.lookback for s in config.specs), expected_sha=expected_sha
    )
    provenance = {
        **source_provenance(),
        "data_version": meta["id"],
        "manifest_sha256": meta["manifest_sha256"],
        "start": backtest.start,
        "end": backtest.end,
        "factors": config.as_list(),
        "market_rules": RULE_VERSION,
        "membership_policy": meta["membership_policy"],
        **financial_provenance(financials, config),
    }
    provenance["neutralization"] = neutralization.as_dict()
    if industry:
        provenance["industry_version"] = industry.manifest["id"]
    store = result_store or ResultStore(runs_root)
    factor_id = "historical-factors-" + fingerprint(provenance)
    stage("factors", "按每日已知成分排名；调入前历史只用于预热", 1)
    if store.exists("factors", factor_id):
        factors = store.get("factors", factor_id, hydrate=False)
    else:
        with StreamingTables(store, "factors", factor_id) as tables:
            factors = market_factors(
                market,
                meta["id"],
                config,
                dynamic_context=meta,
                exchange_calendar=sessions,
                row_sink=tables.append,
                neutralization=neutralization,
                industry_history=industry,
                financials=fundamentals_for(financials, config, market),
            )
            factors["tables"] = tables.finish()
        factors["metadata"].update(
            id=factor_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            provenance=provenance,
            source=meta["source"],
            validation_counts=meta["validation_counts"],
        )
        store.publish("factors", factor_id, factors)
    gc.collect()
    run_provenance = {
        **provenance,
        "backtest": {key: value for key, value in request["backtest"].items() if key != "benchmark"},
        "factor_run_id": factor_id,
    }
    run_id = "historical-qlib-" + fingerprint(run_provenance)
    stage("backtest", "Qlib模拟动态池、调出退出、科创板数量及交易限制", 2)
    backtest_reused = store.exists("backtests", run_id)
    if not backtest_reused:
        execution = apply_execution_rules(execution_market(market, sessions, backtest.end), sessions)
        quotes = adjusted_quotes(execution, benchmark)
        del execution
        names = market.drop_duplicates("instrument").set_index("instrument")["name"].to_dict()
        signals = {**factors, "history": factor_frame(store, factor_id, factors)}
        result = run_qlib(
            quotes,
            signals,
            runs_root / ".qlib" / run_id,
            calendar=sessions,
            start=backtest.start,
            end=backtest.end,
            initial_date=meta["initial_date"],
            names=names,
            initial_cash=backtest.initial_cash,
            topk=backtest.topk,
            n_drop=backtest.n_drop,
            risk_degree=backtest.risk_degree,
            open_cost=backtest.open_cost,
            close_cost=backtest.close_cost,
            min_cost=backtest.min_cost,
            require_availability=True,
            historical=meta,
            **{key: getattr(backtest, key) for key in EXECUTION_FIELDS},
        )
        result["summary"].update(id=run_id, name="沪深300历史成分股动态回测", is_synthetic=False)
        result["metadata"].update(
            provenance=run_provenance,
            data_version=meta["id"],
            factor_run_id=factor_id,
            data_snapshot=str(warehouse_root),
            source=meta["source"],
            point_in_time_certified=False,
            validation_counts=meta["validation_counts"],
            benchmark_name="沪深300价格指数（000300.SH）",
            position_note="Qlib复权收益口径的等效持股数量；不是逐笔现金分红和券商实有股数账本",
            limitations=[
                "历史成分为月度快照后下一交易日生效，可能滞后实际调仓，精确公告可用时点未认证",
                "只用信号日及此前行情，按快照次日可用假设构造名单；成分真实公告时点仍未认证，预热不足和停牌不参与排名",
                "调出持仓无论n_drop如何均尝试退出；停牌或跌停时保留持仓、继续估值并重试，不假装卖出",
                "上市前不能交易；生命周期结束后的未退出持仓按最后可得价冻结估值，禁止交易，未模拟退市清算或强制零回收",
                "日线开盘模型不能重建盘中队列、临时停牌时刻、价格笼子及盘中滑点；保守按开盘涨跌停限制成交",
                "科创板最少200股、之后1股递增、限价单笔最多10万股；主板100股整手、单笔最多100万股；创业板2020-08-24起单笔最多30万股，此前100万股；整仓卖出允许余股",
                "交易数量按Qlib复权因子还原处理，现金分红不重复记现金；策略与价格指数的分红口径不同",
                "费用使用已保存模式与参数；分项模式佣金含规费、印花税及过户费另计，冲击成本为固定成交额比例压力假设；每日估值历史修订与发布时间未独立认证",
                financial_limitation(factors["metadata"]),
            ],
        )
        from finance_quant.research.industry_exposure import attach_industry_exposure

        memberships = pd.read_parquet(Path(warehouse_root) / "reference/memberships.parquet")
        attach_industry_exposure(result, industry, sessions, neutralization=neutralization, memberships=memberships)
        store.publish("backtests", run_id, result)
        del quotes, signals, result
        gc.collect()
    if backtest.benchmark is not None:
        from finance_quant.application.benchmark_workflow import publish_benchmark_view

        view = publish_benchmark_view(
            runs_root,
            run_id,
            backtest.benchmark,
            calendar=sessions,
            result_store=store,
        )
        run_id = view["run_id"]
        backtest_reused = view["reused"]
    stage("evaluation", "按当时动态股票池评分计算IC、分组及覆盖诊断", 3)
    eval_provenance = {
        **provenance,
        "factor_run_id": factor_id,
        "evaluation": evaluation.as_dict(),
        "report_code_sha256": report_code_sha256(),
    }
    eval_id = "historical-eval-" + fingerprint(eval_provenance)
    evaluation_reused = store.exists("evaluations", eval_id)
    if evaluation_reused:
        result = store.get("evaluations", eval_id, hydrate=False)
    else:
        evaluation_calendar = sessions[(sessions >= market.date.min()) & (sessions <= pd.Timestamp(backtest.end))]
        observations = factor_predictions(store, factor_id, factors)
        with StreamingTables(store, "evaluations", eval_id) as tables:
            result = evaluate_factors(
                market,
                factors,
                evaluation_calendar,
                start=backtest.start,
                end=backtest.end,
                config=evaluation,
                raw_predictions=observations,
                row_sink=tables.append,
            )
            result["tables"] = tables.finish()
        del observations
        for row in result["signal_coverage"]:
            row["universe"] = meta["declared_universe"][row["signal_date"]]
        result["metadata"] = {
            "id": eval_id,
            "name": "沪深300历史动态池因子评估",
            "is_synthetic": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "data_version": meta["id"],
            "factor_run_id": factor_id,
            "factor_config": config.as_list(),
            "factor_definitions": factors["metadata"]["factor_definitions"],
            "evaluation": evaluation.as_dict(),
            "start": backtest.start,
            "end": backtest.end,
            "sample_size": int(market.instrument.nunique()),
            "provenance": eval_provenance,
            "source": meta["source"],
            "membership_policy": meta["membership_policy"],
            "point_in_time_certified": False,
        }
        for key in ["neutralization", "industry_data", "temporal_audit", "financial_data"]:
            if key in factors["metadata"]:
                result["metadata"][key] = factors["metadata"][key]
        result["methodology"]["limitations"] = [
            s for s in result["methodology"]["limitations"] if not s.startswith("固定小股票池")
        ]
        result["methodology"]["limitations"] += [
            "评分按信号日已知动态成分；标签仍是独立固定持有期诊断，可能跨越后续调出，不等于实际策略成交收益",
            "月度成分快照的精确公告可用时点未认证",
        ]
        store.publish("evaluations", eval_id, result)
    store.publish_report(eval_id, evaluation_markdown(result))
    return {
        "data_version": meta["id"],
        "factor_run_id": factor_id,
        "backtest_run_id": run_id,
        "evaluation_run_id": eval_id,
        "factor_as_of": factors["metadata"]["as_of"],
        "backtest_reused": backtest_reused,
        "evaluation_reused": evaluation_reused,
    }
