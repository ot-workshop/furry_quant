"""Known-number rolling diagnostics, timing and read-only API regressions."""

import copy
import json

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from finance_quant.research.evaluation_analysis import evaluation_analysis
from finance_quant.storage import ResultStore
from support.fakes import make_app


def artifact():
    dates = pd.bdate_range("2025-01-02", periods=11).strftime("%Y-%m-%d").tolist()
    values = [0.1, None, 0.3, -0.1, 0.5, 0.5, 0.5, None]
    daily = [
        {"date": dates[index], "exit_date": dates[index + 2], "observations": 17, "ic": value, "rank_ic": value}
        for index, value in enumerate(values)
    ]
    return {
        "metadata": {"id": "analysis-fixture"},
        "factors": {name: {"horizons": {"1": {"daily": copy.deepcopy(daily)}}} for name in ("a", "b", "composite")},
        "correlations": {
            "method": "daily Spearman",
            "pairs": [
                {"first": "a", "second": "a", "daily": [{"date": day, "rank_correlation": 1.0} for day in dates[:8]]},
                {
                    "first": "a",
                    "second": "b",
                    "daily": [
                        {"date": day, "rank_correlation": 1.0 if index < 4 else -1.0}
                        for index, day in enumerate(dates[:8])
                    ],
                },
                {"first": "b", "second": "b", "daily": []},
            ],
        },
    }


def test_rolling_matches_sample_statistics_retains_missing_sessions_and_availability():
    source = artifact()
    frozen = copy.deepcopy(source)
    result = evaluation_analysis(source, horizon=1, factor="a", window=3, min_periods=2)
    points = result["series"]
    assert points[1]["ic_mean"] is None and points[1]["ic_count"] == 1
    assert points[2]["ic_mean"] == pytest.approx(0.2)
    assert points[2]["ic_ir"] == pytest.approx(0.2 / np.std([0.1, 0.3], ddof=1))
    # The missing second session still occupies the window: .1 must have rolled out.
    assert points[3]["ic_mean"] == pytest.approx(0.1)
    assert points[3]["rank_ic_count"] == 2
    assert points[6]["ic_mean"] == pytest.approx(0.5) and points[6]["ic_ir"] is None
    assert points[2]["available_on"] == source["factors"]["a"]["horizons"]["1"]["daily"][2]["exit_date"]
    assert points[2]["available_on"] > points[2]["date"]
    assert source == frozen
    json.dumps(result, allow_nan=False)


def test_windows_minimum_and_date_selection_change_actual_numbers_without_resetting_prehistory():
    source = artifact()
    full = evaluation_analysis(source, horizon=1, window=3, min_periods=2)
    longer = evaluation_analysis(source, horizon=1, window=5, min_periods=2)
    assert full["series"][4]["ic_mean"] != longer["series"][4]["ic_mean"]
    strict = evaluation_analysis(source, horizon=1, window=3)
    assert strict["series"][2]["ic_mean"] is None
    selected = evaluation_analysis(source, horizon=1, window=3, min_periods=2, start=full["series"][4]["date"])
    assert selected["series"][0] == full["series"][4]
    assert selected["correlations"]["cells"][1]["value"] == -1
    assert full["correlations"]["cells"][1]["value"] == 0
    assert full["correlations"]["cells"][1]["mean_absolute"] == 1
    assert full["correlations"]["cells"][2]["value"] is None
    assert full["correlations"]["cells"][2]["valid_dates"] == 0


def test_factor_choice_pair_direction_and_later_rows_do_not_rewrite_earlier_rolling_statistics():
    source = artifact()
    for row in source["factors"]["b"]["horizons"]["1"]["daily"]:
        row["ic"] = -row["ic"] if row["ic"] is not None else None
    first = evaluation_analysis(source, horizon=1, factor="a", window=3, min_periods=2)
    second = evaluation_analysis(source, horizon=1, factor="b", window=3, min_periods=2, first="b", second="a")
    assert second["series"][2]["ic_mean"] == -first["series"][2]["ic_mean"]
    assert second["correlations"]["pair"]["daily"] == first["correlations"]["pair"]["daily"]
    source["factors"]["a"]["horizons"]["1"]["daily"][-1]["ic"] = 0.9
    after = evaluation_analysis(source, horizon=1, factor="a", window=3, min_periods=2)
    assert after["series"][:-1] == first["series"][:-1]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window": 1},
        {"window": True},
        {"window": 2521},
        {"min_periods": 1},
        {"window": 3, "min_periods": 4},
        {"factor": "missing"},
        {"horizon": 20},
        {"start": "20250101"},
        {"start": "2025-02-30"},
        {"start": "2025-02-01", "end": "2025-01-01"},
        {"first": "missing"},
    ],
)
def test_invalid_analysis_arguments_are_rejected(kwargs):
    with pytest.raises(ValueError):
        evaluation_analysis(artifact(), **{"horizon": 1, **kwargs})


def test_old_artifact_analysis_api_is_read_only_and_returns_valid_json(tmp_path):
    runs, data = tmp_path / "runs", tmp_path / "data"
    store = ResultStore(runs)
    store.publish("evaluations", "analysis-fixture", artifact())
    original = store.path("evaluations", "analysis-fixture").read_bytes()
    client = TestClient(make_app(runs, data), base_url="http://127.0.0.1")
    path = "/api/v1/evaluations/analysis-fixture/analysis"
    response = client.get(path, params={"horizon": 1, "window": 3, "min_periods": 2})
    assert response.status_code == 200, response.text
    assert response.json()["series"][2]["ic_mean"] == pytest.approx(0.2)
    assert client.get(path, params={"horizon": 1, "min_periods": 61}).status_code == 422
    assert client.get(path, params={"horizon": 1, "factor": "unknown"}).status_code == 422
    empty = client.get(path, params={"horizon": 1, "start": "2030-01-01"})
    assert empty.status_code == 200 and empty.json()["series"] == []
    assert store.path("evaluations", "analysis-fixture").read_bytes() == original


def test_entire_unscored_sessions_are_restored_from_the_exchange_coverage_calendar():
    source = artifact()
    daily = source["factors"]["composite"]["horizons"]["1"]["daily"]
    calendar = [row["date"] for row in daily]
    source["signal_coverage"] = [{"signal_date": day} for day in calendar]
    del daily[1]
    del daily[0]
    result = evaluation_analysis(source, horizon=1, window=3, min_periods=2)
    assert [row["date"] for row in result["series"]] == calendar
    assert result["series"][0]["observations"] == 0
    assert result["series"][0]["ic_count"] == 0
    assert result["series"][2]["ic_mean"] is None
    assert result["series"][3]["ic_mean"] == pytest.approx(0.1)
    source["correlations"]["pairs"][1]["daily"].pop(2)
    result = evaluation_analysis(source, horizon=1, window=3, min_periods=2)
    assert result["correlations"]["pair"]["daily"][2] == {"date": calendar[2], "rank_correlation": None}
