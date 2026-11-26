"""Freeze, execute and replay a two-fold local manual-formula validation."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from stat import S_ISREG
from typing import Any

import pandas as pd
from filelock import FileLock

from finance_quant.application.manual_formula import (
    FORMULA_ID,
    _registered_formula,
    _validate_price_only,
    formula_stage,
)
from finance_quant.application.real_workflow import run_configured
from finance_quant.checksums import canonical_json_sha256, sha256_bytes
from finance_quant.configuration import parse_config, unique_json_fields
from finance_quant.data.market import freeze_market_snapshot, load_frozen_snapshot
from finance_quant.data.symbols import to_qlib
from finance_quant.provenance import source_provenance
from finance_quant.research.manual_formula import FormulaVersion
from finance_quant.research.registry import FactorConfig
from finance_quant.research.rolling_maturity import (
    DIAGNOSTICS,
    PRICE_ONLY,
    RULES,
    SEGMENTS,
    aware,
    causal_scores,
    choose,
    diagnostics,
    formula_config,
    gated_labels,
    labels_for_pool,
    parse_evidence,
    row_summary,
    trading_windows,
    with_scores,
)
from finance_quant.storage import ResultStore

ROLLING_ID = re.compile(r"rolling-[0-9a-f]{64}")


def _identity(plan) -> str:
    return "rolling-" + canonical_json_sha256(plan)


def _sessions(frozen, config):
    market, benchmark, calendar, manifest = frozen.inputs
    _validate_price_only(config)
    if (
        config.factors != FactorConfig()
        or config.evaluation != DIAGNOSTICS
        or manifest.get("universe_type", "fixed_user_selection") != "fixed_user_selection"
        or set(manifest["codes"]) != set(config.data.codes)
        or manifest["start"] != config.data.start
        or manifest["end"] != config.data.end
        or manifest.get("benchmark", "000300.SH") != config.data.benchmark
        or manifest.get("cross_validation", {}).get("mode", "off") != config.data.validation.mode
        or set(market["instrument"]) != {to_qlib(code) for code in config.data.codes}
        or set(benchmark["instrument"]) != {to_qlib(config.data.benchmark)}
    ):
        raise ValueError("Rolling configuration must match the frozen fixed-price pool")
    sessions = pd.DatetimeIndex(calendar.loc[calendar["is_open"], "date"])
    sessions = sessions[(sessions >= pd.Timestamp(config.data.start)) & (sessions <= pd.Timestamp(config.data.end))]
    if not pd.DatetimeIndex(market["date"]).isin(sessions).all() or market.duplicated(["instrument", "date"]).any():
        raise ValueError("Market rows must be unique observations on the frozen trading sessions")
    return sessions


def _evidence(root, path, frozen, sessions, expected_sha=None):
    root = root.resolve()
    path = root / path
    if not path.is_relative_to(root) or path.resolve(strict=True) != path or not S_ISREG(path.lstat().st_mode):
        raise ValueError("Availability evidence must be a regular file within the data root")
    payload = path.read_bytes()
    digest = sha256_bytes(payload)
    if expected_sha is not None and digest != expected_sha:
        raise ValueError("Availability evidence digest changed")
    binding = frozen.binding()
    certification, rows = parse_evidence(
        json.loads(payload, object_pairs_hook=unique_json_fields),
        data_version=binding["data_version"],
        market_sha256=binding["files"]["market.parquet"],
        instruments=set(frozen.inputs[0]["instrument"]),
        days=set(sessions.strftime("%Y-%m-%d")),
    )
    return {"path": path.relative_to(root).as_posix(), "sha256": digest, "certification": certification}, rows


def _window_plan(raw_folds, sessions, backtest):
    folds = trading_windows(raw_folds, sessions)
    for fold in folds:
        signal = fold["test"]
        entry_start, entry_end = (
            sessions[signal[key] + 1].strftime("%Y-%m-%d") for key in ("start_index", "end_index")
        )
        if not backtest.start <= entry_start <= entry_end <= backtest.end:
            raise ValueError("Test entries must fit within the configured backtest range")
    return folds


def plan_rolling(path: Path, *, store: ResultStore, data_root: Path) -> dict:
    with formula_stage(
        "plan", "invalid_rolling_plan", "Use a fixed-pool plan with two formulas and two ordered folds."
    ):
        raw = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_json_fields)
        if not isinstance(raw, dict) or set(raw) != {"research", "formula_ids", "folds", "report_cutoff", "evidence"}:
            raise ValueError("Unknown or missing plan fields")
        ids = raw["formula_ids"]
        if (
            not isinstance(ids, list)
            or len(ids) != 2
            or any(not isinstance(key, str) or not FORMULA_ID.fullmatch(key) for key in ids)
            or ids[0] == ids[1]
        ):
            raise ValueError("Choose two distinct registered formula versions")
        cutoff = aware(raw["report_cutoff"]).isoformat()
        research = {"evaluation": DIAGNOSTICS.as_dict(), **raw["research"], "runs_dir": str(store.root)}
        config = parse_config(research, path.parent.resolve())
        _validate_price_only(config)
        if config.evaluation != DIAGNOSTICS:
            raise ValueError("Rolling validation fixes the horizon, groups and minimum cross-section")
    versions = [_registered_formula(store, key) for key in sorted(ids)]
    with formula_stage("snapshot", "snapshot_unavailable", "The selected market leaf is missing, invalid or changed."):
        frozen = freeze_market_snapshot(data_root, config.data.root)
        config = replace(config, data=replace(config.data, root=frozen.root))
        sessions = _sessions(frozen, config)
    with formula_stage(
        "plan", "invalid_rolling_plan", "Fold bounds must be ordered trading sessions with equal lengths."
    ):
        folds = _window_plan(raw["folds"], sessions, config.backtest)
    with formula_stage("evidence", "evidence_unavailable", "Availability evidence is missing, invalid or changed."):
        evidence, _ = _evidence(data_root, Path(raw["evidence"]), frozen, sessions)
    with formula_stage("plan", "plan_publication_failed", "Unable to freeze the rolling plan or dependency lock."):
        runtime = source_provenance()
        if runtime["dependency_lock_sha256"] is None:
            raise ValueError("The dependency lock is unavailable")
        research = config.as_dict()
        del research["runs_dir"]
        research["data"]["root"] = frozen.relative_path.as_posix()
        plan = {
            "schema_version": 1,
            "rules": RULES,
            "runtime": runtime,
            "research": research,
            "snapshot": frozen.binding(),
            "evidence": evidence,
            "report_cutoff": cutoff,
            "formulas": {version.id: version.as_dict() for version in versions},
            "folds": [{name: [fold[name]["days"][0], fold[name]["days"][-1]] for name in SEGMENTS} for fold in folds],
            "sessions": sessions.strftime("%Y-%m-%d").tolist(),
            "point_in_time_certified": False,
        }
        identifier = _identity(plan)
        store.publish_experiment_record(identifier, "plan", plan)
    return {"rolling_id": identifier}


def _load_plan(identifier, store, data_root, output):
    with formula_stage("plan", "invalid_rolling_plan", "The frozen rolling plan is missing or invalid."):
        if not ROLLING_ID.fullmatch(identifier):
            raise ValueError("Invalid rolling ID")
        plan = store.read_experiment_record(identifier, "plan")
        if _identity(plan) != identifier or plan["schema_version"] != 1 or plan["rules"] != RULES:
            raise ValueError("Plan recipe or rules do not match the rolling ID")
        config = parse_config({**plan["research"], "runs_dir": str(output)}, data_root.resolve())
    with formula_stage("runtime", "runtime_mismatch", "Use the computation code, lock and Python version of the plan."):
        current = source_provenance()
        if current["dependency_lock_sha256"] is None or current != plan["runtime"]:
            raise ValueError("Runtime provenance changed")
    versions = {key: _registered_formula(store, key) for key in sorted(plan["formulas"])}
    with formula_stage("formula", "formula_version_unavailable", "A frozen formula is invalid or changed."):
        if len(versions) != 2 or any(
            version != FormulaVersion.from_record(plan["formulas"][key]) for key, version in versions.items()
        ):
            raise ValueError("Formula versions changed")
    with formula_stage("snapshot", "snapshot_unavailable", "The frozen market leaf is missing, invalid or changed."):
        frozen = load_frozen_snapshot(data_root, plan["snapshot"])
        if frozen.root != config.data.root or frozen.binding() != plan["snapshot"]:
            raise ValueError("The frozen leaf does not match the plan")
        sessions = _sessions(frozen, config)
        if sessions.strftime("%Y-%m-%d").tolist() != plan["sessions"]:
            raise ValueError("Frozen sessions changed")
        folds = _window_plan(plan["folds"], sessions, config.backtest)
    with formula_stage(
        "evidence", "evidence_unavailable", "The frozen availability evidence is missing, invalid or changed."
    ):
        evidence, rows = _evidence(data_root, plan["evidence"]["path"], frozen, sessions, plan["evidence"]["sha256"])
        if evidence != plan["evidence"]:
            raise ValueError("Evidence binding changed")
    return plan, config, frozen, sessions, folds, versions, rows


def _select(folds, market, sessions, instruments, versions, evidence):
    selection = []
    session_days = sessions.strftime("%Y-%m-%d").tolist()
    for fold in folds:
        # The later part of the snapshot is outside the inputs to validation, even before gating.
        pretest = market.loc[market["date"].lt(pd.Timestamp(fold["test"]["days"][0]))]
        labels = labels_for_pool(pretest, sessions, instruments)
        scores = {
            key: causal_scores(pretest, sessions, instruments, fold["validation"]["days"][-1], version)
            for key, version in versions.items()
        }
        phases = {}
        for name, next_name in zip(SEGMENTS[:-1], SEGMENTS[1:], strict=True):
            next_start = fold[next_name]["start_index"]
            previous = session_days[next_start - 1]
            cutoff = aware(f"{previous}T18:00:00+08:00")
            rows = with_scores(
                gated_labels(
                    labels,
                    fold[name]["days"],
                    evidence,
                    cutoff,
                    next_start=session_days[next_start],
                    previous_day=previous,
                ),
                scores,
            )
            phases[name] = {"cutoff": cutoff.isoformat(), "summary": row_summary(rows), "rows": rows}
        validation = phases["validation"]
        choice = choose(validation["rows"], tuple(versions))
        retained = [row for row in validation["rows"] if row["exclusion"] is None]
        validation["digest"] = canonical_json_sha256({"retained": retained, "choice": choice})
        selection.append({**phases, "choice": choice})
    return {"folds": selection}


def _test_fold(identifier, index, plan, fold, winner, config, frozen, sessions, instruments, version, evidence, store):
    test = fold["test"]
    market = frozen.inputs[0]
    first, last = (sessions[test[key] + 1].strftime("%Y-%m-%d") for key in ("start_index", "end_index"))
    configured = replace(
        config,
        backtest=replace(config.backtest, start=first, end=last),
        factors=formula_config(version),
        neutralization=PRICE_ONLY,
    )
    result = run_configured(configured, result_store=store, frozen_snapshot=frozen)
    labels = labels_for_pool(market, sessions, instruments)
    scores = causal_scores(market, sessions, instruments, test["days"][-1], version)
    rows = with_scores(gated_labels(labels, test["days"], evidence, aware(plan["report_cutoff"])), {winner: scores})
    report = {"rows": rows, "summary": row_summary(rows), "rank_ic": diagnostics(rows, winner)}
    evaluation_id = "rolling-eval-" + canonical_json_sha256({"rolling_id": identifier, "fold": index})
    evaluation: dict[str, Any] = {
        "metadata": {
            "id": evaluation_id,
            "name": "本地滚动测试诊断",
            "is_synthetic": plan["evidence"]["certification"] == "synthetic",
            "point_in_time_certified": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "formula_id": winner,
            "factor_run_id": result["factor_run_id"],
            "rolling_id": identifier,
            "fold": index,
            "report_cutoff": plan["report_cutoff"],
            "evidence": plan["evidence"],
        },
        **report,
    }
    if store.exists("evaluations", evaluation_id):
        existing = store.get("evaluations", evaluation_id)
        evaluation["metadata"]["created_at"] = existing["metadata"]["created_at"]
    store.publish("evaluations", evaluation_id, evaluation)
    backtest = store.get("backtests", result["run_id"])
    return {
        "winner": winner,
        "factor_run_id": result["factor_run_id"],
        "backtest_id": result["run_id"],
        "evaluation_id": evaluation_id,
        "evaluation_sha256": canonical_json_sha256(report),
        "scores_sha256": canonical_json_sha256(store.get("factors", result["factor_run_id"])["history"]),
        "backtest_sha256": canonical_json_sha256({key: backtest[key] for key in ("trades", "positions", "equity")}),
        "metrics": result["metrics"],
    }


def _check_result(document, identifier, selection_sha):
    if (
        document["metadata"]["id"] != identifier
        or document["selection_sha256"] != selection_sha
        or document["result_sha256"] != canonical_json_sha256(document["folds"])
    ):
        raise ValueError("Rolling result and selection do not agree")


def run_rolling(identifier: str, *, store: ResultStore, data_root: Path, output: Path | None = None) -> dict:
    """Run a sealed plan, or replay a completed plan and its original selections."""
    target = ResultStore(output) if output is not None else store
    plan, config, frozen, sessions, folds, versions, evidence = _load_plan(identifier, store, data_root, target.root)
    instruments = set(frozen.inputs[0]["instrument"])
    expected = original_selection = None
    if output is not None:
        with formula_stage("replay", "invalid_rolling_result", "The original rolling result or selection is invalid."):
            expected = store.get("experiments", identifier)
            original_selection = store.read_experiment_record(identifier, "selection")
            _check_result(expected, identifier, canonical_json_sha256(original_selection))
    with formula_stage("selection", "selection_failed", "Unable to reproduce the mature validation selection."):
        selected = _select(folds, frozen.inputs[0], sessions, instruments, versions, evidence)
        if original_selection is not None and original_selection != selected:
            raise ValueError("Replay selection differs from the original")
        selection_sha = canonical_json_sha256(selected)
    with formula_stage("output", "output_unavailable", "Replay requires a new output directory."):
        if output is not None:
            target.root.mkdir(parents=True, exist_ok=False)
        locks = target.root / ".locks"
        locks.mkdir(parents=True, exist_ok=True)
    with formula_stage("test", "rolling_test_failed", "Unable to test the sealed winners or reproduce their results."):
        with FileLock(locks / f"{identifier}.lock", timeout=0):
            target.publish_experiment_record(identifier, "plan", plan)
            target.publish_experiment_record(identifier, "selection", selected)
            if target.exists("experiments", identifier):
                existing = target.get("experiments", identifier)
                _check_result(existing, identifier, selection_sha)
                return existing
            results = []
            for index, (fold, selection) in enumerate(zip(folds, selected["folds"], strict=True), 1):
                winner = selection["choice"]["winner"]
                results.append(
                    _test_fold(
                        identifier,
                        index,
                        plan,
                        fold,
                        winner,
                        config,
                        frozen,
                        sessions,
                        instruments,
                        versions[winner],
                        evidence,
                        target,
                    )
                    if winner
                    else {"winner": None, "reason": selection["choice"]["reason"]}
                )
            result_sha = canonical_json_sha256(results)
            if expected is not None and (expected["folds"] != results or expected["result_sha256"] != result_sha):
                raise ValueError("Replayed winner artifacts differ from the original")
            document = {
                "metadata": {
                    "id": identifier,
                    "name": "本地滚动验证",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "is_synthetic": plan["evidence"]["certification"] == "synthetic",
                    "point_in_time_certified": False,
                    "data_version": plan["snapshot"]["data_version"],
                    "status": "completed",
                },
                "selection_sha256": selection_sha,
                "result_sha256": result_sha,
                "folds": results,
            }
            target.publish("experiments", identifier, document)
            return document
