from collections import Counter
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from filelock import FileLock

from finance_quant.api import create_app
from finance_quant.configuration import DataConfig
from finance_quant.data.catalog import Catalog, catalog_status
from finance_quant.data.managed import DatasetDownloadError, download_dataset, month_ranges, year_ranges
from finance_quant.data.market import load_market


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
        dates = pd.date_range(params.get("trade_date", params.get("start_date")), params.get("trade_date", params.get("end_date")))
        if api == "trade_cal":
            return pd.DataFrame({"cal_date": dates.strftime("%Y%m%d"), "is_open": (dates.weekday < 5).astype(int)})
        dates = dates[dates.weekday < 5]
        if self.sparse_range and api == self.sparse_range[0] and code == self.sparse_range[1] and "trade_date" not in params:
            dates = dates[dates != pd.Timestamp(self.sparse_range[2])]
        if self.missing and api == "daily" and code == self.missing[0]:
            dates = dates[dates != pd.Timestamp(self.missing[1])]
        actual_code = "600519.SH" if self.wrong_instrument == (api, code) else code
        frame = pd.DataFrame({"ts_code": actual_code, "trade_date": dates.strftime("%Y%m%d")})
        if api in {"daily", "index_daily"}:
            for field, value in {"open": 10.0, "close": 10.1, "high": 10.2, "low": 9.9, "pre_close": 10.0,
                                 "vol": 10000, "amount": 1000, "pct_chg": 1.0}.items():
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
    assert year_ranges('20241231','20250301') == [('20241231','20241231'),('20250101','20250301')]


def test_snapshot_reuses_every_partition_and_roundtrips(config):
    provider = Provider()
    first = download_dataset(config, provider=provider)
    calls = len(provider.calls)
    second = download_dataset(config, provider=provider)
    assert first["manifest"] == second["manifest"]
    assert len(provider.calls) == calls
    status = catalog_status(config.root)
    assert len(status["snapshots"]) == 1
    assert status["jobs"][0]["cached"] == status["jobs"][0]["total"]
    market, benchmark, calendar, manifest = load_market(config.root)
    assert len(market) == 2 * len(benchmark)
    assert manifest["coverage"]["status"] == "complete"
    assert manifest["benchmark"] == "000300.SH"
    assert len(calendar) > len(benchmark)
    assert load_market(Path(manifest["snapshot_dir"]))[3]["id"] == manifest["id"]


def test_failed_request_is_retried_without_redownloading_successes(config):
    provider = Provider()
    provider.fail_once = ("daily_basic", "000001.SZ")
    with pytest.raises(DatasetDownloadError, match="incomplete"):
        download_dataset(config, provider=provider)
    assert not (config.root / "manifest.json").exists()
    first_calls = Counter(provider.calls)
    status = catalog_status(config.root)
    assert status["jobs"][0]["status"] == "failed"
    assert status["jobs"][0]["failed"] == 1
    assert "secret-not-for-artifacts" not in str(status)
    failed_calls = {(error['api'], tuple(sorted(error['params'].items()))) for error in status['jobs'][0]['report']['failures']}
    result = download_dataset(config, provider=provider)
    assert result["manifest"]["coverage"]["status"] == "complete"
    all_calls = Counter(provider.calls)
    for call, count in first_calls.items():
        assert all_calls[call] == count + (call in failed_calls)
    assert catalog_status(config.root)["jobs"][0]["cached"] == sum(first_calls.values()) - 1


def test_date_extension_and_stock_addition_reuse_prior_complete_years(config):
    provider = Provider()
    first = download_dataset(config, provider=provider)["manifest"]
    extended = replace(config, end="20250331", codes=(*config.codes, "600519.SH"))
    second = download_dataset(extended, provider=provider)["manifest"]
    assert second["trading_days"] > first["trading_days"]
    assert len(second["codes"]) == 3
    december = [(api, params) for api, params in provider.calls if api == "daily" and dict(params).get("ts_code") == "600000.SH"
                and dict(params).get("start_date") == "20241202"]
    assert len(december) == 1
    old_path = config.root / first["snapshot_path"]
    assert load_market(old_path)[3]["id"] == first["id"]
    assert len(catalog_status(config.root)["snapshots"]) == 2


def test_gap_does_not_publish_or_replace_previous_snapshot(config):
    provider = Provider()
    good = download_dataset(config, provider=provider)["manifest"]
    provider.missing = ("600000.SH", "2025-02-10")
    with pytest.raises(DatasetDownloadError, match="incomplete"):
        download_dataset(config, provider=provider, refresh=True)
    assert load_market(config.root)[3]["id"] == good["id"]
    job = catalog_status(config.root)["jobs"][0]
    assert job["status"] == "partial"
    assert any(error.get("missing_dates") == ["2025-02-10"] for error in job["report"]["failures"])


def test_sparse_range_response_is_repaired_by_actual_date_queries(config):
    provider = Provider()
    provider.sparse_range = ('adj_factor', '600000.SH', '2025-02-10')
    result = download_dataset(config, provider=provider)
    assert result['manifest']['coverage']['status'] == 'complete'
    assert any(api == 'adj_factor' and dict(params).get('trade_date') == '20250210' for api, params in provider.calls)
    call_count = len(provider.calls)
    repeated = download_dataset(config, provider=provider)
    assert len(provider.calls) == call_count
    assert repeated['manifest']['id'] == result['manifest']['id']


def test_corrupt_cache_fails_then_can_be_recovered(config):
    provider = Provider()
    good = download_dataset(config, provider=provider)["manifest"]
    original = good["requests"][0]
    path = config.root / original["raw_path"]
    path.write_bytes(b"damaged")
    with pytest.raises(DatasetDownloadError, match="incomplete"):
        download_dataset(config, provider=provider)
    assert load_market(config.root)[3]["id"] == good["id"]
    assert any(item["error"] == "cache_corrupt" for item in catalog_status(config.root)["jobs"][0]["report"]["failures"])
    assert download_dataset(config, provider=provider)["manifest"]["coverage"]["status"] == "complete"


def test_wrong_auxiliary_instrument_cannot_be_merged_silently(config):
    provider = Provider()
    provider.wrong_instrument = ("adj_factor", "600000.SH")
    with pytest.raises(DatasetDownloadError):
        download_dataset(config, provider=provider)
    assert not (config.root / "manifest.json").exists()
    assert catalog_status(config.root)["jobs"][0]["status"] == "failed"


def test_download_lock_does_not_interrupt_another_job(config):
    config.root.mkdir()
    catalog = Catalog(config.root)
    catalog.begin_job("active", {})
    with FileLock(config.root / ".download.lock", timeout=0):
        with pytest.raises(DatasetDownloadError, match="Another download"):
            download_dataset(config, provider=Provider())
    assert catalog_status(config.root)["jobs"][0]["status"] == "running"
    catalog.close()


def test_orphaned_job_is_marked_interrupted_after_acquiring_lock(config):
    catalog = Catalog(config.root)
    catalog.begin_job("orphan", {})
    catalog.close()
    download_dataset(config, provider=Provider())
    status = catalog_status(config.root)
    assert next(job for job in status["jobs"] if job["id"] == "orphan")["status"] == "interrupted"


def test_data_status_api_is_read_only_and_handles_empty_and_populated(config, tmp_path):
    empty_root = tmp_path / "not-created"
    client = TestClient(create_app(tmp_path / "runs", data_root=empty_root))
    assert client.get("/api/v1/data-status").json() == {"snapshots": [], "jobs": [], "partitions": {"succeeded": 0, "failed": 0}, "active_snapshot_id": None}
    assert not empty_root.exists()
    download_dataset(config, provider=Provider())
    client = TestClient(create_app(tmp_path / "runs", data_root=config.root))
    response = client.get("/api/v1/data-status")
    assert response.status_code == 200
    assert response.json()["snapshots"][0]["rows"] > 0
    assert client.post("/api/v1/data-status").status_code == 405


def test_auxiliary_data_on_closed_session_cannot_be_dropped_silently(config):
    class ExtraDayProvider(Provider):
        def query(self, api, **params):
            frame = super().query(api, **params)
            if api == 'daily_basic' and params.get('start_date') == '20250101':
                extra = frame.iloc[[0]].copy()
                extra['trade_date'] = '20250111'
                frame = pd.concat([frame, extra], ignore_index=True)
            return frame
    with pytest.raises(DatasetDownloadError):
        download_dataset(config, provider=ExtraDayProvider())
    assert not (config.root / 'manifest.json').exists()
    assert any(error.get('extra_dates') == ['2025-01-11'] for error in catalog_status(config.root)['jobs'][0]['report']['failures'])


def test_failed_refresh_is_isolated_and_refresh_retry_resumes_same_generation(config):
    provider = Provider()
    original = download_dataset(config, provider=provider)['manifest']
    provider.missing = ('600000.SH','2025-02-10')
    with pytest.raises(DatasetDownloadError):
        download_dataset(config, provider=provider, refresh=True)
    count = len(provider.calls)
    ordinary = download_dataset(config, provider=provider)['manifest']
    assert ordinary['id'] == original['id'] and len(provider.calls) == count
    provider.missing = None
    renewed = download_dataset(config, provider=provider, refresh=True)['manifest']
    assert renewed['id'] != original['id']
    assert renewed['cache_generation'] != 'base'
    assert all(record.get('cache_generation') == renewed['cache_generation'] for record in renewed['requests'])
    assert not (config.root / 'pending-refresh.json').exists()
    assert load_market(config.root / original['snapshot_path'])[3]['id'] == original['id']
