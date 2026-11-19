import json
from dataclasses import replace

import pandas as pd
import pytest
from test_index_download import setup_plan
from test_joint_data import Primary, Secondary, market_frame
from test_shared_market import quote_calls, spec
from test_shared_market import workspace as shared_workspace

from finance_quant.application import market_download as downloads
from finance_quant.application import quote_capture
from finance_quant.application.halt_history import find_halt_anchor
from finance_quant.data import shared_market as shared
from finance_quant.data.network_budget import DownloadPaused
from finance_quant.data.validation_errors import MarketDataError
from support import warehouse


class LongHaltSecondary(Secondary):
    def fetch(self, code, start, end):
        item = super().fetch(code, start, end)
        for frame in (item.raw, item.adjusted):
            halted = frame.date.between("2020-01-01", "2025-02-28")
            frame.loc[halted, "tradestatus"] = "0"
            frame.loc[halted, ["volume", "amount", "pctChg"]] = "0"
        return item


@pytest.fixture
def download_workspace(tmp_path, monkeypatch, market_store):
    return shared_workspace.__wrapped__(tmp_path, monkeypatch, market_store)


def test_long_halt_expands_calendar_preserves_plan_and_resumes_without_quote_downloads(download_workspace, monkeypatch):
    _, root, _, provider = download_workspace
    original = provider.query

    def query(api, **params):
        frame = original(api, **params)
        if api in {"daily", "daily_basic"}:
            frame = frame.loc[~frame.trade_date.between("20200101", "20250228")].reset_index(drop=True)
        return frame

    monkeypatch.setattr(provider, "query", query)
    document = {**spec(end="2025-03-10", codes=["600519.SH"]), "start": "2025-01-02"}
    plan = downloads.prepare(root, document, provider=provider)
    directory = root / "plans" / plan["id"]
    frozen = {name: (directory / name).read_bytes() for name in ("plan.json", "reference/calendar.parquet")}
    result = downloads.run(root, plan["id"], provider=provider, secondary=LongHaltSecondary())
    assert result["status"] == "complete", result["failures"]
    anchor = next(record for record in result["repairs"] if record["action"] == "located_pre_halt_quote")
    assert anchor["dates"] == ["2019-12-31"]
    assert frozen == {name: (directory / name).read_bytes() for name in frozen}
    expected = pd.bdate_range(plan["data_start"], plan["end"])
    frame, _ = shared.read_asset(root, "600519.SH", expected, plan["provider_id"])
    halted = frame.loc[frame.date.le("2025-02-28")]
    assert halted.is_suspended.all() and halted.close.eq(10).all() and halted.volume.eq(0).all()
    before = len(quote_calls(provider))
    again = downloads.run(root, plan["id"], provider=provider, secondary=LongHaltSecondary())
    assert again["status"] == "complete" and len(quote_calls(provider)) == before
    assert again["repairs"] == result["repairs"]
    assert (root / "halt-anchor-raw").is_dir()


def test_anchor_search_is_cached_and_never_uses_a_future_resume_quote(tmp_path):
    class NoPastQuote(Primary):
        def query(self, api, **params):
            frame = super().query(api, **params)
            return frame.iloc[:0] if api == "daily" else frame

    provider = NoPastQuote()
    for _ in range(2):
        with pytest.raises(MarketDataError) as caught:
            find_halt_anchor(tmp_path, provider, "000029.SZ", "2021-01-04", "2019-01-01")
        assert caught.value.category == "missing_pre_halt_quote"
    assert len(provider.calls) == 3
    assert all(dict(params)["end_date"] < "20210104" for _, params in provider.calls)
    (tmp_path / "PAUSE").write_text("pause")
    with pytest.raises(DownloadPaused):
        find_halt_anchor(tmp_path, provider, "000029.SZ", "2021-01-04", "2019-01-01")


@pytest.mark.parametrize("sample", [False, True])
@pytest.mark.parametrize("bad_secondary", ["activity", "basis"])
def test_valid_official_event_does_not_require_broken_secondary(tmp_path, monkeypatch, sample, bad_secondary):
    monkeypatch.setattr(quote_capture, "sample_code", lambda code: sample)

    class EventPrimary(Primary):
        def query(self, api, **params):
            frame = super().query(api, **params)
            if api == "daily":
                after = frame.trade_date.ge("20200106")
                frame.loc[after, ["open", "high", "low", "close"]] *= 2
                frame.loc[frame.trade_date.gt("20200106"), "pre_close"] *= 2
                frame.loc[frame.trade_date.eq("20200106"), "pct_chg"] = 100
            return frame

    class BrokenSecondary(Secondary):
        def fetch(self, code, start, end):
            item = super().fetch(code, start, end)
            raw, adjusted = item.raw.copy(), item.adjusted.copy()
            if bad_secondary == "activity":
                raw.loc[raw.date.eq("2020-01-06"), "tradestatus"] = "0"
            else:
                adjusted.loc[adjusted.date.eq("2020-01-06"), "adjustflag"] = "3"
            return replace(item, raw=raw, adjusted=adjusted)

    root = tmp_path / "warehouse"
    provider = EventPrimary()
    plan = setup_plan(root, provider)
    for task in plan["tasks"]:
        task["trading_events"] = [
            {
                "code": task["code"],
                "date": "2020-01-06",
                "known_by": "2020-01-02",
                "event": "listing_resumption",
                "no_daily_price_limit": True,
                "source": "offline:dated-public-notice",
            }
        ]
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    secondary = BrokenSecondary()
    result = warehouse.run_index_download(root, provider=provider, secondary=secondary)
    assert result["status"] == "complete", result["failures"]
    assert result["validation_counts"]["single_source_passed"] == 2
    assert bool(secondary.calls) == sample
    for asset in result["assets"].values():
        path = root / asset["file"]
        receipt = json.loads(path.with_name("receipt.json").read_text(encoding="utf-8"))
        assert receipt["cross_validation"]["status"] == ("unavailable" if sample else "not_checked")
        assert bool(receipt["quality"]["verified_event_moves"])
        if sample:
            assert receipt["cross_validation"]["validation_dates"] == ["2020-01-06"]
        frame = pd.read_parquet(path)
        assert frame.price_source.eq("tushare").all()
        day = frame.loc[frame.date.eq("2020-01-06")].iloc[0]
        assert day.close == 20 and not day.is_suspended and day.reviewed_unbounded_session


def test_unexplained_large_move_has_dates_rules_and_quarantine_without_replacing_old_data(
    download_workspace, monkeypatch
):
    _, root, _, provider = download_workspace
    original = provider.query
    monkeypatch.setattr(quote_capture, "sample_code", lambda code: False)

    def query(api, **params):
        frame = original(api, **params)
        if api == "daily":
            mask = frame.trade_date.ge("20250305")
            frame.loc[mask, ["open", "high", "low", "close"]] *= 2
            frame.loc[frame.trade_date.gt("20250305"), "pre_close"] *= 2
            frame.loc[frame.trade_date.eq("20250305"), "pct_chg"] = 100
        return frame

    monkeypatch.setattr(provider, "query", query)
    plan = downloads.prepare(root, spec(end="2025-03-10", codes=["600000.SH"]), provider=provider)
    source = market_frame(pd.bdate_range("2025-02-28", "2025-03-10"))
    source.loc[source.date.ge("2025-03-05"), ["open", "high", "low", "close"]] *= 2
    source.loc[source.date.gt("2025-03-05"), "pre_close"] *= 2
    source.loc[source.date.eq("2025-03-05"), "change"] = 1
    result = downloads.run(root, plan["id"], provider=provider, secondary=Secondary({"600000.SH": source}))
    assert result["status"] == "partial"
    failure = result["failures"][0]
    assert failure["category"] == "market_quality_requires_review" and failure["dates"] == ["2025-03-05"]
    assert failure["evidence"]["issues"][0]["rule"] == "unverified_price_move"
    quarantine = root / failure["evidence"]["capture"] / failure["evidence"]["quarantine_file"]
    assert quarantine.exists()
    old, _ = shared.read_asset(root, "600000.SH", pd.bdate_range("2025-01-02", "2025-02-28"), plan["provider_id"])
    assert old.close.eq(10).all()


def test_old_partial_checkpoints_skip_successful_refreshes_and_retry_only_failed_stocks(
    download_workspace, monkeypatch
):
    _, root, _, provider = download_workspace
    document = {**spec(end="2025-03-10"), "refresh_sessions": 20}
    plan = downloads.prepare(root, document, provider=provider)
    original = downloads.SegmentFetch.fetch
    calls = []
    blocked = True

    def fetch(self, task, *args, **kwargs):
        calls.append(task["code"])
        if blocked and task["code"] == "000001.SZ":
            raise MarketDataError("需更新校验规则", category="market_quality_requires_review", dates=["2025-03-05"])
        return original(self, task, *args, **kwargs)

    monkeypatch.setattr(downloads.SegmentFetch, "fetch", fetch)
    first = downloads.run(root, plan["id"], provider=provider, secondary=Secondary())
    assert first["status"] == "partial" and first["completed"] == 1
    progress_path = root / "plans" / plan["id"] / "progress.json"
    old_progress = json.loads(progress_path.read_text(encoding="utf-8"))
    old_progress.pop("completed_codes")
    progress_path.write_text(json.dumps(old_progress), encoding="utf-8")
    calls.clear()
    before = quote_calls(provider).copy()
    blocked = False
    resumed = downloads.run(root, plan["id"], provider=provider, secondary=Secondary())
    assert resumed["status"] == "complete", resumed["failures"]
    assert set(calls) == {"000001.SZ"}
    added = quote_calls(provider)[len(before) :]
    assert added and all(params["ts_code"] == "000001.SZ" for _, params in added)
    assert set(resumed["completed_codes"]) == {"600000.SH", "000001.SZ"}
