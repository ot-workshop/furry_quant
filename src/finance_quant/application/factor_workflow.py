"""Versioned factor artifacts shared by execution and research diagnostics."""

from __future__ import annotations

import hashlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

from finance_quant.data.market import load_market
from finance_quant.research.market_factors import market_factors
from finance_quant.research.registry import FactorConfig
from finance_quant.storage import ResultStore


def source_provenance(files: list[Path]) -> dict:
    lock_path = Path(__file__).parents[3] / "uv.lock"
    return {"code_sha256": hashlib.sha256(b"".join(path.read_bytes() for path in files)).hexdigest(),
            "dependency_lock_sha256": hashlib.sha256(lock_path.read_bytes()).hexdigest() if lock_path.exists() else None,
            "python_version": platform.python_version()}


def fingerprint(provenance: dict) -> str:
    return hashlib.sha256(json.dumps(provenance, sort_keys=True, allow_nan=False).encode()).hexdigest()[:12]


def load_configured_snapshot(config):
    snapshot = load_market(config.data.root)
    manifest = snapshot[3]
    if (set(manifest["codes"]) != set(config.data.codes) or manifest["start"] != config.data.start
        or manifest["end"] != config.data.end or manifest.get("benchmark", "000300.SH") != config.data.benchmark):
        raise ValueError("Published snapshot differs from configured data range, stock pool or benchmark; run download with this config first")
    return snapshot


def publish_factor_run(market, manifest: dict, runs_dir: Path, config: FactorConfig | None = None) -> dict:
    config = config or FactorConfig()
    base = Path(__file__).parents[1]
    provenance = {**source_provenance([Path(__file__), base / "research/market_factors.py", base / "research/registry.py"]),
                  "data_version": manifest["id"], "factors": config.as_list()}
    run_id = "real-factors-" + fingerprint(provenance)
    store = ResultStore(runs_dir)
    locks = runs_dir / ".locks"
    locks.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(locks / (run_id + ".lock"), timeout=0):
            if store.path("factors", run_id).exists():
                existing = store.get("factors", run_id)
                if existing["metadata"]["provenance"] != provenance:
                    raise ValueError("Factor artifact provenance conflict")
                return existing
            result = market_factors(market, manifest["id"], config)
            result["metadata"].update(id=run_id, provenance=provenance, created_at=datetime.now(timezone.utc).isoformat())
            store.publish("factors", run_id, result)
            return result
    except Timeout:
        raise ValueError("This factor configuration is already running") from None
