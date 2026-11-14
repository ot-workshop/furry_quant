"""Translate native Qlib portfolio output into stable research artifacts."""

from __future__ import annotations

from datetime import datetime, timezone

from finance_quant.data.china_a_rules import RULE_VERSION
from finance_quant.data.temporal_audit import add_execution_audit
from finance_quant.research.performance import enrich_backtest


def _equity_curve(report, initial_cash, initial_date):
    equity = [{"date": initial_date, "equity": 1.0, "benchmark": 1.0, "drawdown": 0.0, "cost": 0.0}]
    benchmark_value, peak = 1.0, 1.0
    for day, row in report.iterrows():
        value = float(row["account"] / initial_cash)
        benchmark_value *= 1 + float(row["bench"])
        peak = max(peak, value)
        equity.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "equity": value,
                "benchmark": benchmark_value,
                "drawdown": value / peak - 1,
                "cost": float(row["cost"]),
                "cash": float(row["cash"]),
                "account_value": float(row["account"]),
            }
        )
    return equity


def _holdings(positions, quotes, names):
    quote_factors = quotes.set_index(["instrument", "date"])["factor"]
    holdings = []
    for day, position in positions.items():
        weights = position.get_stock_weight_dict()
        for instrument in sorted(position.get_stock_list()):
            factor = float(quote_factors.loc[(instrument, day)])
            holdings.append(
                {
                    "date": day.strftime("%Y-%m-%d"),
                    "instrument": instrument,
                    "name": names[instrument],
                    "amount": float(position.get_stock_amount(instrument) * factor),
                    "price": float(position.get_stock_price(instrument) / factor),
                    "weight": float(weights.get(instrument, 0)),
                    "cash": float(position.get_cash()),
                }
            )
    return holdings


def export_qlib_result(
    *,
    qlib_version,
    report,
    positions,
    strategy,
    quotes,
    factors,
    names,
    start,
    end,
    initial_date,
    initial_cash,
    costs_policy,
    scheduled,
    rebalance_frequency,
    config,
    topk,
    n_drop,
    risk_degree,
    historical,
):
    """Build one JSON-safe artifact from Qlib account and strategy state."""
    if report.empty:
        raise ValueError("Qlib returned an empty report")
    equity = _equity_curve(report, initial_cash, initial_date)
    holdings = _holdings(positions, quotes, names)
    costs = sum(fill["cost"] for fill in strategy.fills)
    metrics = {
        "total_return": equity[-1]["equity"] - 1,
        "max_drawdown": min(point["drawdown"] for point in equity),
        "total_cost": costs / initial_cash,
        "total_cost_amount": costs,
        "avg_turnover": float(report["turnover"].mean()),
        "benchmark_return": equity[-1]["benchmark"] - 1,
    }
    result = {
        "summary": {
            "id": "unpublished",
            "name": "未发布回测",
            "status": "succeeded",
            "strategy": "TopkDropoutStrategy",
            "start_date": start,
            "end_date": end,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metrics": metrics,
        },
        "metadata": {
            "engine": "qlib",
            "engine_version": qlib_version,
            "execution_costs": costs_policy.as_dict(),
            "cost_summary": {
                key: sum(trade["cost_breakdown"][key] for trade in strategy.fills)
                for key in ["commission", "stamp_tax", "transfer_fee", "impact", "legacy_fee", "total"]
            },
            "rebalance_policy": {
                "frequency": rebalance_frequency,
                "dates": scheduled,
                "scheduled_count": len(scheduled),
                "timing": "区间首日建仓；随后每周/每月首个交易日开盘，使用前一交易日评分；每日估值",
                "exit_policy": (
                    "动态池成分调出每日重试，非调仓日卖出后现金等下个调仓日补仓"
                    if historical is not None
                    else "只在调仓日轮换"
                ),
            },
            "config": {
                **config,
                "topk": topk,
                "n_drop": n_drop,
                "risk_degree": risk_degree,
                "rebalance_frequency": rebalance_frequency,
                "execution_costs": costs_policy.as_dict(),
                "signal_lag": "previous_trading_day",
                "factor_run_id": factors["metadata"]["id"],
            },
        },
        "equity": equity,
        "positions": holdings,
        "trades": strategy.fills,
        "orders": strategy.orders,
        "qlib_report": [
            {"date": day.strftime("%Y-%m-%d"), **{key: float(value) for key, value in row.items()}}
            for day, row in report.iterrows()
        ],
    }
    if historical is not None:
        result["execution_events"] = strategy.events
        final_position = positions[report.index[-1]]
        result["metadata"]["execution_summary"] = {
            "events": len(strategy.events),
            "blocked_exits": sum(
                event["action"] == "sell" and event["reason"] != "score_rotation" and event["status"] == "blocked"
                for event in strategy.events
            ),
            "pending_exits": [
                {"instrument": code, "reason": reason}
                for code, reason in strategy.pending_exits.items()
                if code in final_position.get_stock_list()
            ],
        }
        result["metadata"]["universe_policy"] = historical["membership_policy"]
        result["metadata"]["market_rule_version"] = RULE_VERSION
        result["summary"]["strategy"] = "HistoricalIndexTopkStrategy"
        if "universe_filters" in historical:
            result["summary"]["strategy"] = "DynamicUniverseTopkStrategy"
    add_execution_audit(result, quotes, factors)
    return enrich_backtest(result)
