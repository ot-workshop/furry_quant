import json
import sqlite3
from contextlib import closing

import pandas as pd
import pytest

from finance_quant.configuration import DataConfig
from finance_quant.data.managed import DatasetDownloadError, month_ranges, year_ranges
from support.fixed_pool import download_dataset


class Provider:
    """Deterministic exchange fixture; never accesses credentials or the network."""

    def __init__(self):
        self.calls = []
        self.fail_once = None
        self.missing = None
        self.wrong_instrument = None
        self.sparse_range = None

    def query(self, api, **params):
        self.calls.append((api, tuple(sorted(params.items()))))
        code = params.get("ts_code")
        if self.fail_once == (api, code):
            self.fail_once = None
            raise RuntimeError("response body contains secret-not-for-artifacts")
        if api == "stock_basic":
            return pd.DataFrame({"ts_code": [code], "name": [code], "industry": ["fixture"]})
        dates = pd.date_range(
            params.get("trade_date", params.get("start_date")), params.get("trade_date", params.get("end_date"))
        )
        if api == "trade_cal":
            return pd.DataFrame({"cal_date": dates.strftime("%Y%m%d"), "is_open": (dates.weekday < 5).astype(int)})
        dates = dates[dates.weekday < 5]
        if (
            self.sparse_range
            and api == self.sparse_range[0]
            and code == self.sparse_range[1]
            and "trade_date" not in params
        ):
            dates = dates[dates != pd.Timestamp(self.sparse_range[2])]
        if self.missing and api == "daily" and code == self.missing[0]:
            dates = dates[dates != pd.Timestamp(self.missing[1])]
        actual_code = "600519.SH" if self.wrong_instrument == (api, code) else code
        frame = pd.DataFrame({"ts_code": actual_code, "trade_date": dates.strftime("%Y%m%d")})
        if api in {"daily", "index_daily"}:
            for field, value in {
                "open": 10.0,
                "close": 10.1,
                "high": 10.2,
                "low": 9.9,
                "pre_close": 10.0,
                "vol": 10000,
                "amount": 1000,
                "pct_chg": 1.0,
            }.items():
                frame[field] = value
        elif api == "adj_factor":
            frame["adj_factor"] = 1.0
        elif api == "daily_basic":
            frame["pe_ttm"], frame["pb"], frame["total_mv"] = 10.0, 1.0, 1000
        elif api == "stk_limit":
            frame["up_limit"], frame["down_limit"] = 11.0, 9.0
        else:
            raise AssertionError(api)
        return frame


@pytest.fixture
def config(tmp_path):
    return DataConfig(tmp_path / "market", "20241202", "20250228", ("600000.SH", "000001.SZ"))


def test_month_partitions_cover_leap_year_and_year_boundaries():
    assert month_ranges("20240228", "20240302") == [("20240228", "20240229"), ("20240301", "20240302")]
    assert month_ranges("20241231", "20250101") == [("20241231", "20241231"), ("20250101", "20250101")]
    assert year_ranges("20241231", "20250301") == [("20241231", "20241231"), ("20250101", "20250301")]


def test_sparse_range_response_is_repaired_by_actual_date_queries(config):
    provider = Provider()
    provider.sparse_range = ("adj_factor", "600000.SH", "2025-02-10")
    result = download_dataset(config, provider=provider)
    assert result["manifest"]["coverage"]["status"] == "complete"
    assert any(api == "adj_factor" and dict(params).get("trade_date") == "20250210" for api, params in provider.calls)
    call_count = len(provider.calls)
    repeated = download_dataset(config, provider=provider)
    assert len(provider.calls) == call_count
    assert repeated["manifest"]["id"] == result["manifest"]["id"]


def test_wrong_auxiliary_instrument_cannot_be_merged_silently(config):
    provider = Provider()
    provider.wrong_instrument = ("adj_factor", "600000.SH")
    with pytest.raises(DatasetDownloadError):
        download_dataset(config, provider=provider)
    assert not (config.root / "manifest.json").exists()


def test_auxiliary_data_on_closed_session_cannot_be_dropped_silently(config):
    class ExtraDayProvider(Provider):
        def query(self, api, **params):
            frame = super().query(api, **params)
            if api == "daily_basic" and params.get("start_date") == "20250101":
                extra = frame.iloc[[0]].copy()
                extra["trade_date"] = "20250111"
                frame = pd.concat([frame, extra], ignore_index=True)
            return frame

    with pytest.raises(DatasetDownloadError):
        download_dataset(config, provider=ExtraDayProvider())
    assert not (config.root / "manifest.json").exists()
    with closing(sqlite3.connect(config.root / "catalog.sqlite3")) as connection:
        report = json.loads(connection.execute("SELECT report FROM jobs").fetchone()[0])
    assert any(error.get("extra_dates") == ["2025-01-11"] for error in report["failures"])
