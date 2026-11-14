"""Small Qlib extensions; native Exchange/Account still match and account."""

from __future__ import annotations

import copy

import numpy as np
import pandas as pd
from qlib.backtest.decision import Order, TradeDecisionWO
from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy

from finance_quant.data.china_a_rules import round_shares
from finance_quant.integrations.execution import CostAwareExchange


class MarketStates:
    """Columnar per-instrument lookup instead of millions of Python dictionaries."""

    columns = ["open", "up_limit", "down_limit", "execution_available", "execution_state", "has_daily_price_limit"]

    def __init__(self, quotes, codes):
        self.groups = {}
        frame = quotes.loc[quotes.instrument.isin(codes), ["instrument", "date", *self.columns]]
        for instrument, part in frame.groupby("instrument", sort=False):
            part = part.sort_values("date")
            self.groups[instrument] = (
                part.date.to_numpy(dtype="datetime64[ns]").view("i8"),
                [part[key].to_numpy() for key in self.columns],
            )

    def get(self, key):
        instrument, date = key
        if instrument not in self.groups:
            return None
        dates, values = self.groups[instrument]
        stamp = pd.Timestamp(date).value
        position = dates.searchsorted(stamp)
        if position >= len(dates) or dates[position] != stamp:
            return None
        return {name: array[position] for name, array in zip(self.columns, values, strict=True)}


class ChinaAExchange(CostAwareExchange):
    def __init__(self, *, market_states, **kwargs):
        self.market_states = market_states
        self._rounding_instrument = None
        self._rounding_day = None
        super().__init__(**kwargs)

    def block_reason(self, stock_id, start_time, direction):
        row = self.market_states.get((stock_id, pd.Timestamp(start_time).normalize()))
        if row is None:
            return "missing_quote_or_before_listing"
        if not row["execution_available"]:
            return row["execution_state"]
        if row["has_daily_price_limit"]:
            if direction in [None, Order.BUY] and row["open"] >= row["up_limit"] - 1e-7:
                return "limit_up"
            if direction in [None, Order.SELL] and row["open"] <= row["down_limit"] + 1e-7:
                return "limit_down"
        return None

    def is_stock_tradable(self, stock_id, start_time, end_time, direction=None):
        return self.block_reason(stock_id, start_time, direction) is None and super().is_stock_tradable(
            stock_id, start_time, end_time, direction
        )

    def round_amount_by_trade_unit(self, deal_amount, factor=None, stock_id=None, start_time=None, end_time=None):
        code = stock_id or self._rounding_instrument
        if code is None:
            raise ValueError("混合板块数量取整必须指定股票")
        factor = self._get_factor_or_raise_error(factor, stock_id, start_time, end_time)
        return round_shares(deal_amount, factor, code, start_time if start_time is not None else self._rounding_day)

    def _calc_trade_info_by_order(self, order, position, dealt_order_amount):
        self._rounding_instrument = order.stock_id
        self._rounding_day = order.start_time
        try:
            return super()._calc_trade_info_by_order(order, position, dealt_order_amount)
        finally:
            self._rounding_instrument = None
            self._rounding_day = None

    def _get_buy_amount_by_cash_limit(self, trade_price, cash, cost_ratio):
        if cost_ratio == 0:
            return max(0.0, cash - self.min_cost) / trade_price
        return super()._get_buy_amount_by_cash_limit(trade_price, cash, cost_ratio)


class HistoricalIndexStrategy(TopkDropoutStrategy):
    def __init__(
        self,
        *,
        membership_by_execution_date,
        decision_coverage,
        rebalance_dates=None,
        exit_reason="index_exit",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.membership = membership_by_execution_date
        self.exit_reason = exit_reason
        self.coverage = decision_coverage
        self.pending_exits = {}
        self.events = []
        self.fills = []
        self.orders = []
        self.rebalance_dates = set(rebalance_dates) if rebalance_dates is not None else None

    def generate_trade_decision(self, execute_result=None):
        step = self.trade_calendar.get_trade_step()
        start, end = self.trade_calendar.get_step_time(step)
        before, until = self.trade_calendar.get_step_time(step, shift=1)
        day = start.strftime("%Y-%m-%d")
        signal_day = before.strftime("%Y-%m-%d")
        rebalance = self.rebalance_dates is None or day in self.rebalance_dates
        if day not in self.membership or signal_day not in self.coverage:
            raise ValueError("动态股票池缺少当日已声明的名单或评分覆盖，拒绝沿用旧信号")
        scores = self.signal.get_signal(start_time=before, end_time=until)
        scores = (
            pd.Series(dtype=float)
            if scores is None
            else scores.iloc[:, 0]
            if isinstance(scores, pd.DataFrame)
            else scores
        )
        members = set(self.membership[day])
        scores = scores.loc[scores.index.isin(members)].dropna()
        ranking = sorted(scores.index, key=lambda code: (-float(scores[code]), code))
        if rebalance and len(ranking) < self.topk:
            self.events.append(
                {
                    "date": day,
                    "instrument": "股票池",
                    "action": "buy",
                    "reason": "candidate_shortfall",
                    "status": "blocked",
                    "block_reason": "insufficient_valid_scores",
                }
            )
        temp = copy.deepcopy(self.trade_position)
        held = list(temp.get_stock_list())
        self.pending_exits = {code: reason for code, reason in self.pending_exits.items() if code in held}
        for code in held:
            if code not in members:
                self.pending_exits.setdefault(code, self.exit_reason)
            row = self.trade_exchange.market_states.get((code, start.normalize()))
            if row and row["execution_state"] == "outside_listing_lifecycle":
                self.pending_exits.setdefault(code, "outside_listing_lifecycle")
        sell_codes = list(sorted(self.pending_exits))
        candidates = [c for c in ranking if c not in held]
        remaining = sorted(
            [c for c in held if c not in self.pending_exits], key=lambda c: (float(scores.get(c, -np.inf)), c)
        )
        for candidate in candidates[: self.n_drop if rebalance else 0]:
            if not remaining:
                break
            worst = remaining[0]
            if float(scores[candidate]) > float(scores.get(worst, -np.inf)):
                sell_codes.append(worst)
                remaining.pop(0)
        sell_orders = []
        buy_orders = []
        cash = temp.get_cash()
        for code in sell_codes:
            reason = self.pending_exits.get(code, "score_rotation")
            block = self.trade_exchange.block_reason(code, start, Order.SELL)
            if temp.get_stock_count(code, bar="day") < 1:
                block = block or "t_plus_one"
            if block:
                self.events.append(
                    {
                        "date": day,
                        "instrument": code,
                        "action": "sell",
                        "reason": reason,
                        "status": "blocked",
                        "block_reason": block,
                    }
                )
                continue
            order = Order(
                stock_id=code, amount=temp.get_stock_amount(code), direction=Order.SELL, start_time=start, end_time=end
            )
            sell_orders.append(order)
            value, cost, _ = self.trade_exchange.deal_order(order, position=temp)
            cash += value - cost
            self.events.append(
                {
                    "date": day,
                    "instrument": code,
                    "action": "sell",
                    "reason": reason,
                    "status": "submitted",
                    "block_reason": None,
                }
            )
        slots = max(0, self.topk - len(temp.get_stock_list()))
        buy = (
            [
                code
                for code in ranking
                if code not in temp.get_stock_list() and code not in sell_codes and code not in self.pending_exits
            ][:slots]
            if rebalance
            else []
        )
        value = cash * self.risk_degree / len(buy) if buy else 0.0
        for code in buy:
            block = self.trade_exchange.block_reason(code, start, Order.BUY)
            if block:
                self.events.append(
                    {
                        "date": day,
                        "instrument": code,
                        "action": "buy",
                        "reason": "rank_selection",
                        "status": "blocked",
                        "block_reason": block,
                    }
                )
                continue
            price = self.trade_exchange.get_deal_price(code, start, end, Order.BUY)
            factor = self.trade_exchange.get_factor(code, start, end)
            amount = self.trade_exchange.round_amount_by_trade_unit(
                value / price, factor, stock_id=code, start_time=start, end_time=end
            )
            if amount <= 0:
                self.events.append(
                    {
                        "date": day,
                        "instrument": code,
                        "action": "buy",
                        "reason": "rank_selection",
                        "status": "blocked",
                        "block_reason": "insufficient_cash_for_board_minimum",
                    }
                )
                continue
            buy_orders.append(Order(stock_id=code, amount=amount, direction=Order.BUY, start_time=start, end_time=end))
        return TradeDecisionWO(sell_orders + buy_orders, self)
