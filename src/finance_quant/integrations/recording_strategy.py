"""Qlib strategies that record every requested and filled order."""

from __future__ import annotations

import numpy as np
from qlib.backtest.decision import Order

from finance_quant.integrations.china_a import HistoricalIndexStrategy
from finance_quant.integrations.execution import ScheduledTopkStrategy


class RecordingStrategyMixin:
    """Add deterministic execution records without defining classes inside a run."""

    def __init__(self, *, instrument_names, execution_costs, historical_context=None, **kwargs):
        self.instrument_names = instrument_names
        self.execution_costs = execution_costs
        self.historical_context = historical_context
        self.fills = []
        self.orders = []
        super().__init__(**kwargs)

    def post_exe_step(self, execute_result):
        for order, value, cost, price in execute_result or []:
            factor = float(order.factor) if order.factor is not None else 1.0
            record = {
                "date": order.start_time.strftime("%Y-%m-%d"),
                "instrument": order.stock_id,
                "name": self.instrument_names[order.stock_id],
                "side": "buy" if order.direction == Order.BUY else "sell",
                "requested_amount": float(order.amount * factor),
                "amount": float(order.deal_amount * factor),
                "price": float(price / factor) if np.isfinite(price) else None,
                "value": float(value),
                "cost": float(cost),
            }
            if hasattr(order, "quantity_clipped_from"):
                record["quantity_clipped_from"] = float(order.quantity_clipped_from)
            record["cost_breakdown"] = self.execution_costs.breakdown(
                float(value),
                order.direction == Order.SELL,
                order.start_time,
            )
            if not np.isclose(
                record["cost_breakdown"]["total"],
                float(cost),
                rtol=1e-9,
                atol=1e-7,
            ):
                raise ValueError("费用明细与Qlib实际账户扣费不一致")
            if self.historical_context is not None:
                record["reason"] = getattr(self, "pending_exits", {}).get(
                    order.stock_id,
                    "rank_selection" if order.direction == Order.BUY else "score_rotation",
                )
            self.orders.append(record)
            if order.deal_amount > 0:
                self.fills.append(record)


class RecordingScheduledStrategy(RecordingStrategyMixin, ScheduledTopkStrategy):
    """Fixed-universe scheduled top-k strategy with execution records."""


class RecordingHistoricalStrategy(RecordingStrategyMixin, HistoricalIndexStrategy):
    """Historical-index strategy with membership exits and execution records."""
