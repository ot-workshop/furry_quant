import json
from dataclasses import replace

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from finance_quant.api import create_app
from finance_quant.application.evaluation_workflow import evaluate_configured
from finance_quant.configuration import BacktestConfig, DataConfig, ResearchConfig
from finance_quant.data.managed import download_dataset
from finance_quant.research.evaluation import evaluate_factors, forward_open_labels
from finance_quant.research.registry import EvaluationConfig, FactorConfig, FactorSpec
from finance_quant.storage import ResultStore
from test_managed_data import Provider


def example():
    sessions = pd.bdate_range("2025-01-02", periods=12)
    quotes, history = [], []
    for index, instrument in enumerate(["SH600000", "SZ000001", "SH600519", "SH600036", "SZ000858"]):
        for day, date in enumerate(sessions):
            # T+1 entry=20, T+2 exit=22,24,...30: known returns .1 through .5.
            opening = 10 if day == 0 else 20 if day == 1 else (22 + 2 * index) * (1 + .01 * (day - 2))
            quotes.append({"instrument": instrument, "date": date, "open": opening, "adj_factor": 1.0})
            history.append({"instrument": instrument, "date": date.strftime("%Y-%m-%d"),
                            "available_at": (date.tz_localize("Asia/Shanghai") + pd.Timedelta(hours=18)).isoformat(),
                            "raw": {"momentum": index + 1.0}, "scores": {"momentum": (index + 1) * 20.0},
                            "composite": (index + 1) * 20.0})
    return pd.DataFrame(quotes), {"metadata": {"weights": {"momentum": 1}}, "history": history}, sessions


def evaluate(quotes=None, factors=None, sessions=None, config=None, end=None):
    base_quotes, base_factors, base_sessions = example()
    return evaluate_factors(quotes if quotes is not None else base_quotes,
                            factors if factors is not None else base_factors,
                            sessions if sessions is not None else base_sessions,
                            start="2025-01-03", end=end or "2025-01-10", config=config or EvaluationConfig(horizons=(1, 3)))


def test_known_next_open_labels_native_qlib_ic_and_directional_groups():
    result = evaluate()
    first = result["factors"]["momentum"]["horizons"]["1"]["daily"][0]
    assert first["date"] == "2025-01-02" and first["entry_date"] == "2025-01-03" and first["exit_date"] == "2025-01-06"
    assert first["ic"] == pytest.approx(1) and first["rank_ic"] == pytest.approx(1)
    assert first["group_counts"] == {"1": 2, "2": 1, "3": 2}
    assert first["group_returns"] == pytest.approx({"1": .15, "2": .3, "3": .45})
    assert first["high_minus_low"] == pytest.approx(.3)
    labels = result["labels"]["1"]["observations"]
    known = sorted(row["forward_return"] for row in labels if row["date"] == "2025-01-02")
    assert known == pytest.approx([.1, .2, .3, .4, .5])
    assert result["methodology"]["ic_engine"] == "qlib.contrib.eva.alpha.calc_ic"
    json.dumps(result, allow_nan=False)


def test_low_volatility_direction_matches_trading_scores():
    quotes, factors, sessions = example()
    factors["metadata"]["weights"] = {"volatility": 1}
    for row in factors["history"]:
        row["raw"] = {"volatility": 6 - row["raw"]["momentum"]}
    result = evaluate(quotes, factors, sessions)
    assert result["factors"]["volatility"]["horizons"]["1"]["daily"][0]["rank_ic"] == pytest.approx(1)


def test_labels_do_not_buy_at_signal_close_and_ignore_future_closes():
    quotes, factors, sessions = example()
    quotes["close"] = 1_000_000.0
    result = evaluate(quotes, factors, sessions)
    quotes["close"] = .01
    assert evaluate(quotes, factors, sessions) == result


def test_known_ic_summary_excludes_undefined_tail_instead_of_treating_it_as_zero():
    quotes, factors, sessions = example()
    quotes = quotes.loc[quotes.instrument.isin(["SH600000", "SZ000001", "SH600519"])].copy()
    factors["history"] = [row for row in factors["history"] if row["instrument"] in set(quotes.instrument)]
    for index, instrument in enumerate(["SH600000", "SZ000001", "SH600519"]):
        quotes.loc[(quotes.instrument == instrument) & (quotes.date == sessions[3]), "open"] = (22 + index * 2) * (1.3 - index * .1)
    result = evaluate(quotes, factors, sessions, EvaluationConfig(horizons=(1,)), end="2025-01-07")
    summary = result["factors"]["momentum"]["horizons"]["1"]["summary"]
    assert summary["rank_ic"]["valid_dates"] == 2
    assert summary["rank_ic"]["mean"] == pytest.approx(0, abs=1e-12)
    assert summary["rank_ic"]["std"] == pytest.approx(2 ** .5)
    assert summary["rank_ic"]["ir"] == pytest.approx(0, abs=1e-12)
    assert summary["rank_ic"]["positive_fraction"] == .5


def test_prices_after_evaluation_end_cannot_change_the_report():
    quotes, factors, sessions = example()
    baseline = evaluate(quotes, factors, sessions)
    quotes.loc[quotes.date > "2025-01-10", "open"] *= 3
    assert evaluate(quotes, factors, sessions) == baseline


def test_missing_sessions_are_not_shortened_or_filled_and_splits_preserve_labels():
    quotes, _, sessions = example()
    base = forward_open_labels(quotes, sessions, 1, "2025-01-17")
    missing = quotes.loc[~((quotes.instrument == "SH600000") & (quotes.date == "2025-01-03"))]
    labels = forward_open_labels(missing, sessions, 1, "2025-01-17")
    row = labels.loc[(labels.instrument == "SH600000") & (labels.date == "2025-01-02")].iloc[0]
    assert pd.isna(row.forward_return) and row.exclusion == "missing_entry_or_exit"
    assert row.entry_date == "2025-01-03" and row.exit_date == "2025-01-06"
    split = quotes.copy()
    mask = (split.instrument == "SH600000") & (split.date >= "2025-01-06")
    split.loc[mask, "open"] /= 2
    split.loc[mask, "adj_factor"] *= 2
    pd.testing.assert_frame_equal(forward_open_labels(split, sessions, 1, "2025-01-17"), base)


def test_tail_labels_stay_excluded_and_never_cross_evaluation_end():
    result = evaluate()
    for horizon, report in result["labels"].items():
        assert all(row["exit_date"] <= "2025-01-10" for row in report["observations"])
        assert report["excluded_observations"] == int(horizon) * 5
        assert any(day["exclusions"].get("outside_evaluation_end") for day in report["coverage"])
    assert result["labels"]["1"]["valid_observations"] == 25
    assert result["labels"]["3"]["valid_observations"] == 15
    # No compounding of overlapping h-session labels is offered as a NAV.
    assert "equity" not in result and "nav" not in result


def test_ties_are_not_split_by_ticker_and_undefined_statistics_remain_null():
    quotes, factors, sessions = example()
    for row in factors["history"]:
        row["raw"]["momentum"] = 1
        row["composite"] = 50
    result = evaluate(quotes, factors, sessions)
    values = result["factors"]["momentum"]["horizons"]["1"]
    assert values["summary"]["ic"]["mean"] is None
    assert values["summary"]["rank_ic"]["ir"] is None
    assert values["daily"][0]["ic_exclusion"] == "constant_score"
    assert all(row["group_returns"] == {"1": None, "2": None, "3": None} for row in values["daily"])
    assert all(pair["mean_rank_correlation"] is None for pair in result["correlations"]["pairs"])
    json.dumps(result, allow_nan=False)


def test_partial_ties_share_groups_and_constant_returns_have_no_ic():
    quotes, factors, sessions = example()
    for row in factors["history"]:
        row["raw"]["momentum"] = 1 if row["instrument"] in {"SH600000", "SZ000001", "SH600519"} else 2
    result = evaluate(quotes, factors, sessions)
    first = result["factors"]["momentum"]["horizons"]["1"]["daily"][0]
    assert first["group_counts"] == {"1": 3, "2": 0, "3": 2}
    quotes["open"] = quotes.date.map({day: 10 + step for step, day in enumerate(sessions)})
    constant_returns = evaluate(quotes, factors, sessions)["factors"]["momentum"]["horizons"]["1"]["daily"][0]
    assert constant_returns["ic_exclusion"] == "constant_return" and constant_returns["ic"] is None
    assert constant_returns["high_minus_low"] == pytest.approx(0)


def test_minimum_cross_section_and_missing_signal_days_are_reported():
    quotes, factors, sessions = example()
    factors["history"] = [row for row in factors["history"] if row["instrument"] in {"SH600000", "SZ000001"} and row["date"] != "2025-01-06"]
    result = evaluate(quotes, factors, sessions)
    assert result["factors"]["momentum"]["horizons"]["1"]["daily"][0]["ic_exclusion"] == "insufficient_cross_section"
    missing = [row for row in result["signal_coverage"] if row["signal_date"] == "2025-01-06"][0]
    assert missing["scored_instruments"] == 0 and missing["universe"] == 5


@pytest.mark.parametrize("availability", ["2025-02-01T18:00:00+08:00", "2025-01-02T18:00:00", None])
def test_late_or_unknown_availability_is_rejected(availability):
    quotes, factors, sessions = example()
    factors["history"][0]["available_at"] = availability
    with pytest.raises(ValueError, match="not available"):
        evaluate(quotes, factors, sessions)


def test_input_boundaries():
    quotes, factors, sessions = example()
    for bad_calendar in [sessions[::-1], sessions.append(sessions[-1:]), sessions.tz_localize("UTC")]:
        with pytest.raises(ValueError, match="calendar"):
            evaluate(quotes, factors, bad_calendar)
    with pytest.raises(ValueError, match="unique"):
        evaluate(pd.concat([quotes, quotes.iloc[:1]]), factors, sessions)
    factors["history"][0]["raw"]["momentum"] = float("inf")
    with pytest.raises(ValueError, match="finite"):
        evaluate(quotes, factors, sessions)


@pytest.fixture
def research_config(tmp_path):
    config = ResearchConfig(DataConfig(tmp_path / "data", "20241202", "20250228", ("600000.SH", "000001.SZ", "600519.SH")),
                            BacktestConfig("2025-01-02", "2025-02-28", topk=1), tmp_path / "runs")
    download_dataset(config.data, provider=Provider())
    return config


def test_reproducible_diagnostics_reports_readonly_api_and_configuration_isolation(research_config, monkeypatch):
    def no_execution(*args, **kwargs):
        raise AssertionError("Evaluation must not run the trading engine")
    monkeypatch.setattr("finance_quant.integrations.qlib_runner.run_qlib", no_execution)
    first = evaluate_configured(research_config)
    assert not first["reused"]
    second = evaluate_configured(research_config)
    assert second["reused"] and second["run_id"] == first["run_id"]
    store = ResultStore(research_config.runs_dir)
    document = store.get("evaluations", first["run_id"])
    markdown = store.path("evaluations", first["run_id"]).with_name("report.md").read_text(encoding="utf-8")
    assert document["metadata"]["factor_run_id"] == first["factor_run_id"]
    assert "T+1" in markdown and "不年化" in markdown and "股票池" in markdown
    fees = replace(research_config, backtest=replace(research_config.backtest, initial_cash=200000))
    assert evaluate_configured(fees)["run_id"] == first["run_id"]
    changed = evaluate_configured(replace(research_config, evaluation=EvaluationConfig(horizons=(2,))))
    assert changed["run_id"] != first["run_id"] and changed["factor_run_id"] == first["factor_run_id"]
    selected = evaluate_configured(replace(research_config, factors=FactorConfig((FactorSpec("momentum", 1, 5),))))
    assert selected["run_id"] != first["run_id"] and selected["factor_run_id"] != first["factor_run_id"]
    client = TestClient(create_app(research_config.runs_dir))
    listing = client.get("/api/v1/evaluations").json()
    assert listing["total"] == 3
    assert client.get(f"/api/v1/evaluations/{first['run_id']}").json() == document
    assert client.get(f"/api/v1/evaluations/{first['run_id']}/report").text == markdown
    assert client.get(f"/api/v1/evaluations/{first['run_id']}/download").json() == document
    assert client.get("/api/v1/evaluations/unknown").status_code == 404
    assert client.post("/api/v1/evaluations").status_code == 405
    assert client.get("/api/v1/evaluations/..%5C..%5C.env").status_code == 404
    with pytest.raises(FileExistsError):
        store.publish_report(first["run_id"], "changed")


def test_evaluation_cli_is_offline_and_does_not_change_active_backtest_config(research_config, monkeypatch, capsys):
    from finance_quant.cli import main
    path = research_config.runs_dir.parent / "config.json"
    path.write_text(json.dumps(research_config.as_dict()), encoding="utf-8")
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    monkeypatch.setattr("sys.argv", ["finance-quant", "evaluate", "--config", str(path)])
    main()
    assert json.loads(capsys.readouterr().out)["run_id"].startswith("factor-eval-")
    assert not (research_config.runs_dir / "active-research.json").exists()
