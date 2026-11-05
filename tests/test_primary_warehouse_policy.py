import json
from dataclasses import replace

import pandas as pd
import pytest
from test_index_download import SparsePrimary, setup_plan
from test_joint_data import Primary, Secondary, bundle, market_frame

from finance_quant.application import index_download as module
from finance_quant.data.network_budget import DownloadPaused


def test_clean_unsampled_official_data_never_contacts_secondary(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "sample_code", lambda code: False)
    root = tmp_path / "warehouse"
    provider = Primary()
    setup_plan(root, provider)
    secondary = Secondary(error=True)
    result = module.run_index_download(root, provider=provider, secondary=secondary)
    assert result["status"] == "complete" and secondary.calls == []
    assert result["validation_counts"] == {
        "single_source_passed": 2,
        "dual_source_verified": 0,
        "secondary_fallback_passed": 0,
    }
    for asset in result["assets"].values():
        frame = pd.read_parquet(root / asset["file"])
        receipt = json.loads((root / asset["file"]).with_name("receipt.json").read_text(encoding="utf-8"))
        assert frame.validation_level.eq("single_source_passed").all()
        assert frame.price_source.eq("tushare").all()
        assert receipt["cross_validation"]["status"] == "not_checked"


@pytest.mark.parametrize("paused", [False, True])
def test_optional_sample_failure_keeps_only_single_source_label(tmp_path, monkeypatch, paused):
    monkeypatch.setattr(module, "sample_code", lambda code: True)

    class Unavailable(Secondary):
        def fetch(self, *args):
            if paused:
                raise DownloadPaused("quota")
            return super().fetch(*args)

    root = tmp_path / "warehouse"
    provider = Primary()
    setup_plan(root, provider)
    result = module.run_index_download(root, provider=provider, secondary=Unavailable(error=True))
    assert result["status"] == "complete" and result["validation_counts"]["single_source_passed"] == 2
    for asset in result["assets"].values():
        receipt = json.loads((root / asset["file"]).with_name("receipt.json").read_text(encoding="utf-8"))
        assert receipt["cross_validation"]["status"] == "unavailable"
        assert receipt["validation_level"] == "single_source_passed"


def test_sample_success_is_distinct_from_secondary_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "sample_code", lambda code: True)
    for provider, level in [(Primary(), "dual_source_verified"), (SparsePrimary(), "secondary_fallback_passed")]:
        root = tmp_path / level
        setup_plan(root, provider)
        result = module.run_index_download(root, provider=provider, secondary=Secondary())
        assert result["status"] == "complete" and result["validation_counts"][level] == 2
        assert all(a["validation_level"] == level for a in result["assets"].values())


def test_invalid_primary_cannot_be_saved_as_single_source_when_secondary_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "sample_code", lambda code: False)
    root = tmp_path / "warehouse"
    provider = SparsePrimary()
    setup_plan(root, provider)
    result = module.run_index_download(root, provider=provider, secondary=Secondary(error=True))
    assert result["completed"] == 0 and len(result["failures"]) == 2 and not (root / "manifest.json").exists()


def test_detected_plausible_source_conflict_remains_quarantined(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "sample_code", lambda code: True)

    class Conflicting(Secondary):
        def fetch(self, code, start, end):
            frame = market_frame(pd.bdate_range(start, end), code)
            frame[["open", "high", "low", "close", "pre_close"]] += 1
            return Secondary({code: frame}).fetch(code, start, end)

    root = tmp_path / "warehouse"
    provider = Primary()
    setup_plan(root, provider)
    result = module.run_index_download(root, provider=provider, secondary=Conflicting())
    assert result["completed"] == 0 and len(result["failures"]) == 2
    assert not (root / "manifest.json").exists()


def test_sample_selection_is_stable_and_not_a_fixed_short_stock_list():
    codes = [f"{number:06d}.SZ" for number in range(1, 1001)]
    selected = {code for code in codes if module.sample_code(code)}
    assert selected == {code for code in reversed(codes) if module.sample_code(code)}
    assert 60 < len(selected) < 140


def test_confirmed_halt_does_not_require_secondary_adjusted_prices_to_be_correct(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "sample_code", lambda code: False)

    class HaltPrimary(Primary):
        def query(self, api, **params):
            result = super().query(api, **params)
            if api in ["daily", "daily_basic"]:
                result = result.loc[result.trade_date != "20200106"]
            return result

    class BrokenAdjusted(Secondary):
        def fetch(self, code, start, end):
            item = bundle(market_frame(pd.bdate_range(start, end), code), code)
            raw, adj = item.raw.copy(), item.adjusted.copy()
            for part in [raw, adj]:
                halted = part.date.eq("2020-01-06")
                part.loc[halted, "tradestatus"] = "0"
                part.loc[halted, ["volume", "amount"]] = "0"
            # Bad adjusted prices on an unrelated active day must not be used.
            adj.loc[adj.date.eq("2020-01-08"), "close"] = "9999"
            return replace(item, raw=raw, adjusted=adj)

    root = tmp_path / "warehouse"
    provider = HaltPrimary()
    setup_plan(root, provider)
    result = module.run_index_download(root, provider=provider, secondary=BrokenAdjusted())
    assert result["status"] == "complete" and result["validation_counts"]["single_source_passed"] == 2
    for asset in result["assets"].values():
        frame = pd.read_parquet(root / asset["file"])
        halted = frame.loc[frame.date.eq("2020-01-06")].iloc[0]
        assert halted.is_suspended and halted.volume == 0 and halted.close == 10
        assert frame.adjustment_source.eq("tushare").all()
        receipt = json.loads((root / asset["file"]).with_name("receipt.json").read_text(encoding="utf-8"))
        assert receipt["cross_validation"]["suspension_evidence"]["dates"] == ["2020-01-06"]
        assert receipt["validation_level"] == "single_source_passed"
