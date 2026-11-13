import hashlib
import json

import numpy as np
import pandas as pd
import pytest
import qlib
from qlib.backtest.decision import Order
from qlib.backtest.position import Position

from finance_quant.application.historical_workflow import run_historical_research
from finance_quant.data.china_a_rules import apply_execution_rules, round_shares
from finance_quant.data.historical_warehouse import load_historical_input
from finance_quant.integrations.china_a import ChinaAExchange
from finance_quant.integrations.qlib_runner import write_features
from finance_quant.research.market_factors import market_factors
from finance_quant.research.registry import FactorConfig, FactorSpec


def warehouse_fixture(root, *, blocked=False, terminal=False):
    root.mkdir()
    reference = root / "reference"
    reference.mkdir()
    dates = pd.bdate_range("2024-01-02", "2024-02-02")
    calendar = pd.DataFrame({"date": dates, "is_open": True})
    intervals = pd.DataFrame(
        [
            {
                "code": "600000.SH",
                "snapshot_date": pd.Timestamp("2023-12-29"),
                "effective_from": dates[0],
                "effective_until": pd.Timestamp("2024-01-12"),
                "weight": 50,
            },
            {
                "code": "000001.SZ",
                "snapshot_date": pd.Timestamp("2023-12-29"),
                "effective_from": dates[0],
                "effective_until": pd.NaT,
                "weight": 50,
            },
            {
                "code": "688001.SH",
                "snapshot_date": pd.Timestamp("2024-01-11"),
                "effective_from": pd.Timestamp("2024-01-12"),
                "effective_until": pd.NaT,
                "weight": 50,
            },
        ]
    )
    instruments = pd.DataFrame({"ts_code": ["600000.SH", "000001.SZ", "688001.SH"], "list_date": ["20100101"] * 3})
    benchmark = pd.DataFrame(
        {
            "date": dates,
            "instrument": "SH000300",
            "open": 100.0,
            "close": 100.0,
            "high": 101.0,
            "low": 99.0,
            "volume": 1e8,
        }
    )
    for name, frame in [
        ("calendar", calendar),
        ("memberships", intervals),
        ("instruments", instruments),
        ("benchmark", benchmark),
    ]:
        frame.to_parquet(reference / f"{name}.parquet", index=False)
    assets = {}
    for code, slope in [("600000.SH", 0.2), ("000001.SZ", -0.01), ("688001.SH", 0.12)]:
        code_dates = dates[:8] if terminal and code == "600000.SH" else dates
        close = 10 + np.arange(len(code_dates)) * slope
        frame = pd.DataFrame(
            {
                "date": code_dates,
                "instrument": code[-2:] + code[:6],
                "name": code,
                "industry": "fixture",
                "open": close,
                "close": close,
                "high": close + 0.1,
                "low": close - 0.1,
                "pre_close": np.r_[10, close[:-1]],
                "volume": 1e6,
                "amount": 1e7,
                "adj_factor": 1.0,
                "pe_ttm": 10.0,
                "pb": 1.0,
                "total_mv": 1e10,
                "up_limit": np.r_[10, close[:-1]] * 1.1,
                "down_limit": np.r_[10, close[:-1]] * 0.9,
                "is_suspended": False,
                "history_sessions_before": np.arange(len(code_dates)),
                "reviewed_unbounded_session": False,
                "validation_level": "single_source_passed",
            }
        )
        if blocked and code == "600000.SH":
            pos = frame.index[frame.date.eq("2024-01-12")][0]
            previous = frame.loc[pos - 1, "close"]
            frame.loc[pos, ["open", "close", "high", "low", "pre_close"]] = previous
            frame.loc[pos, ["volume", "amount"]] = 0.0
            frame.loc[pos, "is_suspended"] = True
            pos = frame.index[frame.date.eq("2024-01-15")][0]
            low = frame.loc[pos, "down_limit"]
            frame.loc[pos, ["open", "close", "high", "low"]] = low
        frame["change"] = frame.close / frame.pre_close - 1
        directory = root / "assets" / code
        directory.mkdir(parents=True)
        path = directory / "market.parquet"
        frame.to_parquet(path, index=False)
        assets[code] = {
            "code": code,
            "start": frame.date.min().strftime("%Y%m%d"),
            "end": frame.date.max().strftime("%Y%m%d"),
            "file": path.relative_to(root).as_posix(),
            "rows": len(frame),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "validation_level": "single_source_passed",
        }
    manifest = {
        "schema_version": "historical-index-warehouse-v1",
        "start": "2024-01-08",
        "end": "2024-02-02",
        "data_start": "2024-01-02",
        "warmup_sessions": 3,
        "assets": assets,
        "membership_policy": "monthly_snapshot_next_session",
        "reference_files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in reference.glob("*.parquet")},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def request():
    return {
        "name": "fixture",
        "data": {"mode": "index_warehouse", "warehouse_id": "fixture"},
        "factors": [{"name": "momentum", "window": 2, "weight": 1.0}],
        "backtest": {
            "start": "2024-01-08",
            "end": "2024-01-31",
            "initial_cash": 100003.0,
            "topk": 1,
            "n_drop": 0,
            "risk_degree": 0.9,
            "open_cost": 0.001,
            "close_cost": 0.001,
            "min_cost": 5.0,
        },
        "evaluation": {"horizons": [1], "groups": 2, "min_cross_section": 3},
    }


def test_membership_and_prehistory_do_not_rank_future_constituents(tmp_path):
    root = tmp_path / "warehouse"
    warehouse_fixture(root)
    market, _, calendar, meta = load_historical_input(root, "2024-01-08", "2024-01-31", 2)
    config = FactorConfig((FactorSpec("momentum", 1.0, 2),))
    factors = market_factors(market, meta["id"], config, dynamic_context=meta, exchange_calendar=calendar)
    earlier = [r for r in factors["history"] if r["date"] < "2024-01-11"]
    assert not any(r["instrument"] == "SH688001" for r in earlier)
    assert any(r["instrument"] == "SH688001" and r["date"] == "2024-01-11" for r in factors["history"])
    changed = market.copy()
    changed.loc[changed.date.gt("2024-01-16"), "close"] *= 100
    other = market_factors(changed, meta["id"], config, dynamic_context=meta, exchange_calendar=calendar)
    assert [r for r in factors["history"] if r["date"] <= "2024-01-16"] == [
        r for r in other["history"] if r["date"] <= "2024-01-16"
    ]
    # Metadata membership perturbations effective only after an earlier score
    # cannot contaminate its raw values or cross-sectional percentiles.
    changed = market.copy()
    changed.loc[changed.instrument.eq("SH688001") & changed.date.lt("2024-01-11"), "close"] *= 7
    other = market_factors(changed, meta["id"], config, dynamic_context=meta, exchange_calendar=calendar)
    assert earlier == [r for r in other["history"] if r["date"] < "2024-01-11"]
    path = root / "reference/memberships.parquet"
    intervals = pd.read_parquet(path)
    intervals.loc[intervals.code.eq("688001.SH"), "snapshot_date"] = pd.Timestamp("2024-01-12")
    intervals.loc[intervals.code.eq("688001.SH"), "effective_from"] = pd.Timestamp("2024-01-15")
    intervals.loc[intervals.code.eq("600000.SH"), "effective_until"] = pd.Timestamp("2024-01-15")
    intervals.to_parquet(path, index=False)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["reference_files"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    changed, _, new_calendar, new_meta = load_historical_input(root, "2024-01-08", "2024-01-31", 2)
    other = market_factors(changed, new_meta["id"], config, dynamic_context=new_meta, exchange_calendar=new_calendar)
    assert earlier == [r for r in other["history"] if r["date"] < "2024-01-11"]


@pytest.mark.parametrize("blocked", [False, True])
def test_native_qlib_forces_exit_even_when_ndrop_zero_and_retries_blocked_exit(tmp_path, blocked):
    root = tmp_path / "warehouse"
    warehouse_fixture(root, blocked=blocked)
    output = run_historical_research(request(), root, tmp_path / "runs")
    result = json.loads(
        (tmp_path / "runs/backtests" / output["backtest_run_id"] / "result.json").read_text(encoding="utf-8")
    )
    sales = [t for t in result["trades"] if t["instrument"] == "SH600000" and t["side"] == "sell"]
    assert sales and sales[0]["date"] == ("2024-01-16" if blocked else "2024-01-12")
    assert not any(
        t["instrument"] == "SH600000" and t["side"] == "buy" and t["date"] >= "2024-01-12" for t in result["trades"]
    )
    if blocked:
        events = [e for e in result["execution_events"] if e["reason"] == "index_exit" and e["status"] == "blocked"]
        assert [e["block_reason"] for e in events[:2]] == ["suspended", "limit_down"]
        assert any(p["instrument"] == "SH600000" and p["date"] == "2024-01-15" for p in result["positions"])
    star = [t for t in result["trades"] if t["instrument"] == "SH688001" and t["side"] == "buy"]
    assert star and all(t["amount"] >= 199.999 and abs(t["amount"] - round(t["amount"])) < 0.01 for t in star)
    assert result["metadata"]["engine"] == "qlib"
    assert all(p["cash"] >= -1e-6 for p in result["positions"])
    assert run_historical_research(request(), root, tmp_path / "runs")["backtest_reused"]


def test_board_quantities_no_band_ipo_and_lifecycle_valuation(tmp_path):
    assert round_shares(199, 1, "SH688001") == 0
    assert round_shares(201.9, 1, "SH688001") == 201
    assert round_shares(201.9, 1, "SH600000") == 200
    assert round_shares(403.8, 0.5, "SH688001") == 402
    assert round_shares(2000000, 1, "SH600000") == 1000000
    assert round_shares(500000, 1, "SZ300001", "2020-08-21") == 500000
    assert round_shares(500000, 1, "SZ300001", "2020-08-24") == 300000
    root = tmp_path / "warehouse"
    warehouse_fixture(root)
    market, _, calendar, meta = load_historical_input(root, "2024-01-08", "2024-01-31", 2)
    star = market.loc[market.instrument.eq("SH688001")].copy()
    star["list_date"] = pd.Timestamp("2024-01-02")
    star["execution_available"] = True
    star["execution_state"] = "active"
    processed = apply_execution_rules(star, calendar)
    assert not processed.loc[processed.date.le("2024-01-08"), "has_daily_price_limit"].any()
    assert processed.loc[processed.date.eq("2024-01-09"), "has_daily_price_limit"].all()
    star["list_date"] = pd.Timestamp("2023-01-02")
    assert apply_execution_rules(star, calendar).has_daily_price_limit.all()
    star.loc[star.date.eq("2024-01-10"), ["up_limit", "down_limit"]] = float("nan")
    unknown = apply_execution_rules(star, calendar).loc[lambda f: f.date.eq("2024-01-10")].iloc[0]
    assert not unknown.execution_available and unknown.execution_state == "unknown_price_limits"


def test_lifecycle_end_freezes_unexited_position_without_future_last_day_sale(tmp_path):
    root = tmp_path / "warehouse"
    warehouse_fixture(root, terminal=True)
    output = run_historical_research(request(), root, tmp_path / "runs")
    result = json.loads(
        (tmp_path / "runs/backtests" / output["backtest_run_id"] / "result.json").read_text(encoding="utf-8")
    )
    assert not any(t["instrument"] == "SH600000" and t["side"] == "sell" for t in result["trades"])
    held = [p for p in result["positions"] if p["instrument"] == "SH600000"]
    assert held[-1]["date"] == "2024-01-31"
    assert result["metadata"]["execution_summary"]["pending_exits"] == [
        {"instrument": "SH600000", "reason": "index_exit"}
    ]
    events = [e for e in result["execution_events"] if e["instrument"] == "SH600000"]
    assert events and all(e["status"] == "blocked" and e["block_reason"] == "outside_listing_lifecycle" for e in events)


def test_new_ipo_history_is_excluded_and_bad_snapshot_or_holes_are_rejected(tmp_path):
    root = tmp_path / "warehouse"
    manifest = warehouse_fixture(root)
    code = "688001.SH"
    asset = manifest["assets"][code]
    path = root / asset["file"]
    frame = pd.read_parquet(path)
    frame = frame.loc[frame.date.ge("2024-01-10")].copy()
    frame["history_sessions_before"] = np.arange(len(frame))
    frame.to_parquet(path, index=False)
    manifest["assets"][code].update(
        start="20240110", rows=len(frame), sha256=hashlib.sha256(path.read_bytes()).hexdigest()
    )
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    data, _, calendar, meta = load_historical_input(root, "2024-01-08", "2024-01-31", 2)
    result = market_factors(
        data,
        meta["id"],
        FactorConfig((FactorSpec("momentum", 1.0, 2),)),
        dynamic_context=meta,
        exchange_calendar=calendar,
    )
    assert not any(r["instrument"] == "SH688001" and r["date"] == "2024-01-11" for r in result["history"])
    assert any(r["instrument"] == "SH688001" and r["date"] == "2024-01-12" for r in result["history"])
    assert any(
        r["instrument"] == "SH688001"
        and r["date"] == "2024-01-11"
        and r["reasons"].get("_eligibility") == "insufficient_pre_history"
        for r in result["diagnostics"]
    )
    output = run_historical_research(request(), root, tmp_path / "ipo-runs")
    account = json.loads(
        (tmp_path / "ipo-runs/backtests" / output["backtest_run_id"] / "result.json").read_text(encoding="utf-8")
    )
    assert not any(t["side"] == "buy" and t["date"] == "2024-01-12" for t in account["trades"])
    assert any(e["date"] == "2024-01-12" and e["reason"] == "candidate_shortfall" for e in account["execution_events"])
    with pytest.raises(ValueError, match="版本已变化"):
        load_historical_input(root, "2024-01-08", "2024-01-31", 2, expected_sha="old")
    with pytest.raises(ValueError, match="预热"):
        load_historical_input(root, "2024-01-08", "2024-01-31", 126)
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="行情校验失败"):
        load_historical_input(root, "2024-01-08", "2024-01-31", 2)


def test_qlib_exchange_enforces_star_minimum_tplusone_and_odd_lot_full_exit(tmp_path):

    days = pd.bdate_range("2024-01-02", periods=3)
    quotes = pd.DataFrame(
        {
            "date": days,
            "instrument": "SH688001",
            "open": 10.0,
            "close": 10.0,
            "high": 10.0,
            "low": 10.0,
            "volume": 1e6,
            "factor": 1.0,
            "change": 0.0,
            "up_limit": 12.0,
            "down_limit": 8.0,
        }
    )
    write_features(quotes, days, tmp_path / "qlib")
    qlib.init(provider_uri=str(tmp_path / "qlib"), region="cn", kernels=1, expression_cache=None, dataset_cache=None)
    states = {
        ("SH688001", d): {
            "execution_available": True,
            "execution_state": "active",
            "has_daily_price_limit": True,
            "open": 10.0,
            "up_limit": 12.0,
            "down_limit": 8.0,
        }
        for d in days
    }
    exchange = ChinaAExchange(
        market_states=states,
        start_time=days[0],
        end_time=days[-1],
        codes=["SH688001"],
        deal_price="$open",
        limit_threshold=("$open < 0", "$open < 0"),
        open_cost=0.0,
        close_cost=0.0,
        min_cost=0.0,
        impact_cost=0.0,
    )
    position = Position(cash=10000.0)

    def order(amount, direction, day=days[0]):
        return Order(stock_id="SH688001", amount=amount, direction=direction, start_time=day, end_time=day)

    small = order(199, Order.BUY)
    assert exchange.deal_order(small, position=position)[0] == 0
    buy = order(201, Order.BUY)
    assert exchange.deal_order(buy, position=position)[0] > 0 and buy.deal_amount == 201
    sell = order(201, Order.SELL)
    assert exchange.deal_order(sell, position=position)[0] == 0
    position.add_count_all("day")
    sell = order(201, Order.SELL, days[1])
    assert exchange.deal_order(sell, position=position)[0] > 0
    odd = Position(cash=100.0, position_dict={"SH688001": {"amount": 17.0, "price": 10.0, "count_day": 1}})
    sell = order(17, Order.SELL, days[1])
    assert exchange.deal_order(sell, position=odd)[0] > 0 and sell.deal_amount == 17
