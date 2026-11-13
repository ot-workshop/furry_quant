import hashlib
import json

import numpy as np
import pandas as pd
import pytest
from test_managed_data import Provider

from finance_quant.application.evaluation_workflow import evaluate_configured
from finance_quant.configuration import BacktestConfig, DataConfig, ResearchConfig, load_config
from finance_quant.data.managed import DatasetDownloadError
from finance_quant.data.market import load_market
from finance_quant.data.providers.baostock import BaoStockBundle, BaoStockProviderError
from finance_quant.data.reconciliation import normalize_baostock, reconcile_market
from finance_quant.data.validation_config import CrosscheckConfig
from finance_quant.storage import ResultStore
from support.fixed_pool import download_dataset


def market_frame(dates=None, code="600000.SH"):
    dates = pd.DatetimeIndex(dates if dates is not None else pd.bdate_range("2025-01-02", periods=5))
    return pd.DataFrame(
        {
            "date": dates,
            "instrument": code[-2:] + code[:6],
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "close": 10.0,
            "pre_close": 10.0,
            "volume": 1_000_000.0,
            "amount": 1_000_000.0,
            "change": 0.0,
            "adj_factor": 50.0,
            "pe_ttm": 10.0,
            "pb": 1.0,
            "total_mv": 10_000_000.0,
            "up_limit": 11.0,
            "down_limit": 9.0,
            "name": "fixture",
            "industry": "fixture",
        }
    )


def bundle(frame, code="600000.SH"):
    raw = frame.rename(columns={"pre_close": "preclose", "change": "pctChg"}).copy()
    raw["date"] = raw.date.dt.strftime("%Y-%m-%d")
    raw["code"], raw["adjustflag"], raw["tradestatus"], raw["isST"] = code[-2:].lower() + "." + code[:6], "3", "1", "0"
    raw["pctChg"] *= 100
    fields = [
        "date",
        "code",
        "open",
        "high",
        "low",
        "close",
        "preclose",
        "volume",
        "amount",
        "adjustflag",
        "pctChg",
        "tradestatus",
        "isST",
    ]
    raw = raw[fields]
    adjusted = raw.copy()
    for field in ["open", "high", "low", "close", "preclose"]:
        adjusted[field] *= frame.adj_factor.to_numpy() * 7
    adjusted["adjustflag"] = "1"
    return BaoStockBundle(
        raw.astype(str),
        adjusted.astype(str),
        {"source": "baostock", "files": {"raw.parquet": "hash", "adjusted.parquet": "hash"}},
    )


class Secondary:
    def __init__(self, frames=None, error=False):
        self.frames = frames or {}
        self.calls = []
        self.error = error

    def fetch(self, code, start, end):
        self.calls.append((code, start, end))
        if self.error:
            raise BaoStockProviderError("secret-error-body")
        frame = self.frames.get(code)
        return bundle(frame if frame is not None else market_frame(pd.bdate_range(start, end), code), code)


def test_different_absolute_adjustment_bases_are_not_treated_as_errors():
    market = market_frame()
    result, report = reconcile_market(market, market.date, ("600000.SH",), "fallback", Secondary())
    assert report["status"] == "verified" and report["fallback_codes"] == []
    assert result.adj_factor.eq(50).all() and result.adjustment_source.eq("tushare").all()


def test_mixed_primary_factors_switch_the_entire_instrument_not_single_dates():
    market = market_frame()
    secondary = Secondary({"600000.SH": market.copy()})
    market.loc[2, "adj_factor"] = 1
    before = market.copy(deep=True)
    result, report = reconcile_market(market, market.date, ("600000.SH",), "fallback", secondary)
    assert report["status"] == "fallback" and report["fallback_codes"] == ["600000.SH"]
    assert result.adj_factor.eq(1).all()
    assert result.price_source.eq("baostock").all() and result.adjustment_source.eq("baostock").all()
    assert result.valuation_source.eq("tushare").all() and result.limit_source.eq("tushare").all()
    assert result.pe_ttm.equals(before.pe_ttm)
    pd.testing.assert_frame_equal(market, before)
    json.dumps(report, allow_nan=False)


def test_verify_only_preserves_bad_candidate_and_reports_failure():
    market = market_frame()
    source = market.copy()
    market.loc[2, "adj_factor"] = 1
    result, report = reconcile_market(market, market.date, ("600000.SH",), "verify", Secondary({"600000.SH": source}))
    pd.testing.assert_frame_equal(result, market)
    assert report["status"] == "conflict" and report["stocks"][0]["reason"] == "primary_failed_verify_only"


def test_two_plausible_raw_prices_disagree_no_automatic_source_vote():
    market = market_frame()
    source = market.copy()
    market.loc[2, "close"] = 10.02
    market.loc[2, "adj_factor"] = 1
    result, report = reconcile_market(market, market.date, ("600000.SH",), "fallback", Secondary({"600000.SH": source}))
    assert report["status"] == "conflict" and report["stocks"][0]["raw_price_differences"] == ["2025-01-06"]
    assert report["stocks"][0]["reason"] == "raw_price_conflict"
    pd.testing.assert_frame_equal(result, market)


def test_missing_primary_prices_or_factors_are_filled_from_full_secondary_interval():
    market = market_frame()
    source = market.copy()
    market.loc[2, ["open", "high", "low", "close", "adj_factor"]] = np.nan
    result, report = reconcile_market(market, market.date, ("600000.SH",), "fallback", Secondary({"600000.SH": source}))
    assert report["status"] == "fallback" and result.close.eq(10).all() and result.adj_factor.eq(1).all()


@pytest.mark.parametrize(
    "case", ["missing_date", "wrong_code", "wrong_flag", "suspended", "bad_adjusted_price", "broken_return"]
)
def test_secondary_cannot_be_trusted_when_its_own_checks_fail(case):
    market = market_frame()
    item = bundle(market)
    raw, adjusted = item.raw.copy(), item.adjusted.copy()
    if case == "missing_date":
        raw = raw.iloc[1:]
    elif case == "wrong_code":
        raw.loc[0, "code"] = "sz.000001"
    elif case == "wrong_flag":
        raw.loc[0, "adjustflag"] = "1"
    elif case == "suspended":
        raw.loc[0, "tradestatus"] = "0"
    elif case == "bad_adjusted_price":
        adjusted.loc[0, "high"] = "99999"
    else:
        for field in ["open", "high", "low", "close", "preclose"]:
            adjusted.loc[2, field] = str(float(adjusted.loc[2, field]) * 1.2)

    class BadSecondary:
        def fetch(self, *args):
            return BaoStockBundle(raw, adjusted, item.metadata)

    market.loc[2, "adj_factor"] = 1
    result, report = reconcile_market(market, market.date, ("600000.SH",), "fallback", BadSecondary())
    assert report["status"] == "conflict"
    pd.testing.assert_frame_equal(result, market)


def test_secondary_failure_is_sanitized_and_cannot_publish_a_success():
    market = market_frame()
    _, report = reconcile_market(market, market.date, ("600000.SH",), "fallback", Secondary(error=True))
    assert report["status"] == "conflict" and "secret" not in json.dumps(report)


def test_legitimate_split_preserves_adjusted_returns_and_first_anchor_is_causal():
    market = market_frame()
    market.loc[market.index >= 2, "adj_factor"] *= 2
    for field in ["open", "high", "low", "close", "pre_close"]:
        market.loc[market.index >= 2, field] /= 2
    item = bundle(market)
    normalized = normalize_baostock(item, "600000.SH", pd.DatetimeIndex(market.date))
    assert normalized.adj_factor.tolist() == [1, 1, 2, 2, 2]
    assert (normalized.close * normalized.adj_factor).pct_change(fill_method=None).dropna().eq(0).all()
    truncated = BaoStockBundle(item.raw.iloc[:3], item.adjusted.iloc[:3], item.metadata)
    prefix = normalize_baostock(truncated, "600000.SH", pd.DatetimeIndex(market.date[:3]))
    assert normalized.adj_factor.iloc[:3].tolist() == prefix.adj_factor.tolist()


class Primary(Provider):
    def __init__(self):
        super().__init__()
        self.corrupt = False
        self.fail_api = None

    def query(self, api, **params):
        if api == self.fail_api:
            raise RuntimeError("private-primary-error")
        frame = super().query(api, **params)
        if api in {"daily", "index_daily"}:
            frame["close"], frame["pct_chg"] = 10.0, 0.0
        if self.corrupt and api == "adj_factor":
            frame.loc[frame.trade_date == "20250108", "adj_factor"] = 0.01
        return frame


@pytest.fixture
def config(tmp_path):
    return DataConfig(
        tmp_path / "joint", "20241202", "20250228", ("600000.SH", "000001.SZ"), validation=CrosscheckConfig("fallback")
    )


def test_joint_snapshot_success_source_trace_replay_and_failure_preserves_prior_pointer(config):
    primary = Primary()
    primary.corrupt = True
    secondary = Secondary()
    first = download_dataset(config, provider=primary, secondary_provider=secondary)
    manifest = first["manifest"]
    assert manifest["schema_version"] == "market-v3" and manifest["id"].startswith("joint-")
    assert manifest["cross_validation"]["status"] == "fallback"
    market, _, _, loaded = load_market(config.root)
    assert market.adjustment_source.eq("baostock").all() and loaded["coverage"]["status"] == "complete"
    calls = len(primary.calls)
    second = download_dataset(config, provider=primary, secondary_provider=secondary)
    assert second["manifest"]["id"] == manifest["id"] and len(primary.calls) == calls
    previous = (config.root / "manifest.json").read_bytes()
    with pytest.raises(DatasetDownloadError, match="quality checks"):
        download_dataset(config, provider=primary, secondary_provider=Secondary(error=True))
    assert (config.root / "manifest.json").read_bytes() == previous


@pytest.mark.parametrize("failed_api", ["daily", "adj_factor"])
def test_primary_transport_failure_for_supported_fields_can_fallback(config, failed_api):
    primary = Primary()
    primary.fail_api = failed_api
    result = download_dataset(config, provider=primary, secondary_provider=Secondary())
    report = result["manifest"]["cross_validation"]
    assert report["status"] == "fallback" and report["primary_failures"]
    assert "private-primary" not in json.dumps(result)


def test_valuation_cannot_be_silently_replaced_by_secondary(config):
    primary = Primary()
    primary.fail_api = "daily_basic"
    with pytest.raises(DatasetDownloadError):
        download_dataset(config, provider=primary, secondary_provider=Secondary())
    assert not (config.root / "manifest.json").exists()


def test_validation_configuration_roundtrips_and_rejects_ambiguous_modes(tmp_path):
    document = {
        "data": {
            "root": "data",
            "start": "20250101",
            "end": "20250331",
            "codes": ["600000.SH", "000001.SZ"],
            "validation": {"mode": "fallback"},
        },
        "backtest": {"start": "20250203", "end": "20250331"},
    }
    path = tmp_path / "research.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    config = load_config(path)
    assert config.data.validation.mode == "fallback" and config.as_dict()["data"]["validation"] == {"mode": "fallback"}
    for invalid in ["auto", True, {"mode": "auto"}, {"mode": "fallback", "token": "never-print-this"}]:
        document["data"]["validation"] = invalid
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(ValueError) as exc:
            load_config(path)
        assert "never-print" not in str(exc.value)


def test_missing_primary_daily_rows_use_secondary_without_manufacturing_prices(config):
    primary = Primary()
    primary.missing = ("600000.SH", "20250108")
    result = download_dataset(config, provider=primary, secondary_provider=Secondary())
    assert result["manifest"]["cross_validation"]["fallback_codes"] == ["600000.SH"]
    market = load_market(config.root)[0]
    assert market.loc[market.instrument == "SH600000", "price_source"].eq("baostock").all()
    assert market.loc[market.instrument == "SZ000001", "price_source"].eq("tushare").all()


@pytest.mark.parametrize("tamper", ["status", "row_source"])
def test_loading_independently_rejects_inconsistent_joint_evidence(config, tamper):

    manifest = download_dataset(config, provider=Primary(), secondary_provider=Secondary())["manifest"]
    leaf = config.root / manifest["snapshot_path"]
    if tamper == "status":
        manifest["cross_validation"]["status"] = "conflict"
    else:
        frame = pd.read_parquet(leaf / "market.parquet")
        frame.loc[0, "adjustment_source"] = "baostock"
        frame.to_parquet(leaf / "market.parquet", index=False)
        manifest["files"]["market.parquet"] = hashlib.sha256((leaf / "market.parquet").read_bytes()).hexdigest()
    (leaf / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Joint snapshot"):
        load_market(leaf)


def test_two_plausible_return_series_disagree_without_clear_primary_failure():
    market = market_frame()
    source = market.copy()
    market.loc[2:, "adj_factor"] *= 1.01
    result, report = reconcile_market(market, market.date, ("600000.SH",), "fallback", Secondary({"600000.SH": source}))
    assert report["status"] == "conflict" and report["stocks"][0]["primary_issues"] == []
    assert report["stocks"][0]["reason"] == "plausible_adjusted_return_conflict"


def test_different_algorithms_on_same_action_date_are_reported_not_spliced():
    market = market_frame()
    source = market.copy()
    market.loc[2:, "adj_factor"] *= 1.02
    source.loc[2:, "adj_factor"] *= 1.01
    source.loc[2, "change"] = 0.01
    source.loc[2, "pre_close"] = source.loc[2, "close"] / 1.01
    result, report = reconcile_market(market, market.date, ("600000.SH",), "fallback", Secondary({"600000.SH": source}))
    assert report["status"] == "verified" and len(report["stocks"][0]["corporate_action_differences"]) == 1
    assert result.adj_factor.tolist() == market.adj_factor.tolist()


def test_joint_source_evidence_survives_factor_evaluation_and_portable_report(config, tmp_path):

    primary = Primary()
    primary.corrupt = True
    download_dataset(config, provider=primary, secondary_provider=Secondary())
    research = ResearchConfig(config, BacktestConfig("2025-01-02", "2025-02-28", topk=1), tmp_path / "runs")
    result = evaluate_configured(research)
    store = ResultStore(research.runs_dir)
    factors = store.get("factors", result["factor_run_id"])
    evaluation = store.get("evaluations", result["run_id"])
    assert factors["metadata"]["source"] == "tushare_baostock"
    assert factors["metadata"]["cross_validation"]["stocks"][0]["price_source"] == "baostock"
    assert evaluation["metadata"]["cross_validation"]["status"] == "fallback"
    markdown = store.path("evaluations", result["run_id"]).with_name("report.md").read_text(encoding="utf-8")
    assert "联合数据来源" in markdown and "baostock" in markdown and "涨跌幅复权法" in markdown
