"""Validate public registration inputs and resolve their server-managed source."""

from __future__ import annotations

import json
import re
from datetime import date
from io import BytesIO
from pathlib import Path
from stat import S_ISREG

import pandas as pd
from sqlalchemy import Connection, Engine, RowMapping, select

from finance_quant.application.forward_registration import ForwardCalendar, _freeze_registration
from finance_quant.application.forward_score import _market_dates
from finance_quant.application.research_requests import MAX_CODES
from finance_quant.checksums import sha256_bytes
from finance_quant.data.market import _is_internal_snapshot
from finance_quant.data.symbols import TS_CODE
from finance_quant.persistence.forward_registration import RegistrationReceipt, register_and_confirm
from finance_quant.persistence.market import snapshot_dataset_record
from finance_quant.persistence.schema import dataset
from finance_quant.web.write_requests import research_write_gate

_FIELDS = {"name", "signal_date", "pool_codes", "top_k", "factor_window", "factor_direction", "dataset_id"}
_DATASET_ID = r"snapshot-[0-9a-f]{64}"
_FILES = {"market.parquet", "benchmark.parquet", "calendar.parquet"}
_SOURCE_SCHEMAS = {"market-v1", "market-v2", "market-v3", "market-v4-shared"}


def _registration_request(payload: object) -> tuple[dict, date]:
    if not isinstance(payload, dict) or payload.keys() != _FIELDS:
        raise ValueError("前瞻登记必须提交指定的策略字段与来源数据集标识")
    signal = payload["signal_date"]
    if not isinstance(signal, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", signal):
        raise ValueError("信号日必须采用 YYYY-MM-DD 格式")
    try:
        signal_date = date.fromisoformat(signal)
    except ValueError:
        raise ValueError("信号日无效") from None
    name, pool = payload["name"], payload["pool_codes"]
    if not isinstance(name, str) or not name.strip():
        raise ValueError("登记名称不能为空")
    if not isinstance(pool, list) or not 2 <= len(pool) <= MAX_CODES or not all(isinstance(code, str) for code in pool):
        raise ValueError(f"固定股票池必须包含 2 至 {MAX_CODES} 个股票代码")
    codes = sorted(code.strip().upper() for code in pool)
    if not all(TS_CODE.fullmatch(code) for code in codes):
        raise ValueError("固定股票池含无效 A 股代码")
    if len(set(codes)) != len(codes):
        raise ValueError("固定股票池不能包含重复代码")
    top_k, window, direction = (payload[key] for key in ("top_k", "factor_window", "factor_direction"))
    if type(top_k) is not int or not 0 < top_k < len(codes):
        raise ValueError("前 K 必须是大于零且小于固定池股票数的整数")
    if type(window) is not int or not 2 <= window <= 252 or direction not in ("high", "low"):
        raise ValueError("动量窗口必须是 2 至 252 的整数，方向必须是 high 或 low")
    dataset_id = payload["dataset_id"]
    if not isinstance(dataset_id, str) or not re.fullmatch(_DATASET_ID, dataset_id):
        raise ValueError("来源数据集标识无效")
    return {**payload, "name": name.strip(), "pool_codes": codes, "factor": "momentum"}, signal_date


def _regular_file(path: Path) -> Path:
    if path.resolve(strict=True) != path or not S_ISREG(path.lstat().st_mode):
        raise ValueError("Invalid managed snapshot file")
    return path


def _managed_manifest(
    storage_root: Path, relative_path: str, *, max_bytes: int | None = None
) -> tuple[Path, bytes, dict]:
    relative = Path(relative_path)
    source = storage_root / relative
    if (
        relative.anchor
        or ".." in relative.parts
        or source == storage_root
        or source.resolve(strict=True) != source
        or (source / "rejected.json").exists()
    ):
        raise ValueError("Invalid managed snapshot path")
    with _regular_file(source / "manifest.json").open("rb") as stream:
        raw = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
    if max_bytes is not None and len(raw) > max_bytes:
        raise ValueError("Managed snapshot manifest is too large")
    manifest = json.loads(raw)
    if not isinstance(manifest, dict):
        raise ValueError("Invalid managed snapshot manifest")
    provider_id, files = manifest.get("provider_id"), manifest.get("files")
    if (
        manifest.get("schema_version") not in _SOURCE_SCHEMAS
        or _is_internal_snapshot(source, manifest)
        or not isinstance(provider_id, str)
        or not re.fullmatch(r"[0-9a-f]{64}", provider_id)
        or not isinstance(files, dict)
        or set(files) != _FILES
        or ("snapshot_path" in manifest and manifest["snapshot_path"] != f"snapshots/{source.name}")
    ):
        raise ValueError("Invalid managed snapshot identity")
    for name in files:
        _regular_file(source / name)
    return source, raw, manifest


def _coverage(manifest: dict) -> tuple[str | None, str | None]:
    try:
        start, end = (date.fromisoformat(manifest[key]) for key in ("start", "end"))
    except (KeyError, TypeError, ValueError):
        return None, None
    return (start.isoformat(), end.isoformat()) if start <= end else (None, None)


def _source_projection():
    manifest = dataset.c.manifest
    return select(
        dataset.c.id,
        dataset.c.relative_path,
        manifest["source_manifest_sha256"].astext.label("digest"),
        manifest["source_id"].astext.label("name"),
    ).where(
        dataset.c.status == "ready",
        manifest["kind"].astext == "market_snapshot",
        manifest["schema_version"].astext.in_(_SOURCE_SCHEMAS),
        dataset.c.id.op("~")(f"^{_DATASET_ID}$"),
    )


def _source_option(root: Path, row: RowMapping) -> dict | None:
    try:
        _, raw, source = _managed_manifest(root, row["relative_path"], max_bytes=128 * 1024)
    except (OSError, RecursionError, TypeError, ValueError):
        return None
    digest = sha256_bytes(raw)
    if digest != row["digest"] or row["id"] != "snapshot-" + digest:
        return None
    name = source.get("id")
    if not isinstance(name, str) or name != row["name"] or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", name):
        name = "行情快照"
    start, end = _coverage(source)
    return {"id": row["id"], "name": name, "start": start, "end": end}


def registration_sources(connection: Connection, storage_root: Path, *, offset: int, limit: int) -> dict:
    """Project ready registrations, reading only bounded manifest metadata and file attributes."""
    rows = (
        connection.execute(
            _source_projection().order_by(dataset.c.created_at.desc(), dataset.c.id).offset(offset).limit(limit + 1)
        )
        .mappings()
        .all()
    )
    root = storage_root.resolve()
    items = [item for row in rows[:limit] if (item := _source_option(root, row)) is not None]
    return {"items": items, "has_more": len(rows) > limit}


def registration_source(connection: Connection, storage_root: Path, dataset_id: str) -> dict | None:
    """Recheck one selection independently of its position in the catalog."""
    row = connection.execute(_source_projection().where(dataset.c.id == dataset_id)).mappings().one_or_none()
    return _source_option(storage_root.resolve(), row) if row is not None else None


def _source_calendar(connection: Connection, storage_root: Path, dataset_id: str) -> tuple[bytes, str]:
    saved = connection.execute(select(dataset).where(dataset.c.id == dataset_id)).mappings().one_or_none()
    if (
        saved is None
        or saved["status"] != "ready"
        or not isinstance(saved["manifest"], dict)
        or saved["manifest"].get("kind") != "market_snapshot"
    ):
        raise ValueError("来源数据集不存在或尚未就绪")
    try:
        source, raw, _ = _managed_manifest(storage_root, saved["relative_path"])
        record = snapshot_dataset_record(source, storage_root, request={})
        if (
            record["id"] != dataset_id
            or record["manifest"] != saved["manifest"]
            or sha256_bytes(raw) != record["manifest"]["source_manifest_sha256"]
        ):
            raise ValueError("Invalid managed snapshot evidence")
        calendar = _regular_file(source / "calendar.parquet").read_bytes()
        digest = sha256_bytes(calendar)
        if digest != record["manifest"]["files"]["calendar.parquet"]:
            raise ValueError("Invalid managed calendar evidence")
        return calendar, digest
    except (OSError, KeyError, TypeError, ValueError):
        raise ValueError("来源数据集的受管清单或文件摘要无法验证") from None


def _calendar_for_registration(raw: bytes, digest: str, signal_date: date, window: int) -> ForwardCalendar:
    try:
        frame = pd.read_parquet(BytesIO(raw), columns=["date", "is_open"]).sort_values("date")
        dates = _market_dates(frame)
        opened = frame["is_open"]
        if (
            dates.empty
            or not dates.equals(pd.date_range(dates[0], dates[-1]))
            or not pd.api.types.is_bool_dtype(opened)
            or opened.isna().any()
        ):
            raise ValueError("Invalid managed calendar dates")
        sessions = tuple(dates[opened.to_numpy(dtype=bool)].date)
    except (OSError, KeyError, TypeError, ValueError):
        raise ValueError("受管交易日历无效或缺少连续日期") from None
    if signal_date not in sessions:
        raise ValueError("信号日必须是受管交易日历中的交易日")
    position = sessions.index(signal_date)
    if position < window:
        raise ValueError("受管交易日历缺少动量预热交易日")
    if len(sessions[position + 1 :]) < 2:
        raise ValueError("受管交易日历未覆盖入场日与退出日")
    return ForwardCalendar("cal-" + digest[:36], sessions)


def submit_forward_registration(engine: Engine, storage_root: Path, payload: object, key: str) -> RegistrationReceipt:
    document, signal_date = _registration_request(payload)
    with research_write_gate(engine) as connection:
        # The maintenance lock and all three seal transactions share one checkout.
        def freeze() -> dict:
            calendar, digest = _source_calendar(connection, storage_root.resolve(), document["dataset_id"])
            frozen = _calendar_for_registration(calendar, digest, signal_date, document["factor_window"])
            return _freeze_registration(document, signal_date, frozen)

        return register_and_confirm(engine, key=key, request=document, freeze=freeze, connection=connection)
