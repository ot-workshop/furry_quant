import json
from dataclasses import replace

import pandas as pd
import pytest
from test_real_workflow import sample_market

from finance_quant.application import manual_formula
from finance_quant.application.evaluation_workflow import evaluate_configured
from finance_quant.application.real_workflow import run_configured
from finance_quant.checksums import canonical_json_sha256, file_sha256
from finance_quant.configuration import BacktestConfig, DataConfig, ResearchConfig
from finance_quant.data.managed import atomic_json
from finance_quant.data.market import write_market_snapshot_files
from finance_quant.manual_formula import main
from finance_quant.research.manual_formula import MANUAL_FORMULA, FormulaVersion
from finance_quant.research.market_factors import market_factors
from finance_quant.research.registry import EvaluationConfig, FactorConfig, FactorSpec
from finance_quant.storage import ResultStore

A, B, C = "SH600000", "SZ000001", "SH600519"
CODES = ("600000.SH", "000001.SZ", "600519.SH")
SIGNAL, ENTRY, END = "2025-01-07", "2025-01-08", "2025-01-10"


def _prices():
    market, benchmark = sample_market()
    expected = {A: (9.90, 9.60, 9.80, 10), B: (9.40, 9.90, 9.95, 10), C: (9.98, 9.80, 9.92, 10)}
    for instrument, closes in expected.items():
        index = market.index[market["instrument"] == instrument]
        prices = [*closes, *(10 + 0.05 * i for i in range(1, len(index) - 3))]
        for field in ("close", "open", "high", "low"):
            market.loc[index, field] = prices
    # Both factors must use adjusted closes. A trades at half its prior nominal price after the split.
    post_split = (market["instrument"] == A) & (market["date"] >= SIGNAL)
    market.loc[post_split, "adj_factor"] = 2.0
    market.loc[post_split, ["open", "close", "high", "low"]] /= 2.0
    return market, benchmark


def _publish(batch, version, days):
    market, benchmark = _prices()
    dates = benchmark["date"].iloc[:days]
    leaf = batch / "snapshots" / version
    leaf.mkdir(parents=True)
    files = write_market_snapshot_files(
        leaf,
        market.loc[market["date"].isin(dates)],
        benchmark.iloc[:days],
        pd.DataFrame({"date": dates, "is_open": True}),
    )
    manifest = {
        "schema_version": "market-v2",
        "id": version,
        "snapshot_path": f"snapshots/{version}",
        "universe_type": "fixed_user_selection",
        "start": dates.iloc[0].strftime("%Y%m%d"),
        "end": dates.iloc[-1].strftime("%Y%m%d"),
        "codes": list(CODES),
        "benchmark": "000300.SH",
        "files": files,
    }
    atomic_json(leaf / "manifest.json", manifest)
    atomic_json(batch / "manifest.json", manifest)
    return leaf, manifest


def _rows(artifact, name):
    return {
        (row["date"], row["instrument"]): (row["raw"][name], row["scores"][name], row["rank"])
        for row in artifact["history"]
    }


def _ranking(artifact):
    return [
        row["instrument"] for row in sorted(artifact["history"], key=lambda row: row["rank"]) if row["date"] == SIGNAL
    ]


def test_manual_formula_cli_parity_version_change_frozen_replay_and_tamper(tmp_path, capsys, monkeypatch):
    data = tmp_path / "data"
    batch = data / "research" / "downloads" / "three"
    leaf, manifest = _publish(batch, "original", 7)
    store = ResultStore(tmp_path / "runs")
    config = ResearchConfig(
        DataConfig(batch, manifest["start"], manifest["end"], CODES),
        BacktestConfig(ENTRY, END, topk=1, open_cost=0.002),
        store.root,
        evaluation=EvaluationConfig(horizons=(1,), groups=3),
    )
    config_path = tmp_path / "research.json"
    atomic_json(config_path, config.as_dict())
    formula = tmp_path / "formula.json"

    def command(*args, error=None):
        capsys.readouterr()
        status = main(["--runs-dir", str(store.root), *args])
        captured = capsys.readouterr()
        if error:
            assert status == 1 and error in captured.err
            assert not captured.out
            return
        assert status == 0, captured.err
        return json.loads(captured.out.splitlines()[-1])

    def register(window):
        atomic_json(formula, {"operator": "price_return", "window": window})
        return command("register", str(formula))

    runs = ("run", "--data-root", str(data), "--config", str(config_path))
    version1 = register(2)
    record = store.path("formulas", version1["formula_id"])
    recorded_sha = file_sha256(record)
    formula.write_text(' { "window": 2, "operator": "price_return" } ', encoding="utf-8")
    assert version1 == command("register", str(formula))
    assert file_sha256(record) == recorded_sha
    assert canonical_json_sha256(
        {k: v for k, v in version1["formula_version"].items() if k != "sha256"}
    ) == record.parent.name.removeprefix("formula-")
    first = command(*runs, "--formula-id", version1["formula_id"])

    momentum = replace(config, factors=FactorConfig((FactorSpec("momentum", 1, 2, skip=0),)))
    benchmark_run = run_configured(momentum)
    benchmark_eval = evaluate_configured(momentum)
    built_in = store.get("factors", benchmark_run["factor_run_id"])
    scores = store.get("factors", first["factor_run_id"])
    assert _rows(scores, MANUAL_FORMULA) == _rows(built_in, "momentum")
    assert _ranking(scores) == [A, C, B]
    assert _rows(scores, MANUAL_FORMULA)[(SIGNAL, A)][0] == pytest.approx(10 / 9.60 - 1)
    original_backtest = store.get("backtests", first["backtest_id"])
    builtin_backtest = store.get("backtests", benchmark_run["run_id"])
    for key in ("trades", "positions", "equity"):
        assert original_backtest[key] == builtin_backtest[key]
    assert (original_backtest["trades"][0]["date"], original_backtest["trades"][0]["instrument"]) == (ENTRY, A)
    original_evaluation = store.get("evaluations", first["evaluation_id"])
    builtin_evaluation = store.get("evaluations", benchmark_eval["run_id"])
    assert original_evaluation["labels"] == builtin_evaluation["labels"]
    assert original_evaluation["labels"]["1"]["valid_observations"] > 0
    assert original_evaluation["factors"][MANUAL_FORMULA] == builtin_evaluation["factors"]["momentum"]
    assert original_evaluation["factors"]["composite"] == builtin_evaluation["factors"]["composite"]
    assert "manual_formula | 100.0% | 2" in store.path("evaluations", first["evaluation_id"]).with_name(
        "report.md"
    ).read_text(encoding="utf-8")

    version2 = register(3)
    second = command(*runs, "--formula-id", version2["formula_id"])
    assert first["formula_id"] != second["formula_id"] and first["factor_run_id"] != second["factor_run_id"]
    assert _ranking(store.get("factors", second["factor_run_id"])) == [B, A, C]
    other = store.get("backtests", second["backtest_id"])
    assert (other["trades"][0]["date"], other["trades"][0]["instrument"]) == (ENTRY, B)
    assert not any(row["date"] < SIGNAL for row in store.get("factors", second["factor_run_id"])["history"])

    digest = store.digest("evaluations", first["evaluation_id"])
    earlier = store.digest("backtests", first["backtest_id"])
    snapshot = scores["metadata"]["provenance"]["frozen_snapshot"]
    assert snapshot["snapshot_dir"] == leaf.relative_to(data).as_posix()
    assert snapshot["manifest_sha256"] == file_sha256(leaf / "manifest.json")
    assert snapshot["files"] == {name: file_sha256(leaf / name) for name in manifest["files"]}
    assert original_backtest["metadata"]["provenance"]["frozen_snapshot"] == snapshot
    assert original_evaluation["metadata"]["provenance"]["manual_formula_replay"]["snapshot"] == snapshot
    assert not store.get("evaluations", benchmark_eval["run_id"])["metadata"]["provenance"].get("manual_formula_replay")

    _, current = _publish(batch, "current", 10)
    atomic_json(config_path, replace(config, data=replace(config.data, end=current["end"])).as_dict())
    latest = command(*runs, "--formula-id", version1["formula_id"])
    newest = _rows(store.get("factors", latest["factor_run_id"]), MANUAL_FORMULA)
    assert latest["data_version"] == "current"
    assert {key: newest[key] for key in _rows(scores, MANUAL_FORMULA)} == _rows(scores, MANUAL_FORMULA)

    replay = ("replay", "--data-root", str(data), "--evaluation-id", first["evaluation_id"])
    output = tmp_path / "replay"
    assert command(*replay, "--output", str(output)) == first
    restored = ResultStore(output)
    for key in ("trades", "positions", "equity"):
        assert restored.get("backtests", first["backtest_id"])[key] == original_backtest[key]
    recomputed = restored.get("evaluations", first["evaluation_id"])
    assert recomputed["factors"] == original_evaluation["factors"]
    assert recomputed["metadata"]["provenance"] == original_evaluation["metadata"]["provenance"]
    assert store.digest("evaluations", first["evaluation_id"]) == digest
    assert store.digest("backtests", first["backtest_id"]) == earlier
    command(*replay, "--output", str(output), error="output_unavailable")

    invalid_output = tmp_path / "invalid"
    for path in (leaf / "manifest.json", leaf / "market.parquet"):
        contents = path.read_bytes()
        path.write_bytes(contents + b" ")
        command(*replay, "--output", str(invalid_output), error="snapshot_unavailable")
        path.unlink()
        command(*replay, "--output", str(invalid_output), error="snapshot_unavailable")
        path.write_bytes(contents)
    formula_record = record.read_bytes()
    atomic_json(record, version2["formula_version"])
    command(*replay, "--output", str(invalid_output), error="formula_version_unavailable")
    record.unlink()
    command(*replay, "--output", str(invalid_output), error="formula_version_unavailable")
    record.write_bytes(formula_record)
    source = manual_formula.source_provenance()
    monkeypatch.setattr(manual_formula, "source_provenance", lambda: {**source, "dependency_lock_sha256": "changed"})
    command(*replay, "--output", str(invalid_output), error="runtime_mismatch")
    command(
        "replay",
        "--data-root",
        str(data),
        "--evaluation-id",
        benchmark_eval["run_id"],
        "--output",
        str(invalid_output),
        error="invalid_evaluation_record",
    )
    assert not invalid_output.exists()
    assert store.digest("evaluations", first["evaluation_id"]) == digest
    receipts = [
        json.loads(path.read_text(encoding="utf-8")) for path in store.root.glob("formula_failures/*/result.json")
    ]
    assert {receipt["stage"] for receipt in receipts} == {
        "output",
        "snapshot",
        "formula",
        "runtime",
        "evaluation_record",
    }
    assert all(receipt["status"] == "failed" and receipt["operation"] == "replay" for receipt in receipts)


@pytest.mark.parametrize(
    "document",
    [
        '{"operator":"price_return","window":-2}',
        '{"operator":"price_return","window":253}',
        '{"operator":"price_return","window":2.0}',
        '{"operator":"price_return","window":true}',
        '{"operator":"price_return","window":2,"window":3}',
        '{"operator":"price_return","window":2,"expression":"untrusted text"}',
        '{"operator":"other","window":2}',
        '"untrusted text"',
    ],
)
def test_formula_registration_rejects_unsupported_json(tmp_path, capsys, document):
    formula = tmp_path / "formula.json"
    formula.write_text(document, encoding="utf-8")
    assert main(["--runs-dir", str(tmp_path / "runs"), "register", str(formula)]) == 1
    assert "invalid_formula" in capsys.readouterr().err
    assert not (tmp_path / "runs" / "formulas").exists()


def test_manual_factor_spec_owns_window_and_roundtrips():
    version = FormulaVersion(252)
    assert FormulaVersion.from_record(version.as_dict()) == version
    spec = FactorSpec(MANUAL_FORMULA, 1, formula_version=version)
    assert spec.lookback == 252 and spec.missing_policy == "exclude"
    assert FactorSpec(**spec.as_dict()) == spec
    with pytest.raises(ValueError, match="belongs to"):
        FactorSpec(MANUAL_FORMULA, 1, window=3, formula_version=version)
    with pytest.raises(ValueError, match="evaluator"):
        FormulaVersion.from_record({**version.as_dict(), "operation_version": "future"})
    with pytest.raises(ValueError, match="exclude"):
        FactorSpec(MANUAL_FORMULA, 1, missing_policy="zero", formula_version=version)


def test_no_warmup_score_for_incomplete_formula_window():
    market, _ = _prices()
    config = FactorConfig((FactorSpec(MANUAL_FORMULA, 1, formula_version=FormulaVersion(3)),))
    result = market_factors(market, "fixture", config)
    assert min(row["date"] for row in result["history"]) == SIGNAL
