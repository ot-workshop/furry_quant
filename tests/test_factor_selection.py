"""Known-number selection, safe prescreening, exact solver and maturity regressions."""

import copy
import itertools
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from finance_quant.research.factor_selection import SelectionParameters, select_factors


def sample(quality=None, correlation=None):
    quality = quality or {"A": 0.06, "B": 0.055, "C": 0.04, "D": 0.035}
    dates = pd.bdate_range("2025-01-02", periods=14).strftime("%Y-%m-%d").tolist()
    factors = {}
    for key, mean in quality.items():
        daily = [
            {
                "date": dates[index],
                "exit_date": dates[index + 2],
                "observations": 55,
                "rank_ic": mean + (0.01 if index % 2 == 0 else -0.01),
                "ic": mean,
            }
            for index in range(10)
        ]
        factors[key] = {"horizons": {"1": {"daily": daily}}}
    pairs = []
    keys = sorted(quality)
    for index, first in enumerate(keys):
        for second in keys[index + 1 :]:
            value = (
                correlation.get((first, second), 0.2)
                if correlation is not None
                else 0.92
                if (first, second) == ("A", "B")
                else 0.2
            )
            pairs.append(
                {
                    "first": first,
                    "second": second,
                    "daily": [{"date": day, "rank_correlation": value} for day in dates[:10]],
                }
            )
    return {"metadata": {"end": dates[-1]}, "factors": factors, "correlations": {"pairs": pairs}}, dates


def parameters(dates, **kwargs):
    return SelectionParameters(horizon=1, cutoff=dates[7], factor_count=3, min_ic=0.03, min_ir=0, min_dates=4, **kwargs)


def test_quality_then_dominance_then_milp_reproduces_the_known_optimal_subset():
    source, dates = sample()
    original = copy.deepcopy(source)
    result = select_factors(source, parameters(dates))
    assert result["status"] == "optimal"
    assert result["selected_keys"] == ["A", "C", "D"]
    assert result["objective_sum_ic"] == pytest.approx(0.135)
    assert result["stages"] == {"input": 4, "quality_passed": 4, "dominance_removed": 1, "milp_candidates": 3}
    weaker = next(row for row in result["candidates"] if row["key"] == "B")
    assert weaker["status"] == "dominated"
    assert result["max_selected_correlation"] == pytest.approx(0.2)
    assert all(row["last_label_exit"] <= dates[7] for row in result["candidates"])
    assert source == original


def test_early_pruning_does_not_make_a_greedy_mistake():
    source, dates = sample({"A": 0.08, "B": 0.06, "C": 0.055, "D": 0.01}, {("A", "B"): 0.9, ("A", "C"): 0.9})
    result = select_factors(source, replace(parameters(dates), factor_count=2))
    assert result["selected_keys"] == ["B", "C"]
    assert result["stages"]["quality_passed"] == 3
    assert next(row for row in result["candidates"] if row["key"] == "D")["status"] == "quality_excluded"


@pytest.mark.parametrize("seed", range(16))
def test_prescreened_milp_matches_exhaustive_search(seed):
    rng = np.random.default_rng(seed)
    qualities = {f"f{index}": float(rng.uniform(0.05, 0.2)) for index in range(6)}
    correlations = {pair: 0.95 if rng.uniform() < 0.45 else 0.1 for pair in itertools.combinations(qualities, 2)}
    source, dates = sample(qualities, correlations)
    count = 1 + seed % 4
    result = select_factors(source, replace(parameters(dates), factor_count=count))
    valid = [
        group
        for group in itertools.combinations(qualities, count)
        if all(correlations[pair] <= 0.6 for pair in itertools.combinations(group, 2))
    ]
    if valid:
        assert result["status"] == "optimal"
        assert result["objective_sum_ic"] == pytest.approx(max(sum(qualities[key] for key in group) for group in valid))
    else:
        assert result["status"] == "infeasible" and result["selected_keys"] == []
        maximum = max(
            len(group)
            for size in range(1, 7)
            for group in itertools.combinations(qualities, size)
            if all(correlations[pair] <= 0.6 for pair in itertools.combinations(group, 2))
        )
        assert result["max_feasible_count"] == maximum


def test_unrealized_labels_and_future_correlations_cannot_influence_the_selection():
    source, dates = sample()
    before = select_factors(source, parameters(dates))
    for factor in source["factors"].values():
        for row in factor["horizons"]["1"]["daily"]:
            if row["exit_date"] > dates[7]:
                row["rank_ic"] = -1
    for pair in source["correlations"]["pairs"]:
        for row in pair["daily"]:
            if row["date"] > dates[7]:
                row["rank_correlation"] = 1
    assert select_factors(source, parameters(dates)) == before
    assert next(row for row in before["candidates"] if row["key"] == "A")["rank_ic"]["valid_dates"] == 6


def test_mean_absolute_correlation_and_unknown_pairs_are_not_mistaken_for_diversification():
    source, dates = sample({"A": 0.06, "B": 0.055})
    daily = source["correlations"]["pairs"][0]["daily"]
    for index, row in enumerate(daily):
        row["rank_correlation"] = 0.9 if index % 2 else -0.9
    result = select_factors(source, replace(parameters(dates), factor_count=2))
    assert result["status"] == "infeasible" and result["max_feasible_count"] == 1
    assert result["pairs"][0]["mean_absolute"] == pytest.approx(0.9)
    source["correlations"]["pairs"][0]["daily"] = daily[:1]
    result = select_factors(source, replace(parameters(dates), factor_count=2))
    assert result["pairs"][0]["reason"] == "相关性样本不足"


def test_undefined_ir_and_insufficient_mature_samples_are_explained():
    source, dates = sample()
    for row in source["factors"]["A"]["horizons"]["1"]["daily"]:
        row["rank_ic"] = 0.1
    result = select_factors(source, parameters(dates))
    excluded = next(row for row in result["candidates"] if row["key"] == "A")
    assert excluded["status"] == "quality_excluded"
    assert any("IR 未定义" in reason for reason in excluded["reasons"])
    early = select_factors(source, replace(parameters(dates), cutoff=dates[1]))
    assert early["max_feasible_count"] == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"factor_count": True},
        {"min_ic": float("nan")},
        {"min_ir": -1},
        {"max_correlation": 1.1},
        {"min_dates": 1},
        {"cutoff": "20250230"},
        {"start": "2030-01-01"},
    ],
)
def test_invalid_selection_parameters_fail(kwargs):
    _, dates = sample()
    with pytest.raises(ValueError):
        replace(parameters(dates), **kwargs)


def test_equivalent_integer_and_float_thresholds_have_the_same_canonical_parameters():
    first = SelectionParameters(1, "2025-01-20", min_ic=0, min_ir=0, max_correlation=1)
    second = SelectionParameters(1, "2025-01-20", min_ic=0.0, min_ir=0.0, max_correlation=1.0)
    assert repr(first.as_dict()) == repr(second.as_dict())
    assert all(isinstance(first.as_dict()[key], float) for key in ("min_ic", "min_ir", "max_correlation"))


def test_repeated_concurrent_web_style_solves_are_consistent():
    source, dates = sample()
    config = parameters(dates)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(select_factors, source, config) for _ in range(40)]
        results = [future.result(timeout=30) for future in futures]
    assert all(result["selected_keys"] == ["A", "C", "D"] for result in results)
