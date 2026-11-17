import json
import math

import numpy as np
import pandas as pd
import pytest

from finance_quant.configuration import EvaluationConfig, FactorConfig, FactorSpec, load_config
from finance_quant.research.market_factors import MARKET_WEIGHTS, market_factors


def sample_market(days=40):
    records = []
    for index, code in enumerate(["SH600000", "SZ000001", "SH600519"]):
        for step, date in enumerate(pd.bdate_range("2025-01-02", periods=days)):
            records.append(
                {
                    "instrument": code,
                    "date": date,
                    "close": 10 + index + step * 0.03 + np.sin(step * 0.4 + index) * 0.2,
                    "adj_factor": 1.0,
                    "pe_ttm": 10 + index * 5,
                    "name": code,
                    "industry": "test",
                }
            )
    return pd.DataFrame(records)


def window_market(days=40):
    records = []
    for index, code in enumerate(["SH600000", "SZ000001", "SH600519"]):
        for step, date in enumerate(pd.bdate_range("2025-01-02", periods=days)):
            if index == 0:
                close = 10.0 if step < 30 else 13.0
            elif index == 1:
                close = 10.0 * 1.02**step
            else:
                close = 10.0 - 0.01 * step
            records.append(
                {
                    "instrument": code,
                    "date": date,
                    "close": close,
                    "adj_factor": 1.0,
                    "pe_ttm": 10 + index * 5,
                    "name": code,
                    "industry": "test",
                }
            )
    return pd.DataFrame(records)


def valid_document():
    return {
        "data": {
            "root": "data",
            "start": "20250101",
            "end": "20250331",
            "codes": ["600000.SH", "000001.SZ"],
        },
        "backtest": {"start": "20250203", "end": "20250331"},
    }


def write_document(tmp_path, document):
    path = tmp_path / "research.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_default_config_preserves_weights_and_records_earnings_missing_policy():
    config = FactorConfig()
    assert config.weights == {"momentum": 0.4, "value": 0.3, "volatility": 0.3}
    assert MARKET_WEIGHTS == config.weights
    assert MARKET_WEIGHTS is not FactorConfig().weights
    assert config.as_list() == [
        {"name": "momentum", "weight": 0.4, "window": 20},
        {"name": "value", "weight": 0.3, "window": None, "missing_policy": "zero"},
        {"name": "volatility", "weight": 0.3, "window": 10},
    ]

    market = sample_market()
    implicit = market_factors(market, "fixture")
    explicit = market_factors(market, "fixture", FactorConfig())
    assert implicit == explicit
    assert implicit["metadata"]["weights"] == config.weights
    assert implicit["metadata"]["definitions"]["momentum"] == "历史20个交易日复权收盘收益率"
    assert implicit["metadata"]["direction"] == "动量和盈利收益率越高得分越高；波动率越低得分越高。分数不是上涨概率。"


def test_config_orders_specs_deterministically_and_hashes_canonical_payload():
    config = FactorConfig(
        [
            FactorSpec("volatility", 0.3, 10),
            FactorSpec("momentum", 0.4, 20),
            FactorSpec("value", 0.3),
        ]
    )
    assert tuple(spec.name for spec in config.specs) == ("momentum", "value", "volatility")
    assert [item["name"] for item in config.as_list()] == ["momentum", "value", "volatility"]
    assert list(config.weights) == ["momentum", "value", "volatility"]
    assert config.as_list() == FactorConfig().as_list()
    assert config.scheme == FactorConfig().scheme
    assert len(config.scheme) == len("market-") + 12


def test_weighted_composite_scores_use_configured_weights():
    config = FactorConfig(
        (
            FactorSpec("momentum", 0.5, 10),
            FactorSpec("value", 0.25),
            FactorSpec("volatility", 0.25, 5),
        )
    )
    result = market_factors(sample_market(), "fixture", config)
    assert result["metadata"]["weights"] == {"momentum": 0.5, "value": 0.25, "volatility": 0.25}
    assert result["history"]
    for row in result["history"]:
        expected = sum(row["scores"][name] * weight for name, weight in config.weights.items())
        assert row["composite"] == pytest.approx(expected)
        assert set(row["raw"]) == {"momentum", "value", "volatility"}


def test_single_momentum_factor_allows_missing_valuations():
    market = sample_market()
    market["pe_ttm"] = np.nan
    config = FactorConfig((FactorSpec("momentum", 1.0),))
    result = market_factors(market, "fixture", config)

    assert result["history"]
    assert result["metadata"]["weights"] == {"momentum": 1.0}
    assert result["metadata"]["definitions"] == {"momentum": "历史20个交易日复权收盘收益率"}
    assert all(set(row["raw"]) == {"momentum"} for row in result["history"])
    assert all(set(row["scores"]) == {"momentum"} for row in result["history"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "quality", "weight": 1.0},
        {"name": "momentum", "weight": 0},
        {"name": "momentum", "weight": -0.1},
        {"name": "momentum", "weight": 1.1},
        {"name": "momentum", "weight": True},
        {"name": "momentum", "weight": float("nan")},
        {"name": "momentum", "weight": float("inf")},
        {"name": "value", "weight": 0.3, "window": 5},
        {"name": "momentum", "weight": 0.4, "window": 1},
        {"name": "momentum", "weight": 0.4, "window": 253},
        {"name": "momentum", "weight": 0.4, "window": True},
        {"name": "momentum", "weight": 0.4, "window": 2.5},
    ],
)
def test_invalid_factor_specs_are_rejected(kwargs):
    with pytest.raises(ValueError):
        FactorSpec(**kwargs)


@pytest.mark.parametrize(
    "specs",
    [
        (),
        (FactorSpec("momentum", 0.5), FactorSpec("momentum", 0.5)),
        (FactorSpec("momentum", 0.5), FactorSpec("value", 0.4)),
        ("momentum",),
    ],
)
def test_invalid_factor_configs_are_rejected(specs):
    with pytest.raises(ValueError):
        FactorConfig(specs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"horizons": ()},
        {"horizons": (1, 1)},
        {"horizons": (0,)},
        {"horizons": (253,)},
        {"horizons": (True,)},
        {"horizons": (1.5,)},
        {"groups": True},
        {"groups": 1},
        {"groups": 11},
        {"groups": 3.0},
        {"groups": 5, "min_cross_section": 4},
        {"min_cross_section": 2},
        {"min_cross_section": 100001},
    ],
)
def test_invalid_evaluation_configs_are_rejected(kwargs):
    with pytest.raises(ValueError):
        EvaluationConfig(**kwargs)


def test_load_config_defaults_and_canonical_sections(tmp_path):
    loaded = load_config(write_document(tmp_path, valid_document()))
    assert loaded.factors.as_list() == FactorConfig().as_list()
    assert loaded.evaluation.as_dict() == {"horizons": [1, 5, 10], "groups": 3, "min_cross_section": 3}
    document = loaded.as_dict()
    assert document["factors"] == FactorConfig().as_list()
    assert document["evaluation"] == {"horizons": [1, 5, 10], "groups": 3, "min_cross_section": 3}
    json.dumps(document, allow_nan=False)


def test_load_config_accepts_strict_factor_and_evaluation_sections(tmp_path):
    document = valid_document()
    document["factors"] = [
        {"name": "momentum", "weight": 0.7, "window": 12},
        {"name": "value", "weight": 0.3},
    ]
    document["evaluation"] = {"horizons": [10, 1, 5], "groups": 4, "min_cross_section": 6}
    loaded = load_config(write_document(tmp_path, document))
    assert loaded.factors.as_list() == [
        {"name": "momentum", "weight": 0.7, "window": 12},
        {"name": "value", "weight": 0.3, "window": None, "missing_policy": "zero"},
    ]
    assert loaded.evaluation.horizons == (1, 5, 10)
    assert loaded.evaluation.groups == 4
    assert loaded.evaluation.min_cross_section == 6


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc: doc.update(factors={"name": "momentum", "weight": 1.0}),
        lambda doc: doc.update(factors=[{"name": "momentum", "weight": 1.0, "extra": 1}]),
        lambda doc: doc.update(factors=[{"name": "momentum"}]),
        lambda doc: doc.update(factors=[{"name": "quality", "weight": 1.0}]),
        lambda doc: doc.update(factors=[{"name": "momentum", "weight": True}]),
        lambda doc: doc.update(factors=[{"name": "value", "weight": 0.5, "window": 5}]),
        lambda doc: doc.update(evaluation={"extra": 1}),
        lambda doc: doc.update(evaluation={"horizons": 5}),
        lambda doc: doc.update(evaluation={"groups": True}),
    ],
)
def test_load_config_rejects_unknown_and_invalid_sections(tmp_path, mutate):
    document = valid_document()
    mutate(document)
    with pytest.raises(ValueError):
        load_config(write_document(tmp_path, document))


def test_changed_window_changes_scores_and_scheme():
    base = FactorConfig(
        (
            FactorSpec("momentum", 0.6, 20),
            FactorSpec("value", 0.2),
            FactorSpec("volatility", 0.2, 10),
        )
    )
    shortened = FactorConfig(
        (
            FactorSpec("momentum", 0.6, 5),
            FactorSpec("value", 0.2),
            FactorSpec("volatility", 0.2, 10),
        )
    )
    assert base.scheme != shortened.scheme
    market = window_market()
    baseline = market_factors(market, "fixture", base)
    altered = market_factors(market, "fixture", shortened)
    base_rows = {(row["date"], row["instrument"]): row for row in baseline["history"]}
    changed_rows = {(row["date"], row["instrument"]): row for row in altered["history"]}
    common = sorted(set(base_rows) & set(changed_rows))
    assert common
    assert any(base_rows[key]["raw"]["momentum"] != changed_rows[key]["raw"]["momentum"] for key in common)
    assert any(base_rows[key]["scores"]["momentum"] != changed_rows[key]["scores"]["momentum"] for key in common)


def test_future_prices_do_not_change_earlier_scores():
    config = FactorConfig(
        (
            FactorSpec("momentum", 0.5, 10),
            FactorSpec("volatility", 0.5, 5),
        )
    )
    market = sample_market()
    baseline = market_factors(market, "fixture", config)
    changed = market.copy()
    future = changed["date"] > "2025-02-10"
    changed.loc[future, "close"] *= 2
    changed.loc[future, "adj_factor"] *= 3
    changed.loc[future, "pe_ttm"] *= 4
    altered = market_factors(changed, "fixture", config)

    before = lambda result: [row for row in result["history"] if row["date"] <= "2025-02-10"]
    assert before(baseline) == before(altered)
    assert math.isclose(sum(baseline["metadata"]["weights"].values()), 1.0)
