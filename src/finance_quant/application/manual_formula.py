"""Local registration, fixed-pool execution and replay for a manual formula."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pandas as pd

from finance_quant.application.evaluation_workflow import evaluate_configured
from finance_quant.application.factor_workflow import load_configured_snapshot
from finance_quant.application.real_workflow import run_configured
from finance_quant.configuration import ResearchConfig, parse_config, unique_json_fields
from finance_quant.data.market import FrozenMarketSnapshot, freeze_market_snapshot, load_frozen_snapshot
from finance_quant.data.symbols import to_qlib
from finance_quant.provenance import fingerprint, report_code_sha256, source_provenance
from finance_quant.research.manual_formula import MANUAL_FORMULA, FormulaVersion
from finance_quant.research.neutralization import NeutralizationConfig
from finance_quant.research.registry import FactorConfig, FactorSpec
from finance_quant.storage import ResultStore

FORMULA_ID = re.compile(r"formula-[0-9a-f]{64}")
EVALUATION_ID = re.compile(r"factor-eval-[0-9a-f]{12}")
REPLAY_KEY = "manual_formula_replay"


class ManualFormulaError(ValueError):
    def __init__(self, stage: str, code: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.code = code


@contextmanager
def formula_stage(stage: str, code: str, message: str) -> Iterator[None]:
    try:
        yield
    except ManualFormulaError:
        raise
    except Exception as error:
        raise ManualFormulaError(stage, code, message) from error


def register_formula(input_path: Path, store: ResultStore) -> dict:
    with formula_stage("formula", "invalid_formula", "Use a JSON price_return formula with an integer window 2–252."):
        with input_path.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError("Formula input exceeds 4 KiB")
        version = FormulaVersion.from_input(json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_json_fields))
    with formula_stage("registration", "formula_store_conflict", "Unable to publish the immutable formula version."):
        path = store.publish("formulas", version.id, version.as_dict())
        FormulaVersion.from_record(store.get("formulas", version.id))
    return {"formula_id": version.id, "formula_version": version.as_dict(), "record": str(path)}


def _registered_formula(store: ResultStore, formula_id: str) -> FormulaVersion:
    with formula_stage("formula", "formula_version_unavailable", "The registered formula is missing or has changed."):
        if not FORMULA_ID.fullmatch(formula_id):
            raise ValueError("Invalid formula ID")
        version = FormulaVersion.from_record(store.get("formulas", formula_id))
        if version.id != formula_id:
            raise ValueError("Formula ID does not match its content")
    return version


def _validate_price_only(config: ResearchConfig) -> None:
    if (
        config.financials is not None
        or config.neutralization != NeutralizationConfig()
        or config.backtest.benchmark is not None
    ):
        raise ValueError("Manual formulas use only the frozen market leaf")


def _validate_inputs(config: ResearchConfig, frozen: FrozenMarketSnapshot) -> None:
    market, benchmark, calendar, manifest = load_configured_snapshot(config, frozen_snapshot=frozen)
    if manifest.get("universe_type", "fixed_user_selection") != "fixed_user_selection":
        raise ValueError("Manual formula requires a fixed stock pool")
    if set(market["instrument"]) != {to_qlib(code) for code in config.data.codes} or set(benchmark["instrument"]) != {
        to_qlib(config.data.benchmark)
    }:
        raise ValueError("Market or benchmark records do not match the frozen selection")
    open_dates = pd.DatetimeIndex(calendar.loc[calendar["is_open"], "date"])
    in_range = open_dates[
        (open_dates >= pd.Timestamp(config.data.start)) & (open_dates <= pd.Timestamp(config.data.end))
    ]
    if not pd.DatetimeIndex(sorted(market["date"].unique())).equals(in_range):
        raise ValueError("Market dates do not match the frozen exchange sessions")


def _recipe(config: ResearchConfig, snapshot: FrozenMarketSnapshot) -> dict:
    document = config.as_dict()
    del document["runs_dir"]
    document["data"]["root"] = snapshot.relative_path.as_posix()
    return {"schema_version": 1, "snapshot": snapshot.binding(), "config": document}


def _execute(config: ResearchConfig, frozen: FrozenMarketSnapshot, version: FormulaVersion) -> dict:
    store = ResultStore(config.runs_dir)
    with formula_stage(
        "backtest", "backtest_failed", "Backtest failed. Check the frozen range, warm-up and trade inputs."
    ):
        backtest = run_configured(config, result_store=store, frozen_snapshot=frozen)
    with formula_stage("evaluation", "evaluation_failed", "Evaluation failed on the frozen inputs."):
        evaluation = evaluate_configured(
            config, result_store=store, frozen_snapshot=frozen, replay_recipe=_recipe(config, frozen)
        )
    return {
        "formula_id": version.id,
        "data_version": frozen.inputs[3]["id"],
        "factor_run_id": evaluation["factor_run_id"],
        "backtest_id": backtest["run_id"],
        "evaluation_id": evaluation["run_id"],
    }


def run_formula(config: ResearchConfig, formula_id: str, *, data_root: Path) -> dict:
    version = _registered_formula(ResultStore(config.runs_dir), formula_id)
    with formula_stage(
        "configuration", "unsupported_configuration", "Use the frozen market benchmark and price-only inputs."
    ):
        _validate_price_only(config)
        if config.factors != FactorConfig():
            raise ValueError("The formula command owns factor selection")
        if source_provenance()["dependency_lock_sha256"] is None:
            raise ValueError("The project dependency lock is unavailable")
    with formula_stage(
        "snapshot", "snapshot_unavailable", "The selected fixed-pool market leaf is missing, invalid or changed."
    ):
        frozen = freeze_market_snapshot(data_root, config.data.root)
        config = replace(
            config,
            data=replace(config.data, root=frozen.root),
            factors=FactorConfig((FactorSpec(MANUAL_FORMULA, 1, formula_version=version),)),
        )
        _validate_inputs(config, frozen)
    return _execute(config, frozen, version)


def replay_formula(evaluation_id: str, *, store: ResultStore, data_root: Path, output: Path) -> dict:
    with formula_stage(
        "evaluation_record", "invalid_evaluation_record", "The evaluation has no valid manual-formula recipe."
    ):
        if not EVALUATION_ID.fullmatch(evaluation_id):
            raise ValueError("Invalid evaluation ID")
        metadata = store.get("evaluations", evaluation_id, hydrate=False)["metadata"]
        provenance = metadata["provenance"]
        recipe = provenance[REPLAY_KEY]
        if (
            metadata["id"] != evaluation_id
            or "factor-eval-" + fingerprint(provenance) != evaluation_id
            or type(recipe["schema_version"]) is not int
            or recipe["schema_version"] != 1
        ):
            raise ValueError("Evaluation ID or replay recipe version mismatch")
        snapshot = recipe["snapshot"]
        if Path(snapshot["snapshot_dir"]).is_absolute():
            raise ValueError("Replay requires a managed relative leaf")
        config = parse_config({**recipe["config"], "runs_dir": str(output.resolve())}, data_root.resolve())
        _validate_price_only(config)
        (spec,) = config.factors.specs
        version = spec.formula_version
        if not isinstance(version, FormulaVersion):
            raise ValueError("Missing frozen formula version")
    with formula_stage(
        "runtime",
        "runtime_mismatch",
        "Replay requires the original computation code, dependency lock and Python version.",
    ):
        current = {**source_provenance(), "report_code_sha256": report_code_sha256()}
        if current["dependency_lock_sha256"] is None or any(
            provenance.get(key) != value for key, value in current.items()
        ):
            raise ValueError("Replay runtime does not match recorded provenance")
    _registered_formula(store, version.id)
    with formula_stage("snapshot", "snapshot_unavailable", "The frozen market leaf is missing, invalid or changed."):
        frozen = load_frozen_snapshot(data_root, snapshot)
        _validate_inputs(config, frozen)
        if _recipe(config, frozen) != recipe:
            raise ValueError("Replay configuration differs from the canonical recipe")
    with formula_stage("output", "output_unavailable", "Replay requires a new output directory."):
        config.runs_dir.mkdir(parents=True, exist_ok=False)
    return _execute(config, frozen, version)
