"""Per-stock financial statement observations, immutable versions and point-in-time report versions.

Storage layout below ``<data root>/financials``::

    objects/<content id>.parquet   immutable observation tables (one per stock state) and calendars
    stocks/<ts_code>.json          the current object of each stock plus its last fetch time
    calendar.json                  the current exchange calendar object
    versions/<id>.json             immutable version manifests; ``id`` hashes the manifest content
    current.json                   the newest published version

An *observation* is one distinct statement row as the vendor returned it. Re-fetching
appends rows whose content differs and keeps rows the vendor later dropped, so an
in-place correction seen after our first fetch stays distinguishable from the original.
Object ids hash the observation content (not parquet bytes), so re-fetching unchanged
data reproduces the same version id and does not invalidate research results.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from finance_quant.checksums import file_sha256, sha256_text
from finance_quant.data.managed import atomic_json
from finance_quant.data.point_in_time import availability_from_announcement_dates
from finance_quant.data.providers.tushare import (
    FINANCIAL_KEY_COLUMNS,
    FINANCIAL_STATEMENTS,
    FINANCIAL_VALUE_FIELDS,
)
from finance_quant.data.symbols import to_qlib

VERSION_SCHEMA = "financial-version-v1"
REPORT_TYPES = (1, 4, 5)
REPORT_TYPE_LABELS = {1: "合并报表（最新）", 4: "次年公布的上年同期调整数", 5: "调整前原始数"}
# When several versions of one report period become visible at the same moment the
# later entry wins: original (5) < latest (1) < comparative restatement (4).
TYPE_PRIORITY = {5: 0, 1: 1, 4: 2}
OBSERVATION_COLUMNS = [*FINANCIAL_KEY_COLUMNS, *FINANCIAL_VALUE_FIELDS, "row_sha256", "first_seen"]
POLICY = (
    "公告日取实际公告日（缺失时取公告日），自下一交易日09:30起可用；就地更正自首次观测到的下一交易日起可用；"
    "存在调整前原始数时，原始数按原公告日可用，被覆盖的新值待次年同期调整数公布或首次观测后才可用；"
    "缺少公告日的行隔离，不进入研究"
)
_VERSION_ID = re.compile(r"[a-f0-9]{24}")
_HASHED = ["statement", "report_type", "comp_type", "report_period", "ann_date", "f_ann_date", *FINANCIAL_VALUE_FIELDS]


def financial_root(data_root) -> Path:
    return Path(data_root) / "financials"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cell(value):
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    return value


def row_hashes(frame: pd.DataFrame) -> list[str]:
    """Content digest of each statement row; ``update_flag`` and fetch dates are not content."""
    return [
        sha256_text(json.dumps([_cell(value) for value in row], ensure_ascii=False))
        for row in frame[_HASHED].itertuples(index=False, name=None)
    ]


def empty_observations() -> pd.DataFrame:
    frame = pd.DataFrame({column: pd.Series(dtype="float64") for column in OBSERVATION_COLUMNS})
    for column in ["ts_code", "statement", "comp_type", "update_flag", "row_sha256"]:
        frame[column] = frame[column].astype("string")
    frame["report_type"] = frame["report_type"].astype("int64")
    for column in ["report_period", "ann_date", "f_ann_date", "first_seen"]:
        frame[column] = frame[column].astype("datetime64[ns]")
    return frame


def _canonical(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame[OBSERVATION_COLUMNS].copy()
    for column in ["ts_code", "statement", "comp_type", "update_flag", "row_sha256"]:
        frame[column] = frame[column].astype("string")
    frame["report_type"] = frame["report_type"].astype("int64")
    for column in ["report_period", "ann_date", "f_ann_date", "first_seen"]:
        frame[column] = pd.to_datetime(frame[column]).astype("datetime64[ns]")
    for column in FINANCIAL_VALUE_FIELDS:
        frame[column] = frame[column].astype("float64")
    order = ["statement", "report_type", "report_period", "ann_date", "f_ann_date", "first_seen", "row_sha256"]
    return frame.sort_values(order, na_position="first").reset_index(drop=True)


def merge_observations(existing: pd.DataFrame, fresh: pd.DataFrame, seen_on) -> pd.DataFrame:
    """Append rows whose content is new; keep every earlier observation unchanged.

    ``update_flag`` follows the latest response, since it describes the vendor's
    current view rather than the reported numbers.
    """
    seen_on = pd.Timestamp(seen_on).normalize()
    fresh = fresh.copy()
    fresh["row_sha256"] = row_hashes(fresh) if len(fresh) else pd.Series(dtype="string")
    fresh = fresh.drop_duplicates("row_sha256", keep="last")
    existing = existing.copy() if existing is not None and len(existing) else empty_observations()
    flags = dict(zip(fresh.row_sha256, fresh.update_flag))
    known = existing.row_sha256.isin(flags)
    existing.loc[known, "update_flag"] = existing.loc[known, "row_sha256"].map(flags)
    added = fresh.loc[~fresh.row_sha256.isin(set(existing.row_sha256))].copy()
    added["first_seen"] = seen_on
    parts = [part for part in (existing, added) if len(part)]
    return _canonical(pd.concat(parts, ignore_index=True)) if parts else empty_observations()


def content_id(frame: pd.DataFrame) -> str:
    lines = sorted(
        f"{sha}|{_cell(seen)}|{_cell(flag)}"
        for sha, seen, flag in zip(frame.row_sha256, frame.first_seen, frame.update_flag)
    )
    return sha256_text("\n".join([VERSION_SCHEMA, *lines]))


def quarantined_rows(frame: pd.DataFrame) -> pd.Series:
    """Rows that can never be placed in time: unknown period or announcement, or impossible order."""
    announced = frame.f_ann_date.fillna(frame.ann_date)
    return frame.report_period.isna() | announced.isna() | announced.lt(frame.report_period)


@dataclass(frozen=True)
class FinancialBinding:
    """A research run pinned to one immutable financial version."""

    root: Path
    version: str

    def __post_init__(self):
        if not isinstance(self.version, str) or not _VERSION_ID.fullmatch(self.version):
            raise ValueError("财务数据版本编号无效")
        object.__setattr__(self, "root", Path(self.root))

    def as_dict(self) -> dict:
        return {"version": self.version}


class FinancialStore:
    def __init__(self, root):
        self.root = Path(root)

    # -- objects --------------------------------------------------------------
    def _object_path(self, identifier: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", identifier):
            raise ValueError("财务对象编号无效")
        return self.root / "objects" / f"{identifier}.parquet"

    def _put(self, identifier: str, frame: pd.DataFrame) -> str:
        path = self._object_path(identifier)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(dir=path.parent, suffix=".pending")
            os.close(descriptor)
            try:
                frame.to_parquet(name, index=False)
                os.replace(name, path)
            finally:
                Path(name).unlink(missing_ok=True)
        return file_sha256(path)

    def _get(self, identifier: str, sha256: str) -> pd.DataFrame:
        path = self._object_path(identifier)
        if file_sha256(path) != sha256:
            raise ValueError("财务数据文件校验失败，请重新下载财务数据")
        return pd.read_parquet(path)

    def _read(self, path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    # -- per-stock state -------------------------------------------------------
    def pointer(self, ts_code: str) -> dict | None:
        return self._read(self.root / "stocks" / f"{ts_code}.json")

    def pointers(self) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((self.root / "stocks").glob("*.json"))]

    def observations(self, ts_code: str) -> pd.DataFrame:
        pointer = self.pointer(ts_code)
        return self._get(pointer["object"], pointer["sha256"]) if pointer else empty_observations()

    def save_stock(self, ts_code: str, observations: pd.DataFrame, *, fetched_at: str) -> dict:
        frame = _canonical(observations) if len(observations) else empty_observations()
        identifier = content_id(frame)
        usable = frame.loc[~quarantined_rows(frame)]
        pointer = {
            "ts_code": ts_code,
            "object": identifier,
            "sha256": self._put(identifier, frame),
            "rows": len(frame),
            "quarantined": int(len(frame) - len(usable)),
            "latest_period": usable.report_period.max().strftime("%Y-%m-%d") if len(usable) else None,
            "fetched_at": fetched_at,
        }
        atomic_json(self.root / "stocks" / f"{ts_code}.json", pointer)
        return pointer

    # -- calendar -------------------------------------------------------------
    def save_calendar(self, calendar: pd.DataFrame) -> dict:
        frame = calendar[["date", "is_open"]].sort_values("date").reset_index(drop=True)
        frame["date"] = pd.to_datetime(frame["date"]).astype("datetime64[ns]")
        frame["is_open"] = frame["is_open"].astype(bool)
        payload = frame.to_csv(index=False, date_format="%Y-%m-%d")
        identifier = sha256_text(VERSION_SCHEMA + "\ncalendar\n" + payload)
        pointer = {
            "object": identifier,
            "sha256": self._put(identifier, frame),
            "start": frame.date.min().strftime("%Y-%m-%d"),
            "end": frame.date.max().strftime("%Y-%m-%d"),
        }
        atomic_json(self.root / "calendar.json", pointer)
        return pointer

    def calendar_pointer(self) -> dict | None:
        return self._read(self.root / "calendar.json")

    # -- versions -------------------------------------------------------------
    def publish(self) -> dict:
        calendar = self.calendar_pointer()
        pointers = self.pointers()
        if calendar is None or not pointers:
            raise ValueError("还没有可发布的财务数据")
        content = {
            "schema_version": VERSION_SCHEMA,
            "policy": POLICY,
            "calendar": {k: calendar[k] for k in ("object", "sha256", "start", "end")},
            "stocks": {
                p["ts_code"]: {k: p[k] for k in ("object", "sha256", "rows", "quarantined", "latest_period")}
                for p in pointers
            },
        }
        identifier = sha256_text(json.dumps(content, sort_keys=True, ensure_ascii=False))[:24]
        path = self.root / "versions" / f"{identifier}.json"
        if not path.exists():
            atomic_json(path, {**content, "id": identifier, "created_at": utc_now()})
        atomic_json(self.root / "current.json", {"id": identifier})
        return self.version(identifier)

    def version(self, identifier: str) -> dict:
        if not isinstance(identifier, str) or not _VERSION_ID.fullmatch(identifier):
            raise ValueError("财务数据版本编号无效")
        manifest = self._read(self.root / "versions" / f"{identifier}.json")
        if manifest is None:
            raise ValueError("财务数据版本不存在，请刷新后重新提交")
        content = {key: manifest[key] for key in ("schema_version", "policy", "calendar", "stocks")}
        digest = sha256_text(json.dumps(content, sort_keys=True, ensure_ascii=False))[:24]
        if manifest.get("id") != identifier or digest != identifier or manifest["schema_version"] != VERSION_SCHEMA:
            raise ValueError("财务数据版本校验失败")
        return manifest

    def current(self) -> dict | None:
        pointer = self._read(self.root / "current.json")
        return self.version(pointer["id"]) if pointer else None

    def load(self, manifest: dict, ts_codes) -> tuple[pd.DataFrame, pd.DatetimeIndex]:
        """Observations of the requested stocks (those the version holds) and the exchange sessions."""
        parts = [
            self._get(entry["object"], entry["sha256"])
            for code in sorted(set(ts_codes))
            if (entry := manifest["stocks"].get(code)) and entry["rows"]
        ]
        observations = _canonical(pd.concat(parts, ignore_index=True)) if parts else empty_observations()
        calendar = self._get(manifest["calendar"]["object"], manifest["calendar"]["sha256"])
        return observations, pd.DatetimeIndex(calendar.loc[calendar.is_open, "date"])


def covered_codes(manifest: dict | None, ts_codes) -> list[str]:
    """Stocks that hold at least one report usable for research."""
    if not manifest:
        return []
    stocks = manifest["stocks"]
    return sorted(
        code for code in set(ts_codes) if code in stocks and stocks[code]["rows"] > stocks[code]["quarantined"]
    )


def _later(first: pd.Series, second: pd.Series) -> pd.Series:
    """Elementwise later time; an unknown second time means "never visible"."""
    result = first.where(first >= second, second)
    return result.where(second.notna())


def report_versions(observations: pd.DataFrame, sessions) -> pd.DataFrame:
    """Rebuild when each statement value became visible (see :data:`POLICY`).

    Returns one row per visible version with ``instrument``, ``statement``,
    ``report_period``, ``available_at`` (UTC), ``priority`` and ``revision`` plus values.
    Quarantined rows and versions that never become visible are excluded.
    """
    columns = ["instrument", "statement", "report_type", "report_period", "available_at", "priority", "revision"]
    if observations.empty:
        return pd.DataFrame(columns=[*columns, *FINANCIAL_VALUE_FIELDS])
    frame = observations.loc[~quarantined_rows(observations) & observations.report_type.isin(REPORT_TYPES)].reset_index(
        drop=True
    )
    frame["announced"] = frame.f_ann_date.fillna(frame.ann_date)
    frame["available_at"] = availability_from_announcement_dates(frame.announced, sessions)
    seen = availability_from_announcement_dates(frame.first_seen, sessions)
    # ``update_flag=1`` marks the vendor's current view of a row. When the same
    # announcement date carries both flags, only the current one is a real version:
    # the superseded row is an artefact of the vendor keeping both, not information
    # a reader of the original report would have seen.
    same_announcement = ["ts_code", "statement", "report_type", "report_period", "announced"]
    frame["_flag"] = frame.update_flag.eq("1").fillna(False).astype(int)
    frame["_has_current"] = frame.groupby(same_announcement).update_flag.transform(lambda values: values.eq("1").any())
    frame = frame.loc[~((frame._flag == 0) & frame._has_current)].drop(columns="_has_current")
    # Different numbers announced under one date that are not flagged apart: the first
    # observed stays on the announcement date; later ones are in-place corrections that
    # only become usable once we observed them.
    frame = frame.sort_values(
        ["ts_code", "statement", "report_type", "report_period", "announced", "first_seen", "_flag"]
    )
    seen = seen.loc[frame.index]
    corrected = frame.groupby(["ts_code", "statement", "report_type", "report_period", "announced"]).cumcount().gt(0)
    frame.loc[corrected, "available_at"] = _later(frame.available_at, seen)[corrected]
    # An original (type 5) exists: type-1 values announced no later than it overwrote
    # the original in place. They wait for the next year's comparative (type 4) or our
    # first observation, whichever comes first.
    period = ["ts_code", "statement", "report_period"]
    originals = frame.loc[frame.report_type.eq(5)].groupby(period).announced.min().rename("_original")
    frame = frame.join(originals, on=period)
    comparative = (
        frame.loc[frame.report_type.eq(4) & frame.announced.gt(frame._original)]
        .groupby(period)
        .available_at.min()
        .rename("_comparative")
    )
    frame = frame.join(comparative, on=period)
    overwritten = frame.report_type.eq(1) & frame._original.notna() & frame.announced.le(frame._original)
    replacement = frame._comparative.where(frame._comparative.lt(seen) | seen.isna(), seen)
    frame.loc[overwritten, "available_at"] = _later(frame.available_at, replacement)[overwritten]
    frame = frame.loc[frame.available_at.notna()].copy()
    frame["priority"] = frame.report_type.map(TYPE_PRIORITY).astype("int64")
    frame["instrument"] = frame.ts_code.map(to_qlib)
    frame = frame.sort_values(
        ["instrument", "statement", "report_period", "available_at", "priority", "first_seen", "row_sha256"]
    )
    frame["revision"] = frame.groupby(["instrument", "statement", "report_period"]).cumcount().astype("int64")
    frame["available_at"] = frame.available_at.astype("datetime64[ns, UTC]")
    return frame[[*columns, *FINANCIAL_VALUE_FIELDS]].reset_index(drop=True)


def statement_fields(statement: str) -> tuple[str, ...]:
    return FINANCIAL_STATEMENTS[statement][1]
