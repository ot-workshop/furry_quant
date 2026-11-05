from concurrent.futures import ThreadPoolExecutor

import pytest

from finance_quant.data.network_budget import DownloadPaused, NetworkBudget


def test_daily_budget_is_shared_durable_and_resets_in_shanghai(tmp_path):
    now = [1767196799.0]  # A fixed timestamp; advance by a full local day below.
    path = tmp_path / "quota.sqlite3"
    first = NetworkBudget(path, clock=lambda: now[0])
    second = NetworkBudget(path, clock=lambda: now[0])
    first.reserve("baostock", "login", daily=2, rpm=60, interval=0)
    second.reserve("baostock", "page", daily=2, rpm=60, interval=0)
    with pytest.raises(DownloadPaused):
        first.reserve("baostock", "logout", daily=2, rpm=60, interval=0)
    assert sum(r["count"] for r in second.status()["requests"]) == 2
    now[0] += 86400
    second.reserve("baostock", "login", daily=2, rpm=60, interval=0)
    assert second.status()["requests"][0]["count"] == 1


def test_concurrent_reservations_cannot_overspend(tmp_path):
    path = tmp_path / "quota.sqlite3"
    NetworkBudget(path)

    def run(_):
        try:
            NetworkBudget(path).reserve("baostock", "sdk", daily=3, rpm=100, interval=0)
            return 1
        except DownloadPaused:
            return 0

    with ThreadPoolExecutor(8) as pool:
        assert sum(pool.map(run, range(16))) == 3


def test_minute_limits_and_spacing_wait_without_holding_transaction(tmp_path):
    now = [1000.0]
    sleeps = []

    def sleep(amount):
        sleeps.append(amount)
        now[0] += amount

    budget = NetworkBudget(tmp_path / "quota.sqlite3", clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        budget.reserve("tushare_official", "daily", daily=100, rpm=2, interval=1)
    assert now[0] >= 1060 and sleeps and max(sleeps) <= 1


def test_restriction_hold_persists_and_does_not_send_probe(tmp_path):
    path = tmp_path / "quota.sqlite3"
    budget = NetworkBudget(path)
    budget.hold("baostock")
    with pytest.raises(DownloadPaused):
        NetworkBudget(path).reserve("baostock", "login", daily=100, rpm=60, interval=0)
    assert not budget.status()["requests"] and budget.status()["holds"]


def test_sdk_wire_hook_counts_messages_not_returned_rows(monkeypatch):
    import baostock.util.socketutil as wire

    import finance_quant.data.network_budget as module
    from finance_quant.data.providers.baostock_worker import _sdk

    calls = []
    monkeypatch.setattr(module, "reserve_baostock", lambda: calls.append("reserved"))
    monkeypatch.setattr(wire, "send_msg", lambda message: "ok")
    _sdk()
    assert wire.send_msg("never-persist-this-message") == "ok"
    assert calls == ["reserved"]


def test_worker_quota_stops_without_reconnect(tmp_path):
    from finance_quant.data.providers.baostock import BaoStockProvider

    calls = []

    def worker(request):
        calls.append(request)
        raise DownloadPaused("daily budget")

    with pytest.raises(DownloadPaused):
        BaoStockProvider(tmp_path, worker=worker).fetch("600000.SH", "20200101", "20201231")
    assert len(calls) == 1
