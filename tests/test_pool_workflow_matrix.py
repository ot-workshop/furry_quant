"""Different universes through published snapshots, queued workflow and native Qlib.

Provider data here is synthetic and deterministic, never an online-data claim.
"""

import math
from dataclasses import asdict

import numpy as np
import pandas as pd
import pytest
from test_joint_data import bundle
from test_managed_data import Provider

import finance_quant.application.research_requests as requests
from finance_quant.application.research_requests import snapshot_registry, validate_request
from finance_quant.configuration import BacktestConfig, DataConfig
from finance_quant.data.managed import DatasetDownloadError
from finance_quant.data.validation_config import CrosscheckConfig
from finance_quant.research.registry import EvaluationConfig
from finance_quant.storage import ResultStore
from support.fakes import make_service, run_claimed
from support.fixed_pool import download_dataset, publish_fixture_snapshot


def codes_for(size, seed):
    return tuple(
        f"{601000 + seed + i:06d}.SH"
        if i % 3 == 0
        else f"{300000 + seed + i:06d}.SZ"
        if i % 3 == 1
        else f"{1000 + seed + i:06d}.SZ"
        for i in range(size)
    )


def bars(code, dates):
    dates = pd.DatetimeIndex(dates)
    n = np.busday_count("2024-10-01", dates.to_numpy().astype("datetime64[D]"))
    seed = int(code[:6]) % 997

    def close(x):
        return 8 + seed % 17 + 0.015 * x + 0.4 * np.sin(x / (3 + seed % 11) + seed)

    opening, closing, previous = close(n - 1) + 0.05 * np.cos(n), close(n), close(n - 1)
    volume = 1_000_000 + n % 5 * 20_000
    return pd.DataFrame(
        {
            "date": dates,
            "instrument": code[-2:] + code[:6],
            "name": code,
            "industry": "fixture",
            "open": opening,
            "close": closing,
            "high": np.maximum(opening, closing) + 0.12,
            "low": np.minimum(opening, closing) - 0.12,
            "pre_close": previous,
            "volume": volume,
            "amount": closing * volume,
            "change": closing / previous - 1,
            "adj_factor": 1.0,
            "pe_ttm": 10.0 + seed % 13,
            "pb": 1.0,
            "total_mv": 1e9,
            "up_limit": previous * 1.1,
            "down_limit": previous * 0.9,
        }
    )


class DiversePrimary(Provider):
    def query(self, api, **params):
        frame = super().query(api, **params)
        if api in {"daily", "index_daily", "daily_basic", "adj_factor", "stk_limit"}:
            reference = bars(params["ts_code"], pd.to_datetime(frame.trade_date))
            if api in {"daily", "index_daily"}:
                for field in ["open", "close", "high", "low", "pre_close"]:
                    frame[field] = reference[field].to_numpy()
                frame["vol"], frame["amount"], frame["pct_chg"] = (
                    reference.volume.to_numpy() / 100,
                    reference.amount.to_numpy() / 1000,
                    reference.change.to_numpy() * 100,
                )
            elif api == "daily_basic":
                frame["pe_ttm"] = reference.pe_ttm.to_numpy()
            elif api == "stk_limit":
                frame["up_limit"], frame["down_limit"] = reference.up_limit.to_numpy(), reference.down_limit.to_numpy()
        return frame


class DiverseSecondary:
    def fetch(self, code, start, end):
        return bundle(bars(code, pd.bdate_range(start, end)), code)


def request_for(codes, *, size=None, snapshot_id="pending"):
    size = size or len(codes)
    return {
        "name": f"generated-pool-{size}",
        "data": {"mode": "snapshot", "snapshot_id": snapshot_id, "codes": list(codes)},
        "backtest": asdict(BacktestConfig("2025-01-20", "2025-03-28", topk=min(3, len(codes)))),
        "factors": [
            {"name": "momentum", "weight": 0.5, "window": 7 + size % 20},
            {"name": "value", "weight": 0.2, "window": None},
            {"name": "volatility", "weight": 0.3, "window": 3 + size % 5},
        ],
        "evaluation": EvaluationConfig(horizons=(1, 3)).as_dict(),
    }


def run_request(document, data_root, runs, provider=None):
    codes = document["data"]["codes"]
    name = "pool-" + type(provider or DiversePrimary()).__name__.lower()
    manifest = publish_fixture_snapshot(
        data_root,
        codes,
        "20241202",
        "20250331",
        provider=provider or DiversePrimary(),
        secondary_provider=DiverseSecondary(),
        validation="fallback",
        name=name,
    )
    document = {**document, "data": {**document["data"], "snapshot_id": manifest["id"]}}
    service = make_service(runs, data_root)
    task = service.submit(document, "matrix-" + str(len(service.store.list()["items"])) + "-key")
    assert service.store.claim() == task["id"]
    return run_claimed(service, task["id"])


@pytest.mark.parametrize("size,seed", [(2, 7), (7, 23), (17, 109), (50, 503)])
def test_arbitrary_pool_sizes_complete_factors_and_native_backtest(tmp_path, size, seed):
    codes = codes_for(size, seed)
    request = request_for(codes)
    task = run_request(request, tmp_path / "data", tmp_path / "runs")
    assert task["status"] == "succeeded", task["error"]
    result = task["result"]
    market, _, _, manifest = snapshot_registry(tmp_path / "data")[result["data_version"]]
    assert set(manifest["codes"]) == set(codes)
    assert set(market.instrument) == {code[-2:] + code[:6] for code in codes}
    store = ResultStore(tmp_path / "runs")
    factors = store.get("factors", result["factor_run_id"])
    backtest = store.get("backtests", result["backtest_run_id"])
    evaluation = store.get("evaluations", result["evaluation_run_id"])
    assert backtest["metadata"]["factor_run_id"] == evaluation["metadata"]["factor_run_id"] == result["factor_run_id"]
    assert len(backtest["equity"]) == len(pd.bdate_range("2025-01-20", "2025-03-28")) + 1
    assert {t["instrument"] for t in backtest["trades"]}.issubset(set(market.instrument))

    # Independent scalar calculation and average-tie ranks at three dates.
    config = {s["name"]: s for s in request["factors"]}
    for date in ["2025-01-17", "2025-02-14", "2025-03-28"]:
        expected = {}
        for code in codes:
            section = bars(code, pd.bdate_range("2024-12-02", date))
            values = section.close.to_numpy()
            w, v = config["momentum"]["window"], config["volatility"]["window"]
            returns = values[-v:] / values[-v - 1 : -1] - 1
            expected[code[-2:] + code[:6]] = {
                "momentum": values[-1] / values[-1 - w] - 1,
                "value": 1 / section.pe_ttm.iloc[-1],
                "volatility": float(np.std(returns, ddof=1) * math.sqrt(252)),
            }
        for saved in [r for r in factors["history"] if r["date"] == date]:
            weighted = 0.0
            for name, value in expected[saved["instrument"]].items():
                population = [e[name] for e in expected.values()]
                better = sum(v > value if name == "volatility" else v < value for v in population)
                equal = sum(v == value for v in population)
                score = (better + (equal + 1) / 2) / size * 100
                assert math.isclose(saved["raw"][name], value, abs_tol=1e-10)
                assert math.isclose(saved["scores"][name], score, abs_tol=1e-8)
                weighted += config[name]["weight"] * score
            assert math.isclose(saved["composite"], weighted, abs_tol=1e-8)


@pytest.mark.parametrize("field,value", [("list_date", "20250115"), ("delist_date", "20250115")])
def test_listing_boundaries_fail_with_actionable_dates_before_fetching_that_stocks_history(tmp_path, field, value):
    codes = codes_for(3, 81)

    class Listing(DiversePrimary):
        def query(self, api, **params):
            frame = super().query(api, **params)
            if api == "stock_basic" and params["ts_code"] == codes[-1]:
                frame[field] = value
            return frame

    provider = Listing()
    config = DataConfig(tmp_path / "data", "20241202", "20250331", codes, validation=CrosscheckConfig("fallback"))
    with pytest.raises(DatasetDownloadError):
        download_dataset(config, provider=provider, secondary_provider=DiverseSecondary())
    assert not any(api != "stock_basic" and dict(params).get("ts_code") == codes[-1] for api, params in provider.calls)
    assert not (config.root / "manifest.json").exists()


def test_valuation_ineligible_pool_is_rejected_at_submission_then_price_only_task_succeeds(tmp_path):
    codes = codes_for(5, 93)

    class NoPE(DiversePrimary):
        def query(self, api, **params):
            frame = super().query(api, **params)
            if api == "daily_basic" and params["ts_code"] != codes[-1]:
                frame["pe_ttm"] = -2.0
            return frame

    request = request_for(codes)
    with pytest.raises(ValueError, match="有效评分不足"):
        run_request(request, tmp_path / "data", tmp_path / "runs", NoPE())
    assert not (tmp_path / "runs").exists()
    request["factors"] = [{"name": "momentum", "weight": 1.0, "window": 7}]
    succeeded = run_request(request, tmp_path / "data", tmp_path / "runs", NoPE())
    assert succeeded["status"] == "succeeded", succeeded["error"]


@pytest.mark.parametrize(
    "code", ["688981.SH", "689009.SH", "430047.BJ", "510300.SH", "159915.SZ", "900901.SH", "200002.SZ", "000300.SH"]
)
def test_unsupported_execution_types_are_rejected_before_queue_writes(tmp_path, monkeypatch, code):
    request = request_for([code, "600900.SH"])

    manifest = {
        "id": "pending",
        "snapshot_dir": str(tmp_path),
        "start": "20241202",
        "end": "20250331",
        "codes": [code, "600900.SH"],
        "files": {},
    }
    monkeypatch.setattr(requests, "snapshot_registry", lambda root: {"pending": (None, None, None, manifest)})
    with pytest.raises(ValueError, match="尚未适配"):
        validate_request(request, tmp_path / "data", tmp_path / "runs")
    assert not (tmp_path / "data").exists() and not (tmp_path / "runs").exists()


def test_unreadable_candidate_does_not_hide_other_valid_snapshots(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    manifest = publish_fixture_snapshot(data_root, codes_for(3, 63), "20241202", "20250331", provider=DiversePrimary())
    unreadable = data_root / "research/downloads/fixture/snapshots/unreadable/manifest.json"
    unreadable.parent.mkdir()
    original = type(unreadable).is_file

    def checked(path):
        if path == unreadable:
            raise PermissionError("unreadable local entry")
        return original(path)

    monkeypatch.setattr(type(unreadable), "is_file", checked)
    assert manifest["id"] in snapshot_registry(data_root)
