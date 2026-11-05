"""File-backed double of ``PostgresMarketStore`` for tests without a database.

Segments are referenced from ``<root>/catalog.json``; overlapping values must agree,
mirroring the first-published-wins rule enforced by the PostgreSQL store.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
from filelock import FileLock

from finance_quant.data.managed import atomic_json
from finance_quant.data.shared_market import FIELDS, POOL_FIELDS, digest
from finance_quant.data.temporal_audit import restore_valuation_dates

PRICE_FIELDS = ["open", "high", "low", "close", "adj_factor"]


def _conflicts(proposed: pd.DataFrame, existing: pd.DataFrame) -> list[str]:
    return [
        field
        for field in PRICE_FIELDS
        if ((proposed[field] - existing[field]).abs() > (existing[field].abs() * 1e-7 + 1e-8)).any()
    ]


class FileMarketStore:
    def inventory(self, root):
        path = Path(root) / "catalog.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {"schema_version": "file-market-test", "segments": {}}

    def covered_dates(self, document, code, sessions, provider_id):
        covered = pd.DatetimeIndex([])
        for asset in document["segments"].get(code, []):
            if asset["provider_id"] != provider_id or not FIELDS.issubset(asset["columns"]):
                continue
            covered = covered.union(
                sessions[(sessions >= pd.Timestamp(asset["start"])) & (sessions <= pd.Timestamp(asset["end"]))]
            )
        return covered

    def publish_segment(self, root, record, path, *, batch_id):
        columns = set(pq.read_schema(path).names)
        candidate = pd.read_parquet(path, columns=["date", *PRICE_FIELDS])
        overlap = self.covered_dates(
            self.inventory(root), record["code"], pd.DatetimeIndex(candidate.date), record["provider_id"]
        )
        if len(overlap):
            existing, _ = self.read_asset(root, record["code"], overlap, record["provider_id"])
            field = _conflicts(candidate.set_index("date").loc[overlap], existing.set_index("date").loc[overlap])
            if field:
                raise ValueError(f"{record['code']}增量边界{field[0]}冲突，新分区已保留，未加入共享覆盖")
        self._append(root, record["code"], {**record, "path": str(path), "columns": sorted(columns - POOL_FIELDS)})

    def read_asset(self, root, code, expected, provider_id):
        parts, sources = [], []
        for asset in self.inventory(root)["segments"].get(code, []):
            if asset["provider_id"] != provider_id or not FIELDS.issubset(asset["columns"]):
                continue
            if pd.Timestamp(asset["end"]) < expected.min() or pd.Timestamp(asset["start"]) > expected.max():
                continue
            path = Path(asset["path"])
            if digest(path) != asset["sha256"]:
                raise ValueError(f"{code}共享行情校验失败，旧版本未修改")
            columns = [c for c in pq.read_schema(path).names if c not in POOL_FIELDS]
            frame = restore_valuation_dates(pd.read_parquet(path, columns=columns))
            frame = frame.loc[frame.date.isin(expected)]
            if frame.empty:
                continue
            if parts:
                previous = pd.concat(parts, ignore_index=True).drop_duplicates("date").set_index("date")
                overlap = frame.loc[frame.date.isin(previous.index)].set_index("date")
                field = _conflicts(overlap, previous.loc[overlap.index])
                if field:
                    raise ValueError(f"{code}增量边界{field[0]}与已核验版本冲突，保留旧版本并等待复核")
            parts.append(frame)
            sources.append({k: asset[k] for k in ["path", "sha256", "validation_level", "start", "end"]})
        if not parts:
            raise ValueError(f"{code}没有可用共享行情")
        frame = (
            pd.concat(parts, ignore_index=True)
            .drop_duplicates("date", keep="first")
            .sort_values("date")
            .reset_index(drop=True)
        )
        if not pd.DatetimeIndex(frame.date).equals(expected):
            raise ValueError(f"{code}共享行情仍有缺口")
        return frame, sources

    def publish_references(self, components, *, batch_id, checks):
        return None

    def consistent_read(self):
        return nullcontext()

    def seed_warehouse(self, root, warehouse):
        """Register a published historical warehouse's assets, as an operator import would."""
        warehouse = Path(warehouse).resolve()
        manifest = json.loads((warehouse / "manifest.json").read_text(encoding="utf-8"))
        for code, asset in manifest["assets"].items():
            path = (warehouse / asset["file"]).resolve()
            if digest(path) != asset["sha256"]:
                raise ValueError(f"{code}原仓库校验失败")
            columns = set(pq.read_schema(path).names)
            self._append(root, code, {**asset, "path": str(path), "columns": sorted(columns - POOL_FIELDS)})

    def _append(self, root, code, row):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        with FileLock(root / "catalog.lock"):
            document = self.inventory(root)
            rows = document["segments"].setdefault(code, [])
            if not any(r["sha256"] == row["sha256"] and r["path"] == row["path"] for r in rows):
                rows.append(row)
            atomic_json(root / "catalog.json", document)
