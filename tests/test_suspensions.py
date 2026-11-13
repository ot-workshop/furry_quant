import numpy as np
import pandas as pd
import pytest
from test_joint_data import bundle, market_frame
from test_managed_data import Provider

from finance_quant.application.real_workflow import adjusted_quotes, run_configured
from finance_quant.configuration import BacktestConfig, DataConfig, ResearchConfig
from finance_quant.data.managed import DatasetDownloadError
from finance_quant.data.market import load_market
from finance_quant.data.reconciliation import normalize_baostock
from finance_quant.data.suspensions import carry_halt_rows, invalid_halt_rows
from finance_quant.data.validation_config import CrosscheckConfig
from finance_quant.integrations.qlib_runner import run_qlib, write_features
from finance_quant.research.evaluation import forward_open_labels
from finance_quant.research.registry import EvaluationConfig, FactorConfig, FactorSpec
from finance_quant.storage import ResultStore
from support.fixed_pool import download_dataset

HALTS = ["2025-01-13", "2025-01-14", "2025-01-15"]


def test_missing_pe_is_not_claimed_as_a_carried_value_or_invalid_price():

    frame = market_frame().set_index("date")
    frame["pe_ttm"] = np.nan
    halt = pd.DatetimeIndex(frame.index[1:3])
    frame.loc[halt, ["volume", "amount"]] = 0
    frame = carry_halt_rows(frame, halt)
    assert frame.loc[halt, "pe_ttm"].isna().all()
    assert not frame.loc[halt, "valuation_carried"].any()
    assert not invalid_halt_rows(frame.reset_index()).any()
    frame.loc[halt, "valuation_carried"] = True
    assert invalid_halt_rows(frame.reset_index()).sum() == len(halt)


class Primary(Provider):
    def query(self, api, **params):
        frame = super().query(api, **params)
        if api == "daily":
            frame["close"], frame["pct_chg"] = 10.0, 0.0
        if api == "daily_basic":
            frame["pe_ttm"] = 5.0 if params.get("ts_code") == "600000.SH" else 20.0
            if params.get("ts_code") == "600000.SH":
                frame.loc[frame.trade_date > "20250115", "pe_ttm"] = 100.0
        if api in {"daily", "daily_basic"} and params.get("ts_code") == "600000.SH":
            frame = frame.loc[~frame.trade_date.isin([date.replace("-", "") for date in HALTS])]
        return frame


class Secondary:
    def __init__(self, halted=True):
        self.halted, self.calls = halted, []

    def fetch(self, code, start, end):
        self.calls.append((code, start, end))
        result = bundle(market_frame(pd.bdate_range(start, end), code), code)
        if code == "600000.SH" and self.halted:
            for frame in (result.raw, result.adjusted):
                mask = frame.date.isin(HALTS)
                frame.loc[mask, "tradestatus"] = "0"
                frame.loc[mask, ["volume", "amount", "pctChg"]] = ""
        return result


@pytest.fixture
def snapshot(tmp_path):
    config = DataConfig(
        tmp_path / "data",
        "20241202",
        "20250228",
        ("600000.SH", "000001.SZ", "600519.SH"),
        validation=CrosscheckConfig("fallback"),
    )
    secondary = Secondary()
    download_dataset(config, provider=Primary(), secondary_provider=secondary)
    assert sum(code == "600000.SH" for code, _, _ in secondary.calls) == 1
    return config, load_market(config.root)


def test_confirmed_halts_carry_only_prior_known_values_and_keep_evidence(snapshot):
    config, (market, _, _, manifest) = snapshot
    halted = market.loc[market.is_suspended]
    assert halted.date.dt.strftime("%Y-%m-%d").tolist() == HALTS
    assert halted.volume.eq(0).all() and halted.amount.eq(0).all()
    assert halted.open.eq(10).all() and halted.close.eq(10).all()
    assert halted.pe_ttm.eq(5).all()  # not the post-resumption PE of 100
    assert halted.valuation_carried.all()
    assert halted.valuation_asof.eq("2025-01-10").all()
    stock = next(s for s in manifest["cross_validation"]["stocks"] if s["code"] == "600000.SH")
    assert stock["suspended_dates"] == HALTS and stock["secondary"]["source"] == "baostock"
    research = ResearchConfig(
        config,
        BacktestConfig("2025-01-02", "2025-02-28", topk=1),
        config.root.parent / "runs",
        FactorConfig((FactorSpec("value", 1),)),
        EvaluationConfig(horizons=(1, 3)),
    )
    output = run_configured(research)
    saved = ResultStore(research.runs_dir).get("backtests", output["run_id"])
    assert not any(row["date"] in HALTS and row["instrument"] == "SH600000" for row in saved["trades"])
    assert len(saved["equity"]) == 43


def test_unconfirmed_gaps_are_not_filled_or_published(tmp_path):
    data = DataConfig(
        tmp_path / "unknown",
        "20241202",
        "20250228",
        ("600000.SH", "000001.SZ", "600519.SH"),
        validation=CrosscheckConfig("fallback"),
    )
    with pytest.raises(DatasetDownloadError):
        download_dataset(data, provider=Primary(), secondary_provider=Secondary(halted=False))
    assert not (data.root / "manifest.json").exists()


@pytest.mark.parametrize("mutation", ["different_basis_status", "nonzero_volume", "invalid_activity", "leading_halt"])
def test_inconsistent_or_unanchored_halt_evidence_is_rejected(mutation):
    secondary = Secondary().fetch("600000.SH", "20250102", "20250131")
    if mutation == "different_basis_status":
        secondary.adjusted.loc[secondary.adjusted.date == HALTS[0], "tradestatus"] = "1"
    elif mutation == "nonzero_volume":
        secondary.raw.loc[secondary.raw.date == HALTS[0], "volume"] = "100"
    elif mutation == "invalid_activity":
        secondary.raw.loc[secondary.raw.date == HALTS[0], "volume"] = "not-a-number"
    else:
        for frame in (secondary.raw, secondary.adjusted):
            frame.loc[0, "tradestatus"] = "0"
            frame.loc[0, ["volume", "amount", "pctChg"]] = ""
    with pytest.raises(ValueError):
        normalize_baostock(secondary, "600000.SH", pd.bdate_range("2025-01-02", "2025-01-31"))


@pytest.mark.parametrize("sell_held", [True, False])
def test_native_exchange_blocks_both_selling_and_buying_during_halt(snapshot, tmp_path, sell_held):
    _, (market, benchmark, calendar, _) = snapshot
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
    history = []
    for date in sessions[sessions <= pd.Timestamp("2025-02-28")]:
        target_first = (date < pd.Timestamp("2025-01-10")) == sell_held
        for instrument in ["SH600000", "SZ000001", "SH600519"]:
            score = 100 if instrument == ("SH600000" if target_first else "SZ000001") else 10
            history.append({"date": date.strftime("%Y-%m-%d"), "instrument": instrument, "composite": score})
    factors = {"metadata": {"id": "test-halt-signals"}, "history": history}
    names = market.drop_duplicates("instrument").set_index("instrument")["name"].to_dict()
    result = run_qlib(
        adjusted_quotes(market, benchmark),
        factors,
        tmp_path / ("sell" if sell_held else "buy"),
        calendar=sessions,
        start="2025-01-02",
        end="2025-01-20",
        initial_date="2025-01-01",
        names=names,
        topk=1,
    )
    trades = [r for r in result["trades"] if r["instrument"] == "SH600000"]
    assert trades and not any(r["date"] in HALTS for r in trades)
    if sell_held:
        assert trades[0]["side"] == "buy" and trades[0]["date"] < HALTS[0]
        assert any(r["side"] == "sell" and r["date"] >= "2025-01-16" for r in trades)
    else:
        assert all(r["date"] >= "2025-01-16" for r in trades)


def test_factor_labels_do_not_enter_or_exit_on_nontrading_prices(snapshot):
    _, (market, _, calendar, _) = snapshot
    sessions = pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])
    labels = forward_open_labels(market, sessions, 1, "2025-02-28")
    one = labels.loc[labels.instrument.eq("SH600000")].set_index("date")
    assert one.loc["2025-01-09", "exclusion"] == "non_trading_entry_or_exit"
    assert one.loc["2025-01-10", "exclusion"] == "non_trading_entry_or_exit"
    assert pd.isna(one.loc["2025-01-10", "forward_return"])
    longer = forward_open_labels(market, sessions, 4, "2025-02-28")
    observation = longer.loc[longer.instrument.eq("SH600000") & longer.date.eq("2025-01-09")].iloc[0]
    assert observation.exit_date == "2025-01-16" and observation.exclusion is None


def test_unauthorized_zero_volume_is_still_rejected(snapshot, tmp_path):
    _, (market, benchmark, calendar, _) = snapshot
    quotes = adjusted_quotes(market.drop(columns="is_suspended"), benchmark)
    with pytest.raises(ValueError, match="Nonpositive"):
        write_features(quotes, pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"]), tmp_path / "bad")


@pytest.mark.parametrize("missing_api", ["adj_factor", "stk_limit"])
def test_halt_is_detected_even_if_only_auxiliary_field_is_missing(tmp_path, missing_api):
    class AuxiliaryOnly(Primary):
        def query(self, api, **params):
            if api in {"daily", "daily_basic"}:
                frame = Provider.query(self, api, **params)
                if api == "daily":
                    frame["close"], frame["pct_chg"] = 10.0, 0.0
                    frame.loc[frame.trade_date.isin([d.replace("-", "") for d in HALTS]), ["vol", "amount"]] = 0.0
            else:
                frame = super().query(api, **params)
            if api == missing_api and params.get("ts_code") == "600000.SH":
                frame = frame.loc[~frame.trade_date.isin([d.replace("-", "") for d in HALTS])]
            return frame

    config = DataConfig(
        tmp_path / "data", "20241202", "20250228", ("600000.SH", "000001.SZ"), validation=CrosscheckConfig("fallback")
    )
    provider = AuxiliaryOnly()
    download_dataset(config, provider=provider, secondary_provider=Secondary())
    market = load_market(config.root)[0]
    assert market.loc[market.is_suspended, "date"].dt.strftime("%Y-%m-%d").tolist() == HALTS
    assert not any(
        dict(params).get("trade_date") in [d.replace("-", "") for d in HALTS] for _, params in provider.calls
    )


def test_a_wholly_suspended_year_partition_can_use_prior_year_prices(tmp_path):
    class EmptyYear(Primary):
        def query(self, api, **params):
            frame = super().query(api, **params)
            if api in {"daily", "daily_basic", "adj_factor", "stk_limit"} and params.get("ts_code") == "600000.SH":
                frame = frame.loc[frame.trade_date < "20250101"]
            return frame

    class SuspendedYear(Secondary):
        def fetch(self, code, start, end):
            result = super().fetch(code, start, end)
            if code == "600000.SH":
                for frame in (result.raw, result.adjusted):
                    mask = frame.date >= "2025-01-01"
                    frame.loc[mask, "tradestatus"] = "0"
                    frame.loc[mask, ["volume", "amount", "pctChg"]] = ""
            return result

    config = DataConfig(
        tmp_path / "data", "20241202", "20250131", ("600000.SH", "000001.SZ"), validation=CrosscheckConfig("fallback")
    )
    download_dataset(config, provider=EmptyYear(), secondary_provider=SuspendedYear())
    market = load_market(config.root)[0]
    halted = market.loc[market.is_suspended]
    assert len(halted) == 23 and halted.close.eq(10).all()
    assert halted.valuation_asof.eq("2024-12-31").all()
