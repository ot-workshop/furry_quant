"""Small Qlib extensions for scheduled decisions and separately charged fees."""
from __future__ import annotations
import math
import numpy as np
from qlib.backtest.exchange import Exchange
from qlib.backtest.decision import Order,TradeDecisionWO
from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy
from qlib.backtest.executor import SimulatorExecutor
from qlib.backtest.utils import TradeCalendarManager,epsilon_change
import pandas as pd
from finance_quant.research.execution_policy import ExecutionCosts


class CostAwareExchange(Exchange):
    def __init__(self,*,execution_costs=None,**kwargs):
        self.execution_costs=execution_costs or ExecutionCosts(**{k:kwargs[k] for k in ('open_cost','close_cost','min_cost','impact_cost') if k in kwargs})
        costs=self.execution_costs
        super().__init__(**{**kwargs,'open_cost':costs.open_cost,'close_cost':costs.close_cost,'min_cost':costs.min_cost,'impact_cost':0.})

    def _get_buy_amount_by_cash_limit(self,trade_price,cash,cost_ratio):
        if cost_ratio==0:return max(0.,cash-self.min_cost)/trade_price
        return super()._get_buy_amount_by_cash_limit(trade_price,cash,cost_ratio)

    def _calc_trade_info_by_order(self,order,position,dealt_order_amount):
        from finance_quant.data.china_a_rules import max_order_shares,board
        # Fixed pools and historical pools share the same A-share order bounds.
        # Keep generic synthetic Qlib instruments usable outside the real-data path.
        supported=order.stock_id.startswith(('SH60','SH688','SZ00','SZ30'))
        if supported:
            factor=self.get_factor(order.stock_id,order.start_time,order.end_time)
            if position is not None and order.direction==Order.SELL and position.check_stock(order.stock_id):
                if position.get_stock_count(order.stock_id,bar='day')<1:
                    order.factor=factor;order.deal_amount=0.
                    return self.get_deal_price(order.stock_id,order.start_time,order.end_time,order.direction),0.,0.
            original=order.amount
            order.amount=min(order.amount,max_order_shares(order.stock_id,order.start_time)/factor)
            if position is not None and order.direction==Order.SELL and position.check_stock(order.stock_id):
                current=position.get_stock_amount(order.stock_id)
                # Qlib considers np.isclose(partial, full) a complete disposal.
                # A capped near-full order must not silently delete unpaid odd shares.
                if 0<order.amount<current and np.isclose(order.amount,current):
                    step=(1. if board(order.stock_id)=='star' else 100.)/factor
                    safe=current-(1e-8+1e-5*abs(current))*1.01
                    order.amount=max(0.,min(order.amount,math.floor(safe/step)*step))
            if order.amount<original:order.quantity_clipped_from=original*factor
        costs=self.execution_costs
        if costs.cost_mode=='legacy' and costs.impact_cost==0:
            return super()._calc_trade_info_by_order(order,position,dealt_order_amount)
        # Native Qlib still clips by volume/position, rounds shares and updates
        # Account. Charge our exact costs BEFORE Account receives the fill.
        # Never use the execution day's final volume to estimate opening impact.
        previous=self.open_cost,self.close_cost,self.min_cost,self.impact_cost
        self.open_cost=self.close_cost=self.min_cost=self.impact_cost=0.
        try:price,value,_=super()._calc_trade_info_by_order(order,position,dealt_order_amount)
        finally:self.open_cost,self.close_cost,self.min_cost,self.impact_cost=previous
        sell=order.direction==Order.SELL
        fee=costs.breakdown(value,sell,order.start_time)['total']
        if position is not None and not sell and value+fee>position.get_cash():
            amount=min(order.deal_amount,costs.max_buy_value(position.get_cash(),order.start_time)/price)
            order.deal_amount=self.round_amount_by_trade_unit(amount,order.factor)
            value=order.deal_amount*price;fee=costs.breakdown(value,sell,order.start_time)['total']
        if position is not None and ((not sell and value+fee>position.get_cash()+1e-7) or
                                     (sell and position.get_cash()+value<fee)):
            order.deal_amount=0.;value=fee=0.
        return price,value,fee


class ScheduledTopkStrategy(TopkDropoutStrategy):
    def __init__(self,*,rebalance_dates,**kwargs):
        self.rebalance_dates=set(rebalance_dates)
        super().__init__(**kwargs)

    def generate_trade_decision(self,execute_result=None):
        day,_=self.trade_calendar.get_step_time(self.trade_calendar.get_trade_step())
        if day.strftime('%Y-%m-%d') not in self.rebalance_dates:return TradeDecisionWO([],self)
        return super().generate_trade_decision(execute_result)


class DailyTradeCalendar(TradeCalendarManager):
    def get_step_time(self,trade_step=None,shift=0):
        step=self.get_trade_step() if trade_step is None else trade_step
        index=self.start_index+step-shift
        if index<0:raise ValueError('缺少信号日前的交易日历，不能读取未来日期作为前一日')
        if self.freq=='day' and index==len(self._calendar)-1:
            day=self._calendar[index]
            # Close the last existing daily bar at midnight; do not fabricate
            # an additional exchange session, quote, signal or evaluation label.
            return day,epsilon_change(pd.Timestamp(day).normalize()+pd.Timedelta(days=1))
        return super().get_step_time(trade_step,shift)


class DailySimulatorExecutor(SimulatorExecutor):
    def reset(self,common_infra=None,**kwargs):
        super().reset(common_infra=common_infra,**kwargs)
        calendar=self.level_infra.get('trade_calendar')
        if not isinstance(calendar,DailyTradeCalendar):
            self.level_infra.reset_infra(trade_calendar=DailyTradeCalendar(calendar.freq,start_time=calendar.start_time,
                end_time=calendar.end_time,level_infra=self.level_infra))
