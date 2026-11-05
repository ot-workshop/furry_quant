import json

import pandas as pd
import pytest

from finance_quant.data.temporal_audit import capacity_diagnostics, restore_valuation_dates, validate_valuation_times


@pytest.mark.parametrize(
    "field,column", [("pe_ttm", "valuation_asof"), ("pb", "pb_asof"), ("total_mv", "total_mv_asof")]
)
def test_active_future_valuation_dates_are_rejected(field, column):
    frame = pd.DataFrame({"date": pd.to_datetime(["2024-01-02"]), field: [1.0], column: ["2024-01-03"]})
    with pytest.raises(ValueError, match="未来估值"):
        validate_valuation_times(frame, {field})
    validate_valuation_times(frame, {"close"})
    frame[column] = "2024-01-02"
    validate_valuation_times(frame, {field})


def test_actual_factor_path_rejects_unknown_or_future_availability():
    from test_factor_configuration import sample_market

    from finance_quant.research.market_factors import market_factors
    from finance_quant.research.registry import FactorConfig, FactorSpec

    m = sample_market()
    m["valuation_available_at"] = m.date.dt.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=19)
    with pytest.raises(ValueError, match="晚于当日决策"):
        market_factors(m, "fixture", FactorConfig((FactorSpec("value", 1.0),)))
    result = market_factors(m, "fixture", FactorConfig((FactorSpec("momentum", 1.0, 5),)))
    assert result["metadata"]["temporal_audit"]["valuation_fields_used"] == []
    assert result["metadata"]["temporal_audit"]["point_in_time_certified"] is False


def test_partial_legacy_dates_stay_unknown_but_explicit_bad_or_carried_dates_fail():
    m = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "pe_ttm": [10.0, 10.0],
            "valuation_asof": ["2024-01-02", None],
            "valuation_carried": [False, False],
        }
    )
    validate_valuation_times(m, {"pe_ttm"})
    m.loc[1, "valuation_asof"] = "not-a-date"
    with pytest.raises(ValueError, match="未来估值"):
        validate_valuation_times(m, {"pe_ttm"})
    m.loc[1, "valuation_asof"] = None
    m.loc[1, "valuation_carried"] = True
    with pytest.raises(ValueError, match="未来估值"):
        validate_valuation_times(m, {"pe_ttm"})


def test_carried_observation_date_recovers_without_changing_value_or_archive():
    m = pd.DataFrame(
        {
            "instrument": ["SH600000"] * 4,
            "date": pd.bdate_range("2024-01-02", periods=4),
            "pe_ttm": [10.0, 10.0, 10.0, 12.0],
            "valuation_carried": [False, True, True, False],
            "is_suspended": [False, True, True, False],
        }
    )
    original = m.copy(deep=True)
    r = restore_valuation_dates(m)
    assert r.valuation_asof.tolist() == ["2024-01-02", "2024-01-02", "2024-01-02", "2024-01-05"]
    pd.testing.assert_frame_equal(original, m)
    m.loc[2, "pe_ttm"] = 11.0
    with pytest.raises(ValueError, match="可核对"):
        restore_valuation_dates(m)


def test_capacity_uses_only_previous_turnover_and_does_not_claim_fill_guarantee():
    q = pd.DataFrame(
        {
            "instrument": "SH600000",
            "date": pd.bdate_range("2024-01-02", periods=23),
            "close": 10.0,
            "volume": 100.0,
            "amount": 1000.0,
        }
    )
    t = [{"instrument": "SH600000", "date": q.date.iloc[20].strftime("%Y-%m-%d"), "value": 200.0}]
    before = capacity_diagnostics(q, t)
    assert before["max_prior_turnover_ratio"] == pytest.approx(0.2)
    q.loc[20:, "amount"] = 1e12
    assert capacity_diagnostics(q, t) == before
    assert before["above_ten_percent"] == 1


@pytest.mark.parametrize(
    "code,day,cap",
    [("SH600000", "2024-01-02", 1000000), ("SZ300001", "2020-08-21", 1000000), ("SZ300001", "2020-08-24", 300000)],
)
def test_fixed_exchange_limits_large_orders_and_conserves_odd_remainders(tmp_path, code, day, cap):
    import qlib
    from qlib.backtest.decision import Order
    from qlib.backtest.position import Position

    from finance_quant.integrations.execution import CostAwareExchange
    from finance_quant.integrations.qlib_runner import write_features

    days = pd.bdate_range(day, periods=2)
    q = pd.DataFrame(
        {
            "date": days,
            "instrument": code,
            "open": 10.0,
            "close": 10.0,
            "high": 10.0,
            "low": 10.0,
            "volume": 1e9,
            "factor": 1.0,
            "change": 0.0,
            "up_limit": 11.0,
            "down_limit": 9.0,
        }
    )
    write_features(q, days, tmp_path / "qlib")
    qlib.init(provider_uri=str(tmp_path / "qlib"), region="cn", kernels=1, expression_cache=None, dataset_cache=None)
    ex = CostAwareExchange(
        start_time=days[0],
        end_time=days[-1],
        codes=[code],
        deal_price="$open",
        limit_threshold=("$open < 0", "$open < 0"),
        open_cost=0.0,
        close_cost=0.0,
        min_cost=0.0,
        impact_cost=0.0,
    )

    def order(amount, direction):
        return Order(stock_id=code, amount=amount, direction=direction, start_time=days[0], end_time=days[0])

    cash = 1e9
    p = Position(cash=cash)
    buy = order(cap + 1000, Order.BUY)
    value, fee, _ = ex.deal_order(buy, position=p)
    assert buy.deal_amount == cap and value == cap * 10 and p.get_cash() + p.get_stock_amount(code) * 10 == cash
    same = order(cap, Order.SELL)
    assert ex.deal_order(same, position=p)[0] == 0
    # A capped full-disposal request must preserve the unpaid odd lot.
    p = Position(cash=100.0, position_dict={code: {"amount": cap + 5.0, "price": 10.0, "count_day": 1}})
    sell = order(cap + 5.0, Order.SELL)
    value, fee, _ = ex.deal_order(sell, position=p)
    assert 0 < sell.deal_amount <= cap and p.check_stock(code)
    assert p.get_stock_amount(code) == pytest.approx(cap + 5 - sell.deal_amount)
    assert p.get_cash() + p.get_stock_amount(code) * 10 == pytest.approx(100 + (cap + 5) * 10)


@pytest.mark.parametrize(
    "available,match", [("2024-01-02T09:00:00+08:00", "收盘前"), ("2024-01-03T09:30:00+08:00", "before next-session")]
)
def test_real_signals_cannot_be_backdated_or_arrive_at_execution_open(tmp_path, available, match):
    from finance_quant.integrations.qlib_runner import run_qlib

    dates = pd.bdate_range("2024-01-02", periods=3)
    codes = ["SH600000", "SH000300"]
    q = pd.concat(
        [
            pd.DataFrame(
                {
                    "date": dates,
                    "instrument": code,
                    "open": 10.0,
                    "close": 10.0,
                    "high": 10.0,
                    "low": 10.0,
                    "volume": 1e6,
                    "factor": 1.0,
                    "change": 0.0,
                    "up_limit": 11.0,
                    "down_limit": 9.0,
                }
            )
            for code in codes
        ]
    )
    f = {
        "metadata": {"id": "fixture", "signal_uses_close": True},
        "history": [{"date": "2024-01-02", "instrument": "SH600000", "available_at": available, "composite": 100.0}],
    }
    with pytest.raises(ValueError, match=match):
        run_qlib(
            q,
            f,
            tmp_path / "qlib",
            calendar=dates,
            start="2024-01-03",
            end="2024-01-04",
            initial_date="2024-01-02",
            names={"SH600000": "fixture"},
            topk=1,
            require_availability=True,
        )


def test_industry_manifest_cannot_extend_known_date_or_upgrade_certification(tmp_path):
    from test_industry_neutralization import history

    from finance_quant.data.industries import load_industries, publish_industries

    h = publish_industries(history(["SH600000"]).frame, root=tmp_path)
    path = tmp_path / h.manifest["id"] / "manifest.json"
    m = json.loads(path.read_text(encoding="utf-8"))
    m["data_asof"] = "2099-01-01"
    path.write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(ValueError, match="时点声明"):
        load_industries(h.manifest["id"], root=tmp_path)
