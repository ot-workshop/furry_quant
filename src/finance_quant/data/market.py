"""Immutable research snapshots and cached reference queries; no credentials in artifacts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from stat import S_ISREG

import pandas as pd

from finance_quant.checksums import file_sha256, sha256_bytes, sha256_text
from finance_quant.data.providers import (
    TushareProvider,
)
from finance_quant.data.quality import adjustment_discontinuities, published_adjustment_discontinuities
from finance_quant.data.reconciliation import validate_published_sources
from finance_quant.data.shared_market import validate_snapshot

SCORE_SNAPSHOT_PREFIX = "forward-score-"


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


def write_market_snapshot_files(
    root: Path, market: pd.DataFrame, benchmark: pd.DataFrame, calendar: pd.DataFrame
) -> dict:
    """Write the three managed payloads and return their physical digests before publishing a manifest."""
    files = {}
    for name, frame in (("market", market), ("benchmark", benchmark), ("calendar", calendar)):
        path = root / f"{name}.parquet"
        temporary = path.with_suffix(".pending.parquet")
        frame.to_parquet(temporary, index=False)
        temporary.replace(path)
        files[path.name] = file_sha256(path)
    return files


def load_market(root: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if "snapshot_path" in manifest and root.name != Path(manifest["snapshot_path"]).name:
        snapshot = (root / manifest["snapshot_path"]).resolve()
        if not snapshot.is_relative_to(root.resolve()):
            raise ValueError("Snapshot path escapes batch directory")
        manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        root = snapshot
    return _load_leaf(root, manifest)


@dataclass(frozen=True)
class FrozenMarketSnapshot:
    root: Path
    manifest_sha256: str
    inputs: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]


def load_frozen_snapshot(data_root: Path, execution: dict) -> FrozenMarketSnapshot:
    """Verify and load exactly the queued snapshot leaf, never a latest pointer."""
    raw = execution.get("snapshot_dir")
    if not isinstance(raw, str) or not raw:
        raise ValueError("提交时未冻结数据快照")
    root = data_root.resolve()
    # Accept absolute paths from earlier pending payloads, but require the same topology.
    directory = root / raw
    if not directory.is_relative_to(root):
        raise ValueError("冻结的数据快照路径不在受控目录内")
    parts = directory.relative_to(root).parts
    managed = (len(parts) == 3 and parts[:2] == ("research", "subsets")) or (
        len(parts) == 5 and parts[:2] == ("research", "downloads") and parts[3] == "snapshots"
    )
    if not managed or any(part.startswith(".") for part in parts):
        raise ValueError("冻结的数据快照路径不在受控目录内")
    try:
        resolved = directory.resolve(strict=True)
    except FileNotFoundError:
        raise ValueError("提交时的数据快照已不可用，请重新提交") from None
    if resolved != directory:
        raise ValueError("冻结的数据快照路径不在受控目录内")

    def regular_file(name):
        path = directory / name
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            raise ValueError("提交时的数据快照文件已不可用，请重新提交") from None
        if not S_ISREG(mode) or path.resolve(strict=True) != path:
            raise ValueError("冻结的数据快照文件路径不合法")
        return path

    try:
        (directory / "rejected.json").lstat()
    except FileNotFoundError:
        pass
    else:
        raise ValueError("提交时的数据快照已被隔离，请重新提交")
    with regular_file("manifest.json").open("rb") as stream:
        payload = stream.read(1024 * 1024 + 1)
    digest = sha256_bytes(payload)
    if len(payload) > 1024 * 1024 or digest != execution.get("manifest_sha256"):
        raise ValueError("排队期间股票池历史状态发生变化，请重新提交")
    manifest = json.loads(payload)
    if (
        not isinstance(manifest, dict)
        or not isinstance(manifest.get("id"), str)
        or manifest["id"] != execution.get("data_version")
        or not isinstance(manifest.get("files"), dict)
        or manifest["files"] != execution.get("files")
    ):
        raise ValueError("排队期间数据版本发生变化，请重新提交")
    if "snapshot_path" in manifest and manifest["snapshot_path"] != f"snapshots/{directory.name}":
        raise ValueError("冻结的数据快照清单指向其他路径")
    # Check physical paths before the normal leaf loader hashes and decodes the three files.
    for name in ("market.parquet", "benchmark.parquet", "calendar.parquet"):
        regular_file(name)
    return FrozenMarketSnapshot(directory, digest, _load_leaf(directory, manifest))


def _load_leaf(root, manifest):
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
