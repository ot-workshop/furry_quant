"""Health, data inventory and research overview routes."""

from __future__ import annotations

import json
import os
import re
import threading
import time
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


def _load_manifest_object(path: Path) -> dict | None:
    """Parse a JSON file and require a JSON object (reject arrays/scalars/corrupt text)."""

    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _valid_fixed_snapshot_fields(
    manifest: dict, present_files: set[str] | None = None, path: Path | None = None
) -> str | None:
    """Validate publisher fixed-snapshot header fields without opening parquet payloads."""

    if "projection" in manifest:
        return None
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
    for name, expected in files.items():
        if not isinstance(expected, str) or not expected:
            return None
        if present_files is not None:
            if name not in present_files:
                return None
        elif path is None or not (path / name).is_file():
            return None
    return identifier


def _valid_fixed_snapshot(path: Path, root: Path, manifest: dict) -> str | None:
    """Verify fixed snapshot publisher manifest without opening or hashing payload files.

    Reuses producer-recorded metadata (`schema_version`, `files` digest map,
    non-empty `codes`, absence of `rejected.json`) plus file presence; full payload
    checksum verification remains in `/api/v1/data-status` and data-load paths.
    """

    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or (resolved / "rejected.json").exists():
            return None
        return _valid_fixed_snapshot_fields(manifest, path=resolved)
    except OSError:
        return None


def _valid_warehouse_header(wh_root: Path, expected_schema: str) -> bool:
    """Verify warehouse publisher manifest readiness without opening or hashing payload files."""

    manifest = _load_manifest_object(wh_root / "manifest.json")
    if manifest is None:
        return False
    schema_version = manifest.get("schema_version")
    if not isinstance(schema_version, str) or schema_version != expected_schema:
        return False
    assets = manifest.get("assets")
    if not isinstance(assets, dict) or not assets:
        return False
    ref_files = manifest.get("reference_files")
    calendar_sha = ref_files.get("calendar.parquet") if isinstance(ref_files, dict) else None
    calendar_path = wh_root / "reference/calendar.parquet"
    return isinstance(calendar_sha, str) and bool(calendar_sha) and calendar_path.is_file()


def _inventory_container_signature(root: Path) -> tuple[tuple[str, int], ...]:
    """O(container dirs) mtime signature so new/removed dataset folders invalidate cached counts."""

    stamps: list[tuple[str, int]] = []
    for rel in ("research/downloads", "shared-market/versions"):
        base = root / rel
        try:
            stamps.append((rel, base.stat().st_mtime_ns))
        except OSError:
            stamps.append((rel, -1))
            continue
        if rel == "research/downloads":
            try:
                with os.scandir(base) as sources:
                    for src in sorted(sources, key=lambda entry: entry.name):
                        if not src.is_dir(follow_symlinks=True):
                            continue
                        snaps = Path(src.path) / "snapshots"
                        try:
                            stamps.append((f"{src.name}/snapshots", snaps.stat().st_mtime_ns))
                        except OSError:
                            pass
            except OSError:
                pass
    return tuple(stamps)


def _scan_fixed_snapshots(root: Path) -> tuple[set[str], int]:
    """Scan fixed-pool snapshot headers via os.scandir without opening payload files."""

    fixed_ids: set[str] = set()
    invalid = 0
    downloads_dir = root / "research" / "downloads"
    if not downloads_dir.is_dir():
        return fixed_ids, invalid
    try:
        with os.scandir(downloads_dir) as sources:
            source_entries = sorted(sources, key=lambda entry: entry.name)
    except OSError:
        return fixed_ids, invalid
    for src in source_entries:
        if not src.is_dir(follow_symlinks=True):
            continue
        snaps_dir = Path(src.path) / "snapshots"
        if not snaps_dir.is_dir():
            continue
        try:
            with os.scandir(snaps_dir) as entries:
                snap_entries = sorted(entries, key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in snap_entries:
            if not entry.is_dir(follow_symlinks=True):
                continue
            snap_path = Path(entry.path)
            if entry.is_symlink() or src.is_symlink():
                try:
                    if not snap_path.resolve().is_relative_to(root):
                        continue
                except OSError:
                    continue
            try:
                with os.scandir(snap_path) as children:
                    child_files = {child.name for child in children if child.is_file(follow_symlinks=True)}
            except OSError:
                continue
            if "manifest.json" not in child_files:
                continue
            manifest = _load_manifest_object(snap_path / "manifest.json")
            if manifest is None:
                invalid += 1
                continue
            if "projection" in manifest:
                continue
            if "rejected.json" in child_files:
                invalid += 1
                continue
            identifier = _valid_fixed_snapshot_fields(manifest, present_files=child_files)
            if identifier is not None:
                fixed_ids.add(identifier)
            else:
                invalid += 1
    return fixed_ids, invalid


def _dataset_counts(data_root: Path) -> dict[str, int]:
    """Count published datasets from validated publisher manifests only (not revalidated payloads)."""

    root = Path(data_root).resolve()
    fixed_ids, invalid = _scan_fixed_snapshots(root)

    historical = 0
    for wh_root in market_download.warehouse_roots(root):
        if _valid_warehouse_header(wh_root, "historical-index-warehouse-v1"):
            historical += 1
        else:
            invalid += 1

    market = 0
    for m_root in market_roots(root):
        if _valid_warehouse_header(m_root, "historical-market-warehouse-v1"):
            market += 1
        else:
            invalid += 1

    return {"fixed": len(fixed_ids), "historical": historical, "market": market, "invalid": invalid}


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

    counts_cache_lock = threading.Lock()
    counts_fetch_lock = threading.Lock()
    counts_cache_expires: float = 0.0
    counts_cache_signature: tuple[tuple[str, int], ...] | None = None
    counts_cache_value: tuple[dict[str, int], str] | None = None

    def invalidate_summary_cache() -> None:
        nonlocal cache_expires, cache_value, counts_cache_expires, counts_cache_signature, counts_cache_value
        with cache_lock:
            cache_expires = 0.0
            cache_value = None
        with counts_cache_lock:
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

    def read_database_summary(*, force: bool = False) -> tuple[dict | None, str]:
        nonlocal cache_expires, cache_value
        loader = market_summary
        if loader is None and market_status is None:
            return None, datetime.now(UTC).isoformat()
        now = time.monotonic()
        if not force and summary_cache_ttl > 0:
            with cache_lock:
                if now < cache_expires and cache_value is not None:
                    return cache_value
        with fetch_lock:
            now = time.monotonic()
            if not force and summary_cache_ttl > 0:
                with cache_lock:
                    if now < cache_expires and cache_value is not None:
                        return cache_value
            raw = loader() if loader is not None else market_status()
            summary = {
                "securities": int(raw["securities"]),
                "coverage_start": raw.get("coverage_start"),
                "coverage_end": raw.get("coverage_end"),
            }
            sampled_at = datetime.now(UTC).isoformat()
            with cache_lock:
                if summary_cache_ttl > 0:
                    cache_value = (summary, sampled_at)
                    cache_expires = time.monotonic() + summary_cache_ttl
            return summary, sampled_at

    def read_dataset_counts(*, force: bool = False) -> tuple[dict[str, int], str]:
        nonlocal counts_cache_expires, counts_cache_signature, counts_cache_value
        sig = _inventory_container_signature(resolved_root) if summary_cache_ttl > 0 else ()
        now = time.monotonic()
        if not force and summary_cache_ttl > 0:
            with counts_cache_lock:
                if now < counts_cache_expires and counts_cache_value is not None and counts_cache_signature == sig:
                    return counts_cache_value
        with counts_fetch_lock:
            sig = _inventory_container_signature(resolved_root) if summary_cache_ttl > 0 else ()
            now = time.monotonic()
            if not force and summary_cache_ttl > 0:
                with counts_cache_lock:
                    if now < counts_cache_expires and counts_cache_value is not None and counts_cache_signature == sig:
                        return counts_cache_value
            counts = _dataset_counts(resolved_root)
            counts_sampled_at = datetime.now(UTC).isoformat()
            with counts_cache_lock:
                if summary_cache_ttl > 0:
                    counts_cache_value = (counts, counts_sampled_at)
                    counts_cache_signature = sig
                    counts_cache_expires = time.monotonic() + summary_cache_ttl
            return counts, counts_sampled_at

    @router.get("/dashboard/summary")
    def dashboard_summary(refresh: bool = False):
        try:
            database, sampled_at = read_database_summary(force=refresh)
            counts, counts_sampled_at = read_dataset_counts(force=refresh)
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
