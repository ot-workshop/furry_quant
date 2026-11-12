"""Safety and incremental-download acceptance tests, written before implementation."""

import pandas as pd
import pytest
from test_financial_download import Provider, market_version

from finance_quant.application import financial_download as download
from finance_quant.data.financials import FinancialBinding, FinancialStore, merge_observations, report_versions
from support.financial import income


def test_first_download_cannot_backfill_uncertified_values_into_the_past():
    observations = merge_observations(None, income(), "2025-05-12")
    versions = report_versions(observations, pd.bdate_range("2025-03-01", "2025-06-01"))
    assert versions.iloc[0].available_at == pd.Timestamp("2025-05-13 09:30", tz="Asia/Shanghai")


def test_new_update_flag_does_not_erase_what_was_previously_observed():
    observations = merge_observations(None, income(update_flag="1", n_income_attr_p=10), "2025-03-20")
    observations = merge_observations(observations, income(update_flag="1", n_income_attr_p=12), "2025-05-12")
    versions = report_versions(observations, pd.bdate_range("2025-03-01", "2025-06-01"))
    assert list(versions.n_income_attr_p) == [10, 12]
    assert versions.iloc[1].available_at > versions.iloc[0].available_at


def test_first_seen_timestamp_uses_exchange_local_date():
    observations = merge_observations(None, income(), "2025-05-11T17:30:00Z")
    assert observations.iloc[0].first_seen == pd.Timestamp("2025-05-12")


def test_binding_pins_visibility_policy():
    binding = FinancialBinding("financials", "a" * 24)
    assert binding.as_dict()["availability_policy"] == "observed"


def test_identical_numbers_from_different_stocks_cannot_share_an_object(tmp_path):
    from support.financial import publish

    manifest = publish(tmp_path, [income(ts_code="600000.SH"), income(ts_code="000001.SZ")])
    stocks = manifest["stocks"]
    assert stocks["600000.SH"]["object"] != stocks["000001.SZ"]["object"]
    loaded, _ = FinancialStore(tmp_path).load(manifest, ["000001.SZ"])
    assert set(loaded.ts_code) == {"000001.SZ"}


def test_legacy_cross_stock_collision_is_detected_and_redownloaded(tmp_path):
    from finance_quant.data.financials import FinancialStockMismatch
    from finance_quant.data.managed import atomic_json

    version = market_version(tmp_path, ("600000.SH", "000001.SZ"))
    spec = {"market_version": version, "refresh_days": 0}
    first = download.preview(tmp_path, spec)
    download.run(tmp_path, first["id"], provider=Provider())
    store = FinancialStore(tmp_path / "financials")
    wrong, target = store.pointer("600000.SH"), store.pointer("000001.SZ")
    target.update(object=wrong["object"], sha256=wrong["sha256"])
    atomic_json(store.root / "stocks" / "000001.SZ.json", target)
    with pytest.raises(FinancialStockMismatch):
        store.observations("000001.SZ")
    plan = download.preview(tmp_path, spec)
    provider = Provider()
    assert download.run(tmp_path, plan["id"], provider=provider)["status"] == "complete"
    assert set(store.observations("000001.SZ").ts_code) == {"000001.SZ"}
    assert all(
        params["start_date"] == "19900101"
        for api, params in provider.calls
        if api != "trade_cal" and params["ts_code"] == "000001.SZ"
    )


def test_resume_does_not_refetch_completed_statement_streams(tmp_path):
    version = market_version(tmp_path, ("600000.SH",))
    plan = download.preview(tmp_path, {"market_version": version, "refresh_days": 0})
    failed = Provider(fail=lambda api, params: api == "cashflow")
    assert download.run(tmp_path, plan["id"], provider=failed)["status"] == "partial"
    retry = Provider()
    result = download.run(tmp_path, plan["id"], provider=retry)
    assert result["status"] == "complete"
    assert {api for api, _ in retry.calls} == {"cashflow"}
    before = len(retry.calls)
    assert download.run(tmp_path, plan["id"], provider=retry)["status"] == "complete"
    assert len(retry.calls) == before


def test_refresh_queries_recent_announcements_and_reuses_calendar(tmp_path):
    version = market_version(tmp_path, ("600000.SH",))
    spec = {"market_version": version, "refresh_days": 0}
    first = download.preview(tmp_path, spec)
    download.run(tmp_path, first["id"], provider=Provider())
    second = download.preview(tmp_path, spec)
    assert second["id"] != first["id"]
    provider = Provider()
    assert download.run(tmp_path, second["id"], provider=provider)["status"] == "complete"
    assert all(api != "trade_cal" for api, _ in provider.calls)
    assert all(params["start_date"] > "19900101" for _, params in provider.calls)
    store = FinancialStore(tmp_path / "financials")
    assert store.current() is not None


def test_full_refresh_bypasses_freshness_and_restores_full_announcement_range(tmp_path):
    version = market_version(tmp_path, ("600000.SH",))
    first = download.preview(tmp_path, {"market_version": version, "refresh_days": 0})
    download.run(tmp_path, first["id"], provider=Provider())
    fresh = download.preview(tmp_path, {"market_version": version, "refresh_days": 7})
    assert not fresh["codes"]
    full = download.preview(tmp_path, {"market_version": version, "refresh_days": 7, "full_refresh": True})
    assert full["codes"][0]["start_date"] == "19900101"
    provider = Provider()
    assert download.run(tmp_path, full["id"], provider=provider)["status"] == "complete"
    assert all(params["start_date"] == "19900101" for api, params in provider.calls if api != "trade_cal")


def test_observed_policy_is_pinned_to_request_and_worker_and_rejects_backfill(workspace):
    from finance_quant.application.research_requests import validate_request
    from finance_quant.application.task_worker import financial_binding
    from support.financial import balance, cashflow, publish

    data, runs, request = workspace
    publish(
        data / "financials",
        [maker(ts_code=code) for code in request["data"]["codes"] for maker in (income, balance, cashflow)],
    )
    request["factors"] = [{"name": "roe_ttm", "weight": 1.0}]
    with pytest.raises(ValueError, match="首次观测口径"):
        validate_request(request, data, runs)
    binding = FinancialBinding(data / "financials", FinancialStore(data / "financials").current()["id"])
    assert financial_binding({"financials": binding.as_dict()}, data).availability_policy == "observed"
    assert (
        financial_binding({"financials": {"version": binding.version}}, data).availability_policy
        == "announcement_estimate"
    )
