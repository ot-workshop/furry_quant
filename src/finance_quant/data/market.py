"""Immutable research snapshots and cached reference queries; no credentials in artifacts."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from finance_quant.checksums import file_sha256, sha256_text
from finance_quant.data.providers import (
    TushareProvider,
)
from finance_quant.data.quality import adjustment_discontinuities, published_adjustment_discontinuities
from finance_quant.data.reconciliation import validate_published_sources
from finance_quant.data.shared_market import validate_snapshot


class SnapshotFetcher:
    def __init__(self, provider: TushareProvider, root: Path):
        self.provider, self.root = provider, root
        settings = getattr(provider, "settings", None)
        origin = (
            settings.http_url
            if settings is not None
            else f"offline:{type(provider).__module__}.{type(provider).__qualname__}"
        )
        self.provider_id = sha256_text(origin)
        self.root.mkdir(parents=True, exist_ok=True)
        self.requests = []

    def get(self, api: str, **params) -> pd.DataFrame:
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", api) is None:
            raise ValueError("Invalid dataset API identifier")
        if any(key.lower() in {"token", "password", "secret", "authorization"} for key in params):
            raise ValueError("Credentials must not appear in dataset parameters")
        specification = {"api": api, "params": params, "provider_id": self.provider_id}
        key = sha256_text(json.dumps(specification, sort_keys=True))[:16]
        path, metadata_path = self.root / f"{api}-{key}.parquet", self.root / f"{api}-{key}.json"
        if path.exists() and metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("sha256") != file_sha256(path):
                raise ValueError("Cached source checksum does not match; use a new batch directory")
            frame = pd.read_parquet(path)
        else:
            frame = self.provider.query(api, **params)
            temporary = path.with_suffix(".tmp")
            frame.to_parquet(temporary, index=False)
            temporary.replace(path)
            metadata = {
                **specification,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "rows": len(frame),
                "sha256": file_sha256(path),
            }
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        self.requests.append(metadata)
        print(f"{api}: {len(frame)} rows", flush=True)
        return frame


def load_market(root: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if "snapshot_path" in manifest and root.name != Path(manifest["snapshot_path"]).name:
        snapshot = (root / manifest["snapshot_path"]).resolve()
        if not snapshot.is_relative_to(root.resolve()):
            raise ValueError("Snapshot path escapes batch directory")
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        root = snapshot
    if manifest.get("schema_version") not in {"market-v1", "market-v2", "market-v3", "market-v4-shared"}:
        raise ValueError("Unsupported market snapshot schema")
    if set(manifest["files"]) != {"market.parquet", "benchmark.parquet", "calendar.parquet"}:
        raise ValueError("Incomplete market snapshot file manifest")
    for name, digest in manifest["files"].items():
        if name not in {"market.parquet", "benchmark.parquet", "calendar.parquet"}:
            raise ValueError("Unexpected snapshot file")
        if file_sha256(root / name) != digest:
            raise ValueError("Market snapshot checksum mismatch")
    if (root / "rejected.json").exists():
        raise ValueError("This snapshot was quarantined after a failed quality audit")
    manifest["snapshot_dir"] = str(root.resolve())
    market = pd.read_parquet(root / "market.parquet")
    if market.get("is_suspended", pd.Series(False, index=market.index)).any() and manifest.get(
        "schema_version"
    ) not in {"market-v3", "market-v4-shared"}:
        raise ValueError("Suspended valuation rows require an independently validated joint snapshot")
    if manifest.get("schema_version") == "market-v3":
        validate_published_sources(market, manifest)
    if manifest.get("schema_version") == "market-v4-shared":
        validate_snapshot(market, manifest)
    anomalies = (
        published_adjustment_discontinuities(market)
        if manifest.get("schema_version") == "market-v4-shared"
        else adjustment_discontinuities(market)
    )
    if anomalies:
        raise ValueError(
            "Market adjustment continuity failed; verify supplier data or corporate actions before backtesting"
        )
    return (market, pd.read_parquet(root / "benchmark.parquet"), pd.read_parquet(root / "calendar.parquet"), manifest)
