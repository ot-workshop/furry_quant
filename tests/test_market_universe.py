"""Dynamic market eligibility, historical state, and the complete web task flow."""

from dataclasses import replace

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from test_fundamental_factors import statements
from test_industry_neutralization import history as industry_history
from test_joint_data import Secondary
from test_pool_workflow_matrix import DiversePrimary, DiverseSecondary, codes_for
from test_shared_market import spec
from test_shared_market import workspace as workspace

from finance_quant.application import market_download as downloads
from finance_quant.application.research_requests import research_options, validate_request
from finance_quant.data.financials import financial_root
from finance_quant.data.industries import publish_industries
from finance_quant.data.market_universe import load_market_input, market_option, market_roots
from finance_quant.data.security_history import name_states
from finance_quant.research.universe import UniverseFilters, select_universe
from finance_quant.storage import ResultStore
from support.fakes import make_app, make_service, run_claimed
from support.financial import publish as publish_financials


def sample(size=5):
    dates = pd.bdate_range("2025-01-02", periods=40)
    symbols = ["SH600000", "SZ000001", "SH688001", "SZ300001"] + [f"SH60{i:04d}" for i in range(10, 10 + size - 4)]
    market = pd.DataFrame(
        [
            {
                "instrument": symbol,
                "date": day,
                "list_date": pd.Timestamp("2010-01-01"),
                "delist_date": pd.NaT,
                "total_mv": (index + 1) * 1e9,
                "amount": (index + 1) * 1e6,
            }
            for index, symbol in enumerate(symbols)
            for day in dates
        ]
    )
    names = pd.DataFrame(
        [
            {
                "instrument": symbol,
                "name": "正常股票",
                "start_date": pd.Timestamp("2010-01-01"),
                "end_date": pd.NaT,
                "ann_date": pd.Timestamp("2010-01-01"),
            }
            for symbol in symbols
        ]
    )
    return market, dates, names


@pytest.mark.parametrize("size", [5, 17, 55])
def test_filters_use_each_signal_dates_size_liquidity_and_board(size):
    market, dates, names = sample(size)
    filters = UniverseFilters(
        exclude_st=True,
        min_listing_days=0,
        min_market_cap=2e9,
        max_market_cap=3e9,
        min_avg_amount=1.5e6,
        amount_window=5,
    )
    selected, metadata = select_universe(market, dates, dates[6], dates[-1], filters, names)
    assert set(metadata["membership_by_execution_date"][str(dates[6].date())]) == {"SZ000001", "SH688001"}
    main = replace(filters, boards=("main",))
    _, narrower = select_universe(market, dates, dates[6], dates[-1], main, names)
    assert set(narrower["membership_by_execution_date"][str(dates[6].date())]) == {"SZ000001"}
    changed = market.copy()
    changed.loc[changed.date > dates[20], ["total_mv", "amount"]] *= 100
    _, future = select_universe(changed, dates, dates[6], dates[-1], filters, names)
    for day, members in metadata["membership_by_execution_date"].items():
        if day <= str(dates[21].date()):
            assert future["membership_by_execution_date"][day] == members
    assert selected.selection_member.any()
    _, longer = select_universe(market, dates, dates[6], dates[-1], replace(filters, amount_window=20), names)
    assert longer["declared_universe"][str(dates[5].date())] == 0
    assert metadata["declared_universe"][str(dates[5].date())] == 2


def test_listing_retirement_and_st_do_not_backfill_future_states():
    market, dates, names = sample()
    late = market.instrument.eq("SZ000001")
    market.loc[late, "list_date"] = dates[15]
    retired = market.instrument.eq("SH688001")
    market.loc[retired, "delist_date"] = dates[25]
    names = pd.concat(
        [
            names.loc[~names.instrument.eq("SH600000")],
            pd.DataFrame(
                [
                    {
                        "instrument": "SH600000",
                        "name": "正常股票",
                        "start_date": pd.Timestamp("2010-01-01"),
                        "end_date": dates[19],
                        "ann_date": pd.NaT,
                    },
                    {
                        "instrument": "SH600000",
                        "name": "*ST股票",
                        "start_date": dates[20],
                        "end_date": pd.NaT,
                        "ann_date": dates[19],
                    },
                ]
            ),
        ],
        ignore_index=True,
    )
    filters = UniverseFilters(min_listing_days=0)
    _, pool = select_universe(market, dates, dates[5], dates[-1], filters, names)
    membership = pool["membership_by_execution_date"]
    assert "SZ000001" not in membership[str(dates[15].date())]
    assert "SZ000001" in membership[str(dates[16].date())]
    assert "SH688001" in membership[str(dates[25].date())]  # No advance exit using tomorrow's retirement.
    assert "SH688001" not in membership[str(dates[26].date())]
    assert "SH600000" in membership[str(dates[19].date())]
    assert "SH600000" not in membership[str(dates[22].date())]
    _, allowed = select_universe(market, dates, dates[5], dates[-1], replace(filters, exclude_st=False), names)
    assert "SH600000" in allowed["membership_by_execution_date"][str(dates[22].date())]
    missing = names.loc[~names.instrument.eq("SZ300001")]
    _, unknown = select_universe(market, dates, dates[5], dates[-1], filters, missing)
    assert "SZ300001" not in unknown["membership_by_execution_date"][str(dates[10].date())]
    assert unknown["pool_coverage_by_date"][str(dates[9].date())]["excluded"]["st_unknown"] == 1


def test_unknown_or_late_announcements_are_not_normal_stock_evidence():
    dates = pd.bdate_range("2025-01-02", periods=5)
    history = pd.DataFrame([{"name": "ST股票", "start_date": dates[0], "end_date": pd.NaT, "ann_date": dates[2]}])
    states = name_states(dates, history)
    assert states.iloc[:3].isna().all() and states.iloc[3:].all()


def test_future_name_end_dates_and_late_old_corrections_do_not_rewrite_known_state():
    dates = pd.bdate_range("2025-01-02", periods=10)
    history = pd.DataFrame(
        [
            {
                "name": "正常股票",
                "start_date": pd.Timestamp("2010-01-01"),
                "end_date": dates[2],
                "ann_date": pd.Timestamp("2010-01-01"),
            },
            {"name": "ST股票", "start_date": dates[3], "end_date": pd.NaT, "ann_date": dates[7]},
        ]
    )
    states = name_states(dates, history)
    assert not states.iloc[:8].any() and not states.iloc[:8].isna().any()
    assert states.iloc[8:].all()
    correction = pd.DataFrame(
        [
            {
                "name": "ST旧名称",
                "start_date": pd.Timestamp("2009-01-01"),
                "end_date": pd.Timestamp("2009-12-31"),
                "ann_date": dates[5],
            }
        ]
    )
    pd.testing.assert_series_equal(name_states(dates, pd.concat([history, correction], ignore_index=True)), states)


@pytest.mark.parametrize("field", ["total_mv_asof", "total_mv_available_at"])
def test_market_cap_filter_rejects_values_not_available_at_the_signal(field):
    market, dates, names = sample()
    market[field] = market.date + pd.Timedelta(days=1)
    if field.endswith("available_at"):
        market[field] = market[field].dt.tz_localize("Asia/Shanghai")
    with pytest.raises(ValueError):
        select_universe(market, dates, dates[5], dates[-1], UniverseFilters(min_market_cap=1e9), names)


def test_simultaneous_name_publications_choose_latest_effective_start_and_preserve_true_conflicts():
    dates = pd.bdate_range("2025-01-02", periods=5)
    history = pd.DataFrame(
        [
            {
                "name": "ST旧名称",
                "start_date": pd.Timestamp("2009-01-01"),
                "end_date": pd.Timestamp("2009-12-31"),
                "ann_date": dates[0],
            },
            {"name": "正常股票", "start_date": pd.Timestamp("2010-01-01"), "end_date": pd.NaT, "ann_date": dates[0]},
        ]
    )
    state = name_states(dates, history)
    assert pd.isna(state.iloc[0]) and state.iloc[1:].eq(False).all()
    history.loc[0, "start_date"] = history.loc[1, "start_date"]
    history.loc[0, "end_date"] = pd.NaT
    assert name_states(dates, history).isna().all()


@pytest.mark.parametrize("period", ["early", "late"])
def test_empty_pool_days_hold_cash_and_filter_exits_run_outside_weekly_schedule(workspace, monkeypatch, period):
    data, root, _, provider = workspace
    query = provider.query

    def historical_names(api, **params):
        if api != "namechange":
            return query(api, **params)
        base = {
            "ts_code": params["ts_code"],
            "name": "正常股票",
            "start_date": "20100101",
            "end_date": None,
            "ann_date": "20100101",
        }
        if period == "early":
            return pd.DataFrame([{**base, "ann_date": "20250203"}])
        return pd.DataFrame([base, {**base, "name": "*ST股票", "start_date": "20250204", "ann_date": "20250203"}])

    monkeypatch.setattr(provider, "query", historical_names)
    plan = downloads.prepare(root, spec("all_market", codes=[]), provider=provider)
    assert downloads.run(root, plan["id"], provider=provider, secondary=Secondary())["status"] == "complete"
    option = market_option(market_roots(data)[0])
    request = {
        "name": "动态空池交易",
        "data": {"mode": "market_warehouse", "warehouse_id": option["id"], "filters": {"min_listing_days": 0}},
        "backtest": {"start": "2025-01-10", "end": "2025-02-28", "topk": 1, "rebalance_frequency": "weekly"},
        "factors": [{"name": "momentum", "window": 2, "weight": 1}],
        "evaluation": {"horizons": [1], "groups": 2, "min_cross_section": 3},
    }
    service = make_service(data.parent / "runs", data)
    task = service.submit(request, "empty-pool-weekly-" + period)
    assert service.store.claim() == task["id"]
    done = run_claimed(service, task["id"])
    assert done["status"] == "succeeded", done
    result = ResultStore(data.parent / "runs").get("backtests", done["result"]["backtest_run_id"])
    assert result["trades"]
    if period == "early":
        assert all(trade["date"] >= "2025-02-05" for trade in result["trades"])
    else:
        exits = [
            event
            for event in result["execution_events"]
            if event["action"] == "sell" and event["reason"] == "pool_exit"
        ]
        assert exits and exits[0]["date"] == "2025-02-06"


@pytest.mark.parametrize("frequency", ["daily", "weekly", "monthly"])
def test_download_reuse_web_submit_results_and_frozen_parameters(workspace, frequency):
    data, root, _, provider = workspace
    plan = downloads.prepare(root, spec("all_market", "2025-03-31", codes=[]), provider=provider)
    result = downloads.run(root, plan["id"], provider=provider, secondary=Secondary())
    assert result["status"] == "complete", result
    [view] = market_roots(data)
    option = market_option(view)
    assert option["full_market"] and option["codes_count"] == 3
    client = TestClient(make_app(data.parent / "runs", data_root=data), base_url="http://127.0.0.1")
    assert client.get("/api/v1/research/options").json()["market_universes"][0]["id"] == option["id"]
    request = {
        "name": "动态条件研究",
        "data": {"mode": "market_warehouse", "warehouse_id": option["id"], "filters": {"min_listing_days": 0}},
        "backtest": {"start": "2025-01-10", "end": "2025-03-31", "topk": 1, "rebalance_frequency": frequency},
        "factors": [{"name": "momentum", "window": 2, "weight": 1.0}],
        "evaluation": {"horizons": [1], "groups": 2, "min_cross_section": 3},
    }
    service = make_service(data.parent / "runs", data)
    client = TestClient(make_app(data.parent / "runs", data_root=data, service=service), base_url="http://127.0.0.1")
    submitted = client.post(
        "/api/v1/research/tasks", json=request, headers={"Idempotency-Key": "market-flow-" + frequency}
    )
    assert submitted.status_code == 202, submitted.text
    task = submitted.json()
    assert service.store.claim() == task["id"]
    task = run_claimed(service, task["id"])
    assert task["status"] == "succeeded", task
    detail = client.get("/api/v1/backtests/" + task["result"]["backtest_run_id"]).json()
    assert detail["strategy"] == "DynamicUniverseTopkStrategy"
    assert detail["universe_filters"]["exclude_st"] and detail["universe_filters"]["min_listing_days"] == 0
    assert detail["pool_coverage_by_date"]
    factors = ResultStore(data.parent / "runs").get("factors", task["result"]["factor_run_id"])
    retired_rows = [row for row in factors["history"] if row["instrument"] == "SH600519"]
    assert retired_rows and all(row["date"] < "2025-03-20" for row in retired_rows)
    before = len(provider.calls)
    assert downloads.run(root, plan["id"], provider=provider, secondary=Secondary())["status"] == "complete"
    assert len(provider.calls) == before
    _, _, _, pinned = load_market_input(
        view, "2025-01-10", "2025-03-31", 2, filters=UniverseFilters(min_listing_days=0)
    )
    assert pinned["membership_by_execution_date"]


def test_full_workflow_above_fixed_pool_limit_with_generated_stocks(tmp_path):
    codes = codes_for(55, 124)

    class Provider(DiversePrimary):
        def query(self, api, **params):
            if api == "stock_basic":
                selected = codes if params.get("list_status") == "L" else ()
                return pd.DataFrame(
                    [
                        {
                            "ts_code": code,
                            "name": code,
                            "industry": "fixture",
                            "list_date": "20100101",
                            "delist_date": None,
                            "list_status": "L",
                        }
                        for code in selected
                    ],
                    columns=["ts_code", "name", "industry", "list_date", "delist_date", "list_status"],
                )
            if api == "namechange":
                return pd.DataFrame(
                    [
                        {
                            "ts_code": params["ts_code"],
                            "name": "测试股票",
                            "start_date": "20100101",
                            "end_date": None,
                            "ann_date": "20100101",
                        }
                    ]
                )
            return super().query(api, **params)

    data, runs = tmp_path / "data", tmp_path / "runs"
    root = downloads.shared_root(data)
    provider = Provider()
    plan = downloads.prepare(root, spec("all_market", codes=[]), provider=provider)
    assert downloads.run(root, plan["id"], provider=provider, secondary=DiverseSecondary())["status"] == "complete"
    option = market_option(market_roots(data)[0])
    assert option["codes_count"] == 55
    defaults = research_options(data, runs)["defaults"]
    assert defaults["data"]["mode"] == "market_warehouse"
    validate_request(defaults, data, runs)
    request = {
        "name": "全市场55只测试",
        "data": {"mode": "market_warehouse", "warehouse_id": option["id"], "filters": {"min_listing_days": 0}},
        "backtest": {"start": "2025-01-10", "end": "2025-02-28", "topk": 3},
        "factors": [{"name": "momentum", "window": 2, "weight": 1}],
        "evaluation": {"horizons": [1], "groups": 3, "min_cross_section": 3},
    }
    service = make_service(runs, data)
    task = service.submit(request, "large-market-universe-55")
    assert service.store.claim() == task["id"]
    done = run_claimed(service, task["id"])
    assert done["status"] == "succeeded", done
    factors = ResultStore(runs).get("factors", done["result"]["factor_run_id"])
    assert len({row["instrument"] for row in factors["history"]}) == 55
    assert all(count["selected"] == 55 for count in factors["metadata"]["pool_coverage_by_date"].values())


def test_financial_and_industry_versions_are_reused_and_pinned(workspace):
    data, root, _, provider = workspace
    plan = downloads.prepare(root, spec("all_market", "2025-03-31", codes=[]), provider=provider)
    assert downloads.run(root, plan["id"], provider=provider, secondary=Secondary())["status"] == "complete"
    option = market_option(market_roots(data)[0])
    codes = option["codes"]
    publish_financials(financial_root(data), statements(codes))
    symbols = [code[-2:] + code[:6] for code in codes]
    publish_industries(industry_history(symbols).frame)
    request = {
        "name": "财务行业筛选",
        "data": {
            "mode": "market_warehouse",
            "warehouse_id": option["id"],
            "filters": {"min_listing_days": 0, "industries": ["I0"]},
        },
        "backtest": {"start": "2025-01-10", "end": "2025-03-31", "topk": 1},
        "factors": [{"name": "roe_ttm", "weight": 1}],
        "evaluation": {"horizons": [1], "groups": 2, "min_cross_section": 3},
    }
    service = make_service(data.parent / "runs", data)
    task = service.submit(request, "market-financial-industry-version")
    pinned = task["request"]["data"]["filters"]["industry_version"]
    changed = industry_history(symbols).frame
    changed["industry_code"] = "DIFFERENT"
    publish_industries(changed)
    assert service.store.claim() == task["id"]
    task = run_claimed(service, task["id"])
    assert task["status"] == "succeeded", task
    factors = ResultStore(data.parent / "runs").get("factors", task["result"]["factor_run_id"])
    assert factors["metadata"]["universe_filters"]["industry_version"] == pinned
    assert factors["metadata"]["industry_data"]["id"] == pinned
    assert factors["metadata"]["financial_data"]["version"] == task["request"]["financials"]["version"]
    assert {row["instrument"] for row in factors["history"]} == set(symbols[:2])
