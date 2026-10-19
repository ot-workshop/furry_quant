import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from finance_quant.configuration import (
    BacktestConfig,
    DataConfig,
    ResearchConfig,
    load_config,
)


def make_data(**overrides):
    values = {
        "root": Path("data"),
        "start": "20260803",
        "end": "20260930",
        "codes": ("600000.SH", "000001.SZ"),
    }
    values.update(overrides)
    return DataConfig(**values)


def make_backtest(**overrides):
    values = {"start": "2026-09-01", "end": "2026-09-30"}
    values.update(overrides)
    return BacktestConfig(**values)


def write_config(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def valid_document():
    return {
        "data": {
            "root": "../data/managed",
            "start": "20260803",
            "end": "20260930",
            "codes": ["600000.SH", "000001.SZ", "600519.SH"],
        },
        "backtest": {"start": "2026-09-01", "end": "2026-09-30"},
        "runs_dir": "../runs",
    }


def test_dates_and_codes_are_canonical_and_immutable():
    data = DataConfig(
        root=Path("data"),
        start="2026-08-03",
        end="20260930",
        codes=("sh600000", "000001.sz", "BJ430047"),
        benchmark="sh000300",
    )
    assert data.start == "20260803"
    assert data.end == "20260930"
    assert data.codes == ("600000.SH", "000001.SZ", "430047.BJ")
    assert data.benchmark == "000300.SH"

    backtest = BacktestConfig(start="20260901", end="2026-09-30")
    assert backtest.start == "2026-09-01"
    assert backtest.end == "2026-09-30"
    with pytest.raises(FrozenInstanceError):
        data.start = "20260101"


@pytest.mark.parametrize(
    "value", ["", "20260230", "2026-13-01", "2026-09-01T00:00:00", "2026091"]
)
def test_invalid_dates_and_times(value):
    with pytest.raises(ValueError):
        make_data(start=value)
    with pytest.raises(ValueError):
        BacktestConfig(start=value, end="2026-12-31")


def test_inverted_ranges_are_rejected():
    with pytest.raises(ValueError):
        make_data(start="20260930", end="20260803")
    with pytest.raises(ValueError):
        BacktestConfig(start="2026-10-01", end="2026-09-30")


@pytest.mark.parametrize(
    "codes",
    [
        (),
        ("600000.SH",),
        ("SH600000", "600000.sh"),
        ("600000.SH", "60000.SZ"),
        ("600000.SH", "HK600000"),
        ("600000.SH", ""),
    ],
)
def test_invalid_codes(codes):
    with pytest.raises(ValueError):
        make_data(codes=codes)


def test_codes_must_be_a_sequence_and_benchmark_is_bounded():
    with pytest.raises(ValueError):
        make_data(codes="600000.SH")
    with pytest.raises(ValueError):
        make_data(benchmark="000016.SH")


@pytest.mark.parametrize("root", ["", Path("")])
def test_empty_paths_are_rejected(root):
    with pytest.raises(ValueError):
        make_data(root=root)
    with pytest.raises(ValueError):
        ResearchConfig(make_data(), make_backtest(), Path(""))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("initial_cash", float("nan")),
        ("initial_cash", float("inf")),
        ("initial_cash", True),
        ("topk", True),
        ("topk", 1.0),
        ("n_drop", False),
        ("n_drop", 0.5),
        ("risk_degree", float("-inf")),
        ("risk_degree", True),
        ("open_cost", float("nan")),
        ("open_cost", False),
        ("close_cost", float("inf")),
        ("min_cost", float("nan")),
        ("min_cost", True),
    ],
)
def test_nonfinite_and_boolean_numbers_are_rejected(field, value):
    with pytest.raises(ValueError):
        make_backtest(**{field: value})


@pytest.mark.parametrize(
    "overrides",
    [
        {"initial_cash": 0},
        {"topk": 0},
        {"n_drop": -1},
        {"n_drop": 3},
        {"risk_degree": 0},
        {"risk_degree": 1.1},
        {"open_cost": -0.1},
        {"open_cost": 1},
        {"close_cost": -0.1},
        {"close_cost": 1},
        {"min_cost": -0.1},
    ],
)
def test_numeric_bounds_are_rejected(overrides):
    with pytest.raises(ValueError):
        make_backtest(**overrides)


def test_numeric_boundaries_are_accepted():
    backtest = make_backtest(
        initial_cash=1,
        topk=3,
        n_drop=3,
        risk_degree=1,
        open_cost=0,
        close_cost=0.999,
        min_cost=0,
    )
    assert backtest.n_drop == backtest.topk == 3


def test_research_bounds_and_topk():
    research = ResearchConfig(
        make_data(), make_backtest(), Path("runs")
    )
    assert research.runs_dir == Path("runs")
    with pytest.raises(ValueError):
        ResearchConfig(
            make_data(), make_backtest(start="2026-08-02"), Path("runs")
        )
    with pytest.raises(ValueError):
        ResearchConfig(
            make_data(), make_backtest(end="2026-10-01"), Path("runs")
        )
    with pytest.raises(ValueError):
        ResearchConfig(make_data(), make_backtest(topk=3), Path("runs"))


def test_as_dict_is_json_serializable_and_canonical(tmp_path):
    research = ResearchConfig(
        make_data(root=tmp_path / "data", start="2026-08-03"),
        make_backtest(),
        tmp_path / "runs",
    )
    document = research.as_dict()
    json.dumps(document, allow_nan=False)
    assert document["data"]["root"] == str(tmp_path / "data")
    assert document["data"]["start"] == "20260803"
    assert document["data"]["codes"] == ["600000.SH", "000001.SZ"]
    assert document["backtest"]["start"] == "2026-09-01"
    assert document["runs_dir"] == str(tmp_path / "runs")


def test_load_config_resolves_paths_relative_to_file(tmp_path, monkeypatch):
    config = write_config(tmp_path / "configs" / "research.json", valid_document())
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    loaded = load_config(config)
    assert loaded.data.root == (tmp_path / "data" / "managed").resolve()
    assert loaded.runs_dir == (tmp_path / "runs").resolve()
    assert loaded.data.benchmark == "000300.SH"
    assert loaded.backtest.initial_cash == 100000.0


def test_runs_dir_defaults_next_to_config(tmp_path):
    document = valid_document()
    document.pop("runs_dir")
    config = write_config(tmp_path / "configs" / "research.json", document)
    assert load_config(config).runs_dir == (tmp_path / "configs" / "runs").resolve()


@pytest.mark.parametrize("level", ["top", "data", "backtest"])
def test_credentials_fields_are_rejected_without_echoing_values(tmp_path, level):
    document = valid_document()
    if level == "top":
        document["tushare_token"] = "super-secret-value"
    elif level == "data":
        document["data"]["api_key"] = "super-secret-value"
    else:
        document["backtest"]["password"] = "super-secret-value"
    config = write_config(tmp_path / "research.json", document)

    with pytest.raises(ValueError) as error:
        load_config(config)
    assert "super-secret-value" not in str(error.value)


@pytest.mark.parametrize(
    ("section", "field"), [("data", "start"), ("data", "root"), ("backtest", "end")]
)
def test_missing_fields_are_clear(tmp_path, section, field):
    document = valid_document()
    del document[section][field]
    with pytest.raises(ValueError, match=field):
        load_config(write_config(tmp_path / "research.json", document))


def test_malformed_json_and_wrong_types_do_not_expose_the_file(tmp_path):
    config = tmp_path / "research.json"
    config.write_text('{"data": {broken: text}', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        load_config(config)
    assert "broken" not in str(error.value)

    document = valid_document()
    document["data"]["codes"] = "600000.SH"
    with pytest.raises(ValueError):
        load_config(write_config(config, document))

    document = valid_document()
    document["backtest"]["topk"] = "2"
    with pytest.raises(ValueError):
        load_config(write_config(config, document))

    with pytest.raises(ValueError):
        load_config(write_config(config, []))


def test_loaded_config_has_no_credential_attributes(tmp_path):
    config = write_config(tmp_path / "research.json", valid_document())
    loaded = load_config(config)
    assert not hasattr(loaded, "token")
    assert not hasattr(loaded.data, "api_key")


def test_utf8_bom_is_supported_and_duplicate_fields_are_rejected(tmp_path):
    path = tmp_path / 'research.json'
    path.write_text(json.dumps(valid_document()), encoding='utf-8-sig')
    assert load_config(path).data.benchmark == '000300.SH'
    path.write_text('{"data":{},"data":{}}', encoding='utf-8')
    with pytest.raises(ValueError, match='duplicate'):
        load_config(path)
