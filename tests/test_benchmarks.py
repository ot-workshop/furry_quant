import copy
import math
import statistics
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest
from test_historical_workflow import request, warehouse_fixture
from test_managed_data import Provider

from finance_quant.application.benchmark_workflow import publish_benchmark_view
from finance_quant.application.historical_workflow import run_historical_research
from finance_quant.application.real_workflow import run_configured
from finance_quant.configuration import BacktestConfig, DataConfig, ResearchConfig
from finance_quant.data.benchmarks import (
    ensure_series,
    load_series,
    normalize_benchmark,
    normalize_series,
    publish_series,
    validate_coverage,
)
from finance_quant.data.market import load_market
from finance_quant.data.providers.tushare import TushareProviderError, TushareSettings
from finance_quant.research.performance import METRIC_KEYS, calculate_performance
from finance_quant.storage import ResultStore
from support.fixed_pool import download_dataset


def seed(root, dates, code="H00300.CSI", slope=0.2):
    close = 100.0 + np.arange(len(dates)) * slope
    frame = pd.DataFrame({"date": dates, "close": close, "pre_close": np.r_[100.0 - slope, close[:-1]]})
    return publish_series(code, frame, root=root)


def test_official_base_observation_is_only_an_anchor_and_invalid_quotes_are_rejected():
    raw = pd.DataFrame(
        {
            "ts_code": ["H00300.CSI"] * 2,
            "trade_date": ["20041231", "20050104"],
            "close": [1000.0, 982.794],
            "pre_close": [np.nan, 1000.0],
        }
    )
    result = normalize_series(raw, "H00300.CSI")
    assert pd.isna(result.pre_close.iloc[0])
    for column, value in [("close", 0.0), ("close", np.nan), ("pre_close", 0.0)]:
        changed = raw.copy()
        changed.loc[1, column] = value
        with pytest.raises(ValueError):
            normalize_series(changed, "H00300.CSI")
    with pytest.raises(ValueError, match="不连续"):
        normalize_series(raw.assign(pre_close=[np.nan, 999.0]), "H00300.CSI")
    with pytest.raises(ValueError, match="代码不匹配"):
        normalize_series(raw, "000300.SH")
    assert BacktestConfig("2025-01-02", "2025-02-28", benchmark="h00300.csi").benchmark == "H00300.CSI"
    with pytest.raises(ValueError):
        normalize_benchmark("H00300.CSI/../../x")


def test_coverage_requires_initial_anchor_and_exact_calendar():
    dates = pd.bdate_range("2025-01-02", periods=5)
    frame = pd.DataFrame({"date": dates, "close": 100.0})
    with pytest.raises(ValueError, match="期初锚点"):
        validate_coverage(frame.iloc[1:], dates)
    with pytest.raises(ValueError, match="不一致"):
        validate_coverage(frame, dates.delete(2))


def test_benchmark_cache_is_immutable_and_no_network_when_covered(tmp_path):
    dates = pd.bdate_range("2025-01-02", periods=5)
    _, first = seed(tmp_path, dates)
    old = (tmp_path / "H00300.CSI" / first["file"]).read_bytes()

    class Forbidden:
        def query(self, *args, **kwargs):
            raise AssertionError("Unexpected network")

    series, meta = ensure_series("H00300.CSI", dates[1:], root=tmp_path, provider=Forbidden())
    assert len(series) == 4 and meta == first
    _, second = seed(tmp_path, dates, slope=0.3)
    assert first["file"] != second["file"]
    assert (tmp_path / "H00300.CSI" / first["file"]).read_bytes() == old
    path = tmp_path / "H00300.CSI" / second["file"]
    path.write_bytes(b"bad")
    with pytest.raises(ValueError, match="校验失败"):
        load_series("H00300.CSI", tmp_path)


def test_missing_data_does_not_publish_or_replace_an_existing_cache(tmp_path):

    dates = pd.bdate_range("2025-01-02", periods=6)
    seed(tmp_path, dates[:-1])
    pointer = tmp_path / "H00300.CSI/manifest.json"
    before = pointer.read_bytes()

    class Denied:
        def query(self, *args, **kwargs):
            raise TushareProviderError("hidden", category="access_denied")

    with pytest.raises(ValueError, match="access_denied"):
        ensure_series("H00300.CSI", dates, root=tmp_path, provider=Denied())
    assert pointer.read_bytes() == before

    class Hole:
        def query(self, *args, **kwargs):
            close = 100 + np.arange(5) * 0.2
            return pd.DataFrame(
                {
                    "ts_code": "H00300.CSI",
                    "trade_date": dates[:-1].strftime("%Y%m%d"),
                    "close": close,
                    "pre_close": np.r_[99.8, close[:-1]],
                }
            )

    with pytest.raises(ValueError, match="缺少1个"):
        ensure_series("H00300.CSI", dates, root=tmp_path, provider=Hole())
    assert pointer.read_bytes() == before


def test_download_uses_official_settings_even_if_stock_source_is_a_proxy(tmp_path, monkeypatch):

    monkeypatch.setattr(
        TushareSettings, "from_env", classmethod(lambda cls: cls("test-secret", "https://example.test/proxy"))
    )
    dates = pd.bdate_range("2025-01-02", periods=3)
    captured = []

    class Fake:
        def __init__(self, settings):
            captured.append(settings.http_url)

        def query(self, *args, **kwargs):
            return pd.DataFrame(
                {
                    "ts_code": "H00300.CSI",
                    "trade_date": dates.strftime("%Y%m%d"),
                    "close": [100.0, 101.0, 102.0],
                    "pre_close": [99.0, 100.0, 101.0],
                }
            )

    monkeypatch.setattr("finance_quant.data.benchmarks.TushareProvider", Fake)
    ensure_series("H00300.CSI", dates, root=tmp_path)
    assert captured == ["https://api.tushare.pro"]


def test_relative_metrics_match_independent_math_and_handle_missing_zero_risk():

    dates = pd.bdate_range("2025-01-02", periods=4).strftime("%Y-%m-%d")
    equity = [
        {"date": d, "equity": a, "benchmark": b}
        for d, a, b in zip(dates, [1.0, 1.1, 1.045, 1.1495], [1.0, 1.02, 1.01, 1.04], strict=True)
    ]
    result = calculate_performance(equity)["relative_metrics"]
    active = np.diff([1.0, 1.1, 1.045, 1.1495]) / np.array([1.0, 1.1, 1.045]) - np.diff(
        [1.0, 1.02, 1.01, 1.04]
    ) / np.array([1.0, 1.02, 1.01])
    tracking = statistics.stdev(active) * math.sqrt(252)
    assert result["tracking_error"] == pytest.approx(tracking)
    assert result["information_ratio"] == pytest.approx(statistics.mean(active) * 252 / tracking)
    assert result["relative_return"] == pytest.approx(1.1495 / 1.04 - 1)
    same = [{**row, "benchmark": row["equity"]} for row in equity]
    assert calculate_performance(same)["relative_metrics"]["information_ratio"] is None
    missing = copy.deepcopy(equity)
    missing[1]["benchmark"] = None
    assert all(v is None for v in calculate_performance(missing)["relative_metrics"].values())


def test_incremental_download_only_requests_years_with_missing_sessions(tmp_path):
    dates = pd.bdate_range("2024-12-27", "2025-01-03")
    close = 100 + np.arange(len(dates)) * 0.2
    frame = pd.DataFrame({"date": dates, "close": close, "pre_close": np.r_[99.8, close[:-1]]})
    publish_series("H00300.CSI", frame.loc[frame.date.dt.year.eq(2024)], root=tmp_path)
    calls = []

    class Fake:
        def query(self, api, **params):
            calls.append(params)
            selected = frame.loc[frame.date.dt.year.eq(2025)]
            return pd.DataFrame(
                {
                    "ts_code": "H00300.CSI",
                    "trade_date": selected.date.dt.strftime("%Y%m%d"),
                    "close": selected.close,
                    "pre_close": selected.pre_close,
                }
            )

    result, _ = ensure_series("H00300.CSI", dates, root=tmp_path, provider=Fake())
    assert result.date.tolist() == dates.tolist()
    assert len(calls) == 1 and calls[0]["start_date"] == "20250101"


@pytest.mark.parametrize("historical", [False, True])
def test_switch_benchmark_reuses_native_execution_and_preserves_all_strategy_evidence(
    tmp_path, monkeypatch, historical
):
    benchmark_root = tmp_path / "benchmarks"
    monkeypatch.setenv("FINANCE_QUANT_BENCHMARK_ROOT", str(benchmark_root))
    if historical:
        root = tmp_path / "warehouse"
        warehouse_fixture(root)
        doc = request()

        def run(code):
            req = copy.deepcopy(doc)
            if code:
                req["backtest"]["benchmark"] = code
            output = run_historical_research(req, root, tmp_path / "runs")
            return {**output, "run_id": output["backtest_run_id"]}

        calendar = pd.read_parquet(root / "reference/calendar.parquet").date
    else:
        config = ResearchConfig(
            DataConfig(tmp_path / "data", "20241202", "20250228", ("600000.SH", "000001.SZ")),
            BacktestConfig("2025-01-02", "2025-02-28", topk=1),
            tmp_path / "runs",
        )
        download_dataset(config.data, provider=Provider())

        def run(code):
            return run_configured(replace(config, backtest=replace(config.backtest, benchmark=code)))

        cal = load_market(config.data.root)[2]
        calendar = cal.loc[cal.is_open, "date"]
    first = run(None)
    store = ResultStore(tmp_path / "runs")
    path = store.path("backtests", first["run_id"])
    before = path.read_bytes()
    original = store.get("backtests", first["run_id"])
    dates = pd.DatetimeIndex([row["date"] for row in original["equity"]])
    seed(benchmark_root, dates)
    seed(benchmark_root, dates, "000300.SH", slope=0.1)

    def forbidden(*args, **kwargs):
        raise AssertionError("Benchmark switch reran native Qlib")

    monkeypatch.setattr("finance_quant.application.real_workflow.run_qlib", forbidden)
    monkeypatch.setattr("finance_quant.application.historical_workflow.run_qlib", forbidden)
    price, total = run("000300.SH"), run("H00300.CSI")
    assert len({first["run_id"], price["run_id"], total["run_id"]}) == 3
    assert first["factor_run_id"] == price["factor_run_id"] == total["factor_run_id"]
    if historical:
        assert first["evaluation_run_id"] == price["evaluation_run_id"] == total["evaluation_run_id"]
    a, b = [store.get("backtests", r["run_id"]) for r in (price, total)]
    for result in (a, b):
        assert result["metadata"]["execution_run_id"] == first["run_id"]
        for key in ("trades", "orders", "positions", "execution_events"):
            assert result.get(key) == original.get(key)
        assert [{k: v for k, v in row.items() if k != "benchmark"} for row in result["equity"]] == [
            {k: v for k, v in row.items() if k != "benchmark"} for row in original["equity"]
        ]
        assert all(result["summary"]["metrics"][key] == original["summary"]["metrics"][key] for key in METRIC_KEYS)
    assert a["summary"]["metrics"]["benchmark_return"] != b["summary"]["metrics"]["benchmark_return"]
    assert a["summary"]["metrics"]["relative_return"] != b["summary"]["metrics"]["relative_return"]
    assert path.read_bytes() == before
    assert run("H00300.CSI")["run_id"] == total["run_id"]
    assert publish_benchmark_view(tmp_path / "runs", first["run_id"], "H00300.CSI", calendar=calendar)["reused"]
