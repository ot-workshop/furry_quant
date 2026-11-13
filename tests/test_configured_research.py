import json
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from filelock import FileLock
from test_managed_data import Provider
from test_real_workflow import sample_market

from finance_quant.application.real_workflow import adjusted_quotes, run_configured
from finance_quant.configuration import BacktestConfig, DataConfig, ResearchConfig, load_config
from finance_quant.data.market import load_market
from finance_quant.integrations.qlib_runner import run_qlib
from finance_quant.research.registry import EvaluationConfig, FactorConfig, FactorSpec
from finance_quant.storage import ResultStore
from support.fixed_pool import download_dataset
from support.synthetic import sample_factors, sample_prices


def test_nondefault_benchmark_and_execution_config_reach_native_qlib(tmp_path):

    prices, benchmark = sample_market()
    benchmark["instrument"] = "SZ399006"
    factors = sample_factors(sample_prices())
    result = run_qlib(
        adjusted_quotes(prices, benchmark),
        factors,
        tmp_path / "qlib",
        calendar=pd.bdate_range("2025-01-02", "2025-01-31"),
        start="2025-01-07",
        end="2025-01-17",
        initial_date="2025-01-06",
        names=prices.drop_duplicates("instrument").set_index("instrument")["name"].to_dict(),
        benchmark="SZ399006",
        initial_cash=200000,
        topk=1,
        n_drop=0,
        risk_degree=0.5,
        open_cost=0.002,
        close_cost=0.003,
        min_cost=8,
    )
    config = result["metadata"]["config"]
    assert config["benchmark"] == "SZ399006"
    assert config["account"] == 200000
    assert config["n_drop"] == 0 and config["risk_degree"] == 0.5
    assert config["exchange_kwargs"]["open_cost"] == 0.002
    assert result["trades"] and result["equity"][-1]["cash"] >= 0
    assert all(trade["cost"] >= 8 for trade in result["trades"])


@pytest.fixture
def research_config(tmp_path):
    config = ResearchConfig(
        DataConfig(tmp_path / "data", "20241202", "20250228", ("600000.SH", "000001.SZ")),
        BacktestConfig("2025-01-02", "2025-02-28", topk=1),
        tmp_path / "runs",
    )
    download_dataset(config.data, provider=Provider())
    return config


def test_configured_run_rejects_snapshot_universe_or_dates_mismatch(research_config):
    for data in [
        replace(research_config.data, codes=("600519.SH", "000001.SZ")),
        replace(research_config.data, start="20241203"),
        replace(research_config.data, benchmark="399006.SZ"),
    ]:
        with pytest.raises(ValueError, match="snapshot differs"):
            run_configured(replace(research_config, data=data))


def test_changed_execution_parameters_create_independent_reproducible_runs(research_config):
    first = run_configured(research_config)
    repeated = run_configured(research_config)
    assert repeated["reused"] and repeated["run_id"] == first["run_id"]
    altered = replace(
        research_config, backtest=replace(research_config.backtest, initial_cash=150000, min_cost=10, risk_degree=0.5)
    )
    second = run_configured(altered)
    assert second["run_id"] != first["run_id"]
    before = json.loads(
        (research_config.runs_dir / "backtests" / first["run_id"] / "result.json").read_text(encoding="utf-8")
    )
    after = json.loads(
        (research_config.runs_dir / "backtests" / second["run_id"] / "result.json").read_text(encoding="utf-8")
    )
    assert before["metadata"]["provenance"]["initial_cash"] == 100000
    assert after["metadata"]["provenance"]["initial_cash"] == 150000
    assert after["metadata"]["config"]["risk_degree"] == 0.5
    assert second["factor_run_id"] == first["factor_run_id"]

    snapshot_dir = load_market(research_config.data.root)[3]["snapshot_dir"]

    assert not (Path(snapshot_dir) / "qlib").exists()
    assert (research_config.runs_dir / ".qlib").is_dir()


def test_scheduled_itemized_config_roundtrips_and_reaches_real_workflow(research_config):

    configured = replace(
        research_config,
        backtest=replace(
            research_config.backtest,
            rebalance_frequency="monthly",
            cost_mode="itemized",
            commission_rate=0.0002,
            tax_mode="fixed",
            stamp_tax_rate=0.0006,
            transfer_fee_rate=0.00003,
            impact_cost=0.0015,
        ),
    )
    path = research_config.runs_dir.parent / "itemized.json"
    path.write_text(json.dumps(configured.as_dict()), encoding="utf-8")
    assert load_config(path) == configured
    output = run_configured(configured)
    saved = ResultStore(configured.runs_dir).get("backtests", output["run_id"])
    assert saved["metadata"]["rebalance_policy"]["frequency"] == "monthly"
    assert saved["metadata"]["execution_costs"]["commission_rate"] == 0.0002
    assert saved["metadata"]["config"]["exchange_kwargs"]["impact_cost"] == 0.0015
    assert saved["trades"] and all(t["date"] in saved["metadata"]["rebalance_policy"]["dates"] for t in saved["trades"])
    assert saved["metadata"]["cost_summary"]["impact"] > 0


def test_factor_selection_reaches_native_strategy_and_evaluation_settings_do_not_change_execution(research_config):

    config = replace(research_config, factors=FactorConfig((FactorSpec("momentum", 1, 5),)))
    result = run_configured(config)
    store = ResultStore(config.runs_dir)
    factors = store.get("factors", result["factor_run_id"])
    backtest = store.get("backtests", result["run_id"])
    assert factors["metadata"]["weights"] == {"momentum": 1}
    assert factors["metadata"]["factor_config"][0]["window"] == 5
    assert all(set(row["scores"]) == {"momentum"} for row in factors["history"])
    assert backtest["metadata"]["config"]["factor_run_id"] == factors["metadata"]["id"]
    assert backtest["trades"]
    repeated = run_configured(replace(config, evaluation=EvaluationConfig(horizons=(3,))))
    assert repeated["reused"] and repeated["run_id"] == result["run_id"]


def test_missing_daily_scores_cannot_silently_reuse_old_signals(tmp_path):
    class MissingValuationProvider(Provider):
        def query(self, api, **params):
            frame = super().query(api, **params)
            if api == "daily_basic":
                frame.loc[frame.trade_date == "20250108", "pe_ttm"] = float("nan")
            return frame

    config = ResearchConfig(
        DataConfig(tmp_path / "data", "20241202", "20250228", ("600000.SH", "000001.SZ")),
        BacktestConfig("2025-01-02", "2025-02-28", topk=1),
        tmp_path / "runs",
    )
    download_dataset(config.data, provider=MissingValuationProvider())
    with pytest.raises(ValueError, match="lacks valid factor scores"):
        run_configured(config)


def test_identical_qlib_workspaces_are_locked_before_feature_writes(tmp_path):

    directory = tmp_path / "features"
    with FileLock(directory.with_name(directory.name + ".lock"), timeout=0):
        with pytest.raises(ValueError, match="already running"):
            run_qlib(None, None, directory)
    assert not directory.exists()
