"""Dependency ordering, retry, pause and scheduling without live data services."""

import copy
from datetime import datetime

import pytest
from test_financial_download import Provider as FinancialProvider
from test_financial_download import market_version
from test_industry_download import Provider as IndustryProvider
from test_shared_market import SharedProvider
from test_shared_market import spec as market_spec

from finance_quant.application import data_updates as updates
from support.fakes import MemoryTaskStore


class CombinedProvider(SharedProvider):
    def __init__(self):
        super().__init__()
        self.financial = FinancialProvider()
        self.industry = IndustryProvider()
        self.financial_fail = False

    def query(self, api, **params):
        if api in {"income", "balancesheet", "cashflow"}:
            if self.financial_fail and api == "cashflow":
                raise RuntimeError("simulated provider outage")
            return self.financial.query(api, **params)
        if api in {"index_classify", "index_member", "index_member_all"}:
            return self.industry.query(api, **params)
        if api == "trade_cal":
            return self.financial.query(api, **params)
        result = super().query(api, **params)
        if api == "index_daily":
            result["ts_code"] = params["ts_code"]
        return result


def spec():
    return {
        "market": market_spec(),
        "market_version": None,
        "financial": {"refresh_days": 7, "full_refresh": False},
        "industry": {"levels": ["L1", "L2", "L3"], "refresh": True},
        "benchmarks": ["H00300.CSI"],
    }


def test_dependency_failure_blocks_financial_but_allows_independent_industry(tmp_path, monkeypatch):
    provider = CombinedProvider()
    plan = updates.preview(tmp_path, spec(), provider=provider)
    monkeypatch.setattr(
        updates.market_download, "run", lambda *a, **kw: {"status": "partial", "message": "missing quotes"}
    )
    result = updates.run(tmp_path, plan["id"], provider=provider)
    assert result["status"] == "partial"
    assert result["steps"]["financial"]["status"] == "blocked"
    assert result["steps"]["industry"]["status"] == "complete"
    assert result["steps"]["benchmarks"]["status"] == "blocked"
    assert not provider.financial.calls or all(api == "trade_cal" for api, _ in provider.financial.calls)


def test_schedule_validates_and_enqueues_once_per_day(tmp_path):
    store = MemoryTaskStore()
    updates.save_schedule(tmp_path, {"enabled": True, "hour": 0, "spec": spec()})
    updates.enqueue_due(store, tmp_path)
    updates.enqueue_due(store, tmp_path)
    assert len(store.list()["items"]) == 1
    assert store.list()["items"][0]["kind"] == "data_update"
    updates.save_schedule(tmp_path, {"enabled": False, "hour": 0, "spec": spec()})
    updates.enqueue_due(store, tmp_path)
    assert len(store.list()["items"]) == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.update(benchmarks=["unknown"]),
        lambda s: s.update(market=None),
        lambda s: s.update(industry={"levels": [], "refresh": False}),
    ],
)
def test_invalid_dependencies_and_specs_are_rejected(change):
    document = copy.deepcopy(spec())
    change(document)
    with pytest.raises(ValueError):
        updates.validate_spec(document)


def test_financial_only_update_uses_existing_market_version_and_resumes(tmp_path):
    version = market_version(tmp_path)
    document = {
        "market": None,
        "market_version": version,
        "financial": {"refresh_days": 0},
        "industry": None,
        "benchmarks": [],
    }
    provider = CombinedProvider()
    plan = updates.preview(tmp_path, document, provider=provider)
    provider.financial_fail = True
    failed = updates.run(tmp_path, plan["id"], provider=provider)
    assert failed["status"] == "partial"
    before = len(provider.financial.calls)
    provider.financial_fail = False
    assert updates.run(tmp_path, plan["id"], provider=provider)["status"] == "complete"
    assert {api for api, _ in provider.financial.calls[before:]} == {"cashflow"}


def test_disabled_schedule_does_not_require_any_provider_calls(tmp_path):
    assert updates.enqueue_due(MemoryTaskStore(), tmp_path) is None


def test_morning_schedule_does_not_change_its_request_after_market_close(tmp_path, monkeypatch):
    class Clock:
        hour = 1

        @classmethod
        def now(cls, zone):
            return datetime(2026, 11, 10, cls.hour, tzinfo=zone)

    monkeypatch.setattr(updates, "datetime", Clock)
    store = MemoryTaskStore()
    updates.save_schedule(tmp_path, {"enabled": True, "hour": 0, "spec": spec()})
    updates.enqueue_due(store, tmp_path)
    Clock.hour = 19
    updates.enqueue_due(store, tmp_path)
    assert len(store.list()["items"]) == 1


def test_full_queue_defers_schedule_without_stopping_the_worker(tmp_path):
    store = MemoryTaskStore()
    for index in range(20):
        store.create({}, {}, f"queued-task-{index}")
    updates.save_schedule(tmp_path, {"enabled": True, "hour": 0, "spec": spec()})
    updates.enqueue_due(store, tmp_path)
    assert len(store.list()["items"]) == 20
    store.finish(store.claim(), "succeeded", result={})
    updates.enqueue_due(store, tmp_path)
    assert any(row["kind"] == "data_update" for row in store.list()["items"])
