"""Health, data inventory and research overview routes."""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from finance_quant.application import market_download
from finance_quant.application.research_requests import snapshot_registry
from finance_quant.data.historical_warehouse import warehouse_option
from finance_quant.data.market_universe import market_option, market_roots

SYNTHETIC_NOTICE = "合成样本，仅验证系统流程，不代表真实股票表现"
_SNAPSHOT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
_FIXED_SCHEMAS = {"market-v1", "market-v2", "market-v3", "market-v4-shared"}
_FIXED_FILES = {"market.parquet", "benchmark.parquet", "calendar.parquet"}
_WAREHOUSE_SCHEMAS = {
    "historical": "historical-index-warehouse-v1",
    "market": "historical-market-warehouse-v1",
}
_WAREHOUSE_VIEWS = {"historical": "index-view", "market": "market-view"}
_SUMMARY_CACHE_TTL_SECONDS = 30.0


def research_snapshots(data_root) -> list[dict]:
    """Selectable research datasets with their coverage evidence, newest first."""

    items = []
    for identifier, (_, _, _, manifest) in snapshot_registry(data_root).items():
        coverage = manifest.get("coverage", {})
        items.append(
            {
                "id": identifier,
                "source": manifest.get("source"),
                "schema_version": manifest.get("schema_version"),
                "start": manifest["start"],
                "end": manifest["end"],
                "codes": manifest["codes"],
                "benchmark": manifest.get("benchmark"),
                "trading_days": manifest.get("trading_days"),
                "rows": manifest.get("rows"),
                "is_subset": "projection" in manifest,
                "parent_id": manifest.get("projection", {}).get("parent_data_version"),
                "shared_version": manifest.get("shared_version"),
                "validation_counts": manifest.get("validation_counts"),
                "coverage": {
                    key: coverage.get(key)
                    for key in ("status", "expected_sessions", "stock_count", "stocks", "benchmark", "note")
                },
                "cross_validation": manifest.get("cross_validation"),
                "notes": manifest.get("notes", []),
            }
        )
    items.sort(key=lambda row: (row["is_subset"], row["end"]), reverse=False)
    return items


def historical_indices(data_root) -> list[dict]:
    """Published CSI 300 dynamic-universe versions, newest first, without the per-day calendar."""

    items = []
    for root in market_download.warehouse_roots(data_root):
        option = warehouse_option(root)
        if option:
            dates = option.pop("trading_dates")
            items.append({**option, "trading_days": len(dates)})
    return items


def _confined_entry(
    path: Path, root: Path, *, entry: os.DirEntry[str] | None = None
) -> tuple[Path, os.stat_result] | None:
    """Inspect a child of an already confined directory; never follow an unchecked link."""

    try:
        info = entry.stat(follow_symlinks=False) if entry is not None else path.lstat()
        # Windows junctions are reparse points, even when is_symlink() is false.
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            try:
                path = path.resolve(strict=True)
            except RuntimeError as exc:  # Symlink loops on Python 3.11/3.12
                raise OSError(errno.ELOOP, "Loop resolving inventory path", path) from exc
            if not path.is_relative_to(root):
                return None
            info = path.stat()
        return path, info
    except FileNotFoundError:
        return None


def _confined_dir(
    path: Path, root: Path, *, entry: os.DirEntry[str] | None = None
) -> tuple[Path, os.stat_result] | None:
    found = _confined_entry(path, root, entry=entry)
    return found if found is not None and stat.S_ISDIR(found[1].st_mode) else None


def _directories(parent: Path, root: Path) -> Iterator[Path]:
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                found = _confined_dir(Path(entry.path), root, entry=entry)
                if found is not None:
                    yield found[0]
    except FileNotFoundError:
        # A concurrently removed directory is absent, not a storage outage.
        return


def _regular_file(parent: Path, name: str, root: Path) -> bool:
    found = _confined_entry(parent / name, root)
    return found is not None and stat.S_ISREG(found[1].st_mode)


def _load_manifest_object(parent: Path, root: Path) -> dict | None:
    """Raise on missing or I/O failure; only wrong type or malformed content counts as invalid."""

    found = _confined_entry(parent / "manifest.json", root)
    if found is None:
        raise FileNotFoundError(parent / "manifest.json")
    path, info = found
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _fixed_snapshot_id(path: Path, root: Path, manifest: dict) -> str | None:
    identifier = manifest.get("id")
    if not isinstance(identifier, str) or not _SNAPSHOT_ID_RE.fullmatch(identifier):
        return None
    schema_version = manifest.get("schema_version")
    if not isinstance(schema_version, str) or schema_version not in _FIXED_SCHEMAS:
        return None
    codes = manifest.get("codes")
    if not isinstance(codes, list) or not codes:
        return None
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != _FIXED_FILES:
        return None
    # Presence only; full checksums remain the responsibility of the existing data-load paths.
    for name, expected in files.items():
        if not isinstance(expected, str) or not expected or not _regular_file(path, name, root):
            return None
    return identifier


def _valid_warehouse_header(path: Path, root: Path, manifest: dict, expected_schema: str) -> bool:
    if manifest.get("schema_version") != expected_schema:
        return False
    assets = manifest.get("assets")
    if not isinstance(assets, dict) or not assets:
        return False
    ref_files = manifest.get("reference_files")
    calendar_sha = ref_files.get("calendar.parquet") if isinstance(ref_files, dict) else None
    if not isinstance(calendar_sha, str) or not calendar_sha:
        return False
    reference = _confined_dir(path / "reference", root)
    return reference is not None and _regular_file(reference[0], "calendar.parquet", root)


@dataclass(frozen=True)
class _InventoryContainers:
    snapshots: tuple[Path, ...]
    versions: Path | None
    signature: tuple[tuple[Path, int], ...]


def _inventory_containers(root: Path) -> _InventoryContainers:
    """Discover container paths once and reuse them for both cache validation and cold counts."""

    stamps: list[tuple[Path, int]] = []
    snapshots: list[Path] = []
    research = _confined_dir(root / "research", root)
    downloads = _confined_dir(research[0] / "downloads", root) if research is not None else None
    if downloads is not None:
        stamps.append((downloads[0], downloads[1].st_mtime_ns))
        for source in _directories(downloads[0], root):
            info = _confined_dir(source / "snapshots", root)
            if info is not None:
                snapshots.append(info[0])
                stamps.append((info[0], info[1].st_mtime_ns))

    shared = _confined_dir(root / "shared-market", root)
    versions = _confined_dir(shared[0] / "versions", root) if shared is not None else None
    if versions is not None:
        stamps.append((versions[0], versions[1].st_mtime_ns))
    return _InventoryContainers(tuple(snapshots), versions[0] if versions else None, tuple(sorted(stamps)))


def _dataset_candidates(root: Path, containers: _InventoryContainers) -> Iterator[tuple[str, Path]]:
    for snapshots in containers.snapshots:
        for path in _directories(snapshots, root):
            yield "fixed", path
    if containers.versions is not None:
        for version in _directories(containers.versions, root):
            if not _regular_file(version, "version.json", root):
                continue  # The publisher writes this marker last.
            for kind, name in _WAREHOUSE_VIEWS.items():
                view = _confined_dir(version / name, root)
                if view is not None:
                    yield kind, view[0]


def _dataset_counts(root: Path, containers: _InventoryContainers) -> dict[str, int]:
    """One confined traversal for all three dataset kinds, without opening payload files."""

    fixed_ids: set[str] = set()
    warehouses: dict[str, set[Path]] = {kind: set() for kind in _WAREHOUSE_VIEWS}
    seen: set[tuple[str, Path]] = set()
    invalid = 0
    for kind, path in _dataset_candidates(root, containers):
        if (kind, path) in seen:
            continue
        seen.add((kind, path))
        try:
            manifest = _load_manifest_object(path, root)
        except FileNotFoundError:
            continue  # Incomplete or concurrently removed publication, not corrupt content.
        if manifest is None:
            invalid += 1
        elif kind == "fixed":
            if "projection" in manifest:
                continue
            try:
                # A rejection marker need not be opened or followed to exclude the snapshot.
                (path / "rejected.json").lstat()
            except FileNotFoundError:
                identifier = _fixed_snapshot_id(path, root, manifest)
                if identifier is not None:
                    fixed_ids.add(identifier)
                    continue
            invalid += 1
        elif _valid_warehouse_header(path, root, manifest, _WAREHOUSE_SCHEMAS[kind]):
            warehouses[kind].add(path)
        else:
            invalid += 1
    return {
        "fixed": len(fixed_ids),
        "historical": len(warehouses["historical"]),
        "market": len(warehouses["market"]),
        "invalid": invalid,
    }


def system_router(
    store,
    data_root,
    *,
    database_probe=None,
    market_status=None,
    market_summary=None,
    summary_cache_ttl: float = _SUMMARY_CACHE_TTL_SECONDS,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    resolved_root = Path(data_root).resolve()
    cache_lock = threading.Lock()
    fetch_lock = threading.Lock()
    cache_expires: float = 0.0
    cache_value: tuple[dict | None, str] | None = None
    db_epoch: int = 0

    counts_cache_lock = threading.Lock()
    counts_fetch_lock = threading.Lock()
    counts_cache_expires: float = 0.0
    counts_cache_signature: tuple[tuple[Path, int], ...] | None = None
    counts_cache_value: tuple[dict[str, int], str] | None = None
    counts_epoch: int = 0

    def invalidate_summary_cache() -> None:
        nonlocal cache_expires, cache_value, db_epoch
        nonlocal counts_cache_expires, counts_cache_signature, counts_cache_value, counts_epoch
        with cache_lock:
            db_epoch += 1
            cache_expires = 0.0
            cache_value = None
        with counts_cache_lock:
            counts_epoch += 1
            counts_cache_expires = 0.0
            counts_cache_signature = None
            counts_cache_value = None

    setattr(router, "invalidate_summary_cache", invalidate_summary_cache)

    @router.get("/health")
    def health():
        database = {}
        if database_probe is not None:
            try:
                database = database_probe()
            except Exception:
                raise HTTPException(503, "数据库连接或版本检查未通过") from None
        return {"status": "ok", "read_only": False, "task_execution": "postgres_worker", **database}

    def read_database_summary() -> tuple[dict | None, str]:
        nonlocal cache_expires, cache_value
        loader = market_summary
        if loader is None:
            return None, datetime.now(UTC).isoformat()
        now = time.monotonic()
        if summary_cache_ttl > 0:
            with cache_lock:
                if now < cache_expires and cache_value is not None:
                    return cache_value
        with fetch_lock:
            now = time.monotonic()
            with cache_lock:
                if summary_cache_ttl > 0 and now < cache_expires and cache_value is not None:
                    return cache_value
                start_epoch = db_epoch
            raw = loader()
            summary = {
                "securities": int(raw["securities"]),
                "coverage_start": raw.get("coverage_start"),
                "coverage_end": raw.get("coverage_end"),
            }
            sampled_at = datetime.now(UTC).isoformat()
            with cache_lock:
                if summary_cache_ttl > 0 and db_epoch == start_epoch:
                    cache_value = (summary, sampled_at)
                    cache_expires = time.monotonic() + summary_cache_ttl
            return summary, sampled_at

    def read_dataset_counts() -> tuple[dict[str, int], str]:
        nonlocal counts_cache_expires, counts_cache_signature, counts_cache_value
        with counts_fetch_lock:
            with counts_cache_lock:
                start_epoch = counts_epoch
            containers = _inventory_containers(resolved_root)
            sig = containers.signature
            now = time.monotonic()
            with counts_cache_lock:
                if (
                    summary_cache_ttl > 0
                    and now < counts_cache_expires
                    and counts_cache_value is not None
                    and counts_cache_signature == sig
                ):
                    return counts_cache_value
            counts = _dataset_counts(resolved_root, containers)
            counts_sampled_at = datetime.now(UTC).isoformat()
            with counts_cache_lock:
                if summary_cache_ttl > 0 and counts_epoch == start_epoch:
                    counts_cache_value = (counts, counts_sampled_at)
                    counts_cache_signature = sig
                    counts_cache_expires = time.monotonic() + summary_cache_ttl
            return counts, counts_sampled_at

    @router.get("/dashboard/summary")
    def dashboard_summary():
        try:
            database, sampled_at = read_database_summary()
            counts, counts_sampled_at = read_dataset_counts()
            return {
                "database": database,
                "counts": counts,
                "as_of": sampled_at,
                "counts_as_of": counts_sampled_at,
                "counts_basis": "publisher_manifests",
                "cache_ttl_seconds": summary_cache_ttl,
            }
        except (SQLAlchemyError, OSError, ValueError):
            raise HTTPException(503, "工作台摘要暂时无法读取，请检查数据库连接与存储目录") from None

    @router.get("/data-status")
    def data_status():
        try:
            return {
                "snapshots": research_snapshots(data_root),
                "historical_indices": historical_indices(data_root),
                "market_universes": [
                    {
                        **{key: value for key, value in option.items() if key != "trading_dates"},
                        "trading_days": len(option["trading_dates"]),
                    }
                    for root in market_roots(data_root)
                    if (option := market_option(root))
                ],
                "database": market_status() if market_status is not None else None,
            }
        except (SQLAlchemyError, OSError, ValueError):
            raise HTTPException(503, "数据状态暂时无法读取，请检查数据库连接与存储目录") from None

    @router.get("/overview")
    def overview():
        factors, unavailable = store.list_metadata("factors")
        backtests, bad_backtests = store.list_metadata("backtests")
        _, bad_evaluations = store.list_metadata("evaluations")
        unavailable += bad_backtests + bad_evaluations
        instruments = {row["instrument"] for result in factors for row in result["metadata"]["universe_members"]}
        modes = {bool(result["metadata"]["is_synthetic"]) for result in factors}
        modes.update(bool(result["summary"]["is_synthetic"]) for result in backtests)
        if len(modes) > 1:
            mode = "mixed"
        elif modes == {True}:
            mode = "synthetic"
        elif modes == {False}:
            mode = "real"
        else:
            mode = "empty"
        notices = {
            "synthetic": SYNTHETIC_NOTICE,
            "real": "真实行情按批次保存；请查看所选批次的数据来源、校验等级与模拟假设",
            "mixed": "真实行情与合成样本分别保存；请以所选评分批次和回测的数据标识为准",
            "empty": "尚无研究结果",
        }
        notice = notices[mode]
        if unavailable:
            notice += f"；有{len(unavailable)}条结果暂不可读，已隔离显示，其他结果仍可查看"
        return {
            "mode": mode,
            "notice": notice,
            "factor_runs": len(factors),
            "backtests": len(backtests),
            "instruments": len(instruments),
            **({"unavailable_results": unavailable} if unavailable else {}),
        }

    return router
