"""Local commands to register, run and replay a restricted formula."""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from finance_quant.application.manual_formula import (
    EVALUATION_ID,
    FORMULA_ID,
    ManualFormulaError,
    formula_stage,
    register_formula,
    replay_formula,
    run_formula,
)
from finance_quant.configuration import load_config
from finance_quant.storage import ResultStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, required=True, help="Local formula and research artifact directory")
    commands = parser.add_subparsers(dest="operation", required=True)
    register = commands.add_parser("register", help="Publish an immutable formula JSON document")
    register.add_argument("formula", type=Path)
    run = commands.add_parser("run", help="Evaluate a formula and backtest it on a managed fixed pool")
    run.add_argument("--data-root", type=Path, required=True)
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--formula-id", required=True)
    replay = commands.add_parser("replay", help="Recompute an evaluation and backtest into a fresh directory")
    replay.add_argument("--data-root", type=Path, required=True)
    replay.add_argument("--evaluation-id", required=True)
    replay.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    store = ResultStore(args.runs_dir)
    try:
        with redirect_stdout(sys.stderr):
            if args.operation == "register":
                result = register_formula(args.formula, store)
            elif args.operation == "run":
                with formula_stage(
                    "configuration", "invalid_configuration", "Unable to load the research configuration."
                ):
                    config = replace(load_config(args.config), runs_dir=store.root)
                result = run_formula(config, args.formula_id, data_root=args.data_root)
            else:
                result = replay_formula(args.evaluation_id, store=store, data_root=args.data_root, output=args.output)
    except ManualFormulaError as error:
        record = {
            "status": "failed",
            "operation": args.operation,
            "stage": error.stage,
            "code": error.code,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        for field, pattern in (("formula_id", FORMULA_ID), ("evaluation_id", EVALUATION_ID)):
            if pattern.fullmatch(value := getattr(args, field, "")):
                record[field] = value
        receipt_id = f"formula-failure-{uuid4().hex}"
        try:
            store.publish("formula_failures", receipt_id, record)
        except (OSError, ValueError):
            receipt_id = "failure receipt unavailable"
        print(f"{error.code} ({receipt_id}): {error}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
