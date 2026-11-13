"""Immutable, checksum-verified large result tables with bounded row reads."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from finance_quant.checksums import file_sha256

JSON_COLUMNS = {"raw", "scores", "contributions", "reasons", "processed", "before_scores", "before_contributions"}


class ResultTableError(ValueError):
    pass


sha256 = file_sha256


def _section(document, path):
    keys = path.split("/")
    current = document
    for key in keys[:-1]:
        current = current[key]
    return current, keys[-1]


class StreamingTables:
    """Bounded immutable table writer; no result is visible until finish succeeds."""

    def __init__(self, store, category, identifier):
        self.directory = store.path(category, identifier).parent
        self.directory.mkdir(parents=True, exist_ok=True)
        self.states = {}

    def __enter__(self):
        return self

    def append(self, path, rows):
        if not rows:
            return
        if path not in self.states:
            descriptor, temporary = tempfile.mkstemp(dir=self.directory, suffix=".parquet")
            os.close(descriptor)
            self.states[path] = {
                "temporary": Path(temporary),
                "writer": None,
                "buffer": [],
                "columns": sorted(JSON_COLUMNS.intersection(rows[0])),
                "rows": 0,
            }
        state = self.states[path]
        state["buffer"].extend(rows)
        state["rows"] += len(rows)
        while len(state["buffer"]) >= 8192:
            self._flush(state, 8192)

    @staticmethod
    def _flush(state, count):
        batch = []
        for row in state["buffer"][:count]:
            if any(isinstance(v, float) and not math.isfinite(v) for v in row.values()):
                raise ValueError("Nonfinite result table value")
            batch.append(
                {
                    k: json.dumps(v, ensure_ascii=False, allow_nan=False) if k in state["columns"] else v
                    for k, v in row.items()
                }
            )
        table = pa.Table.from_pylist(batch)
        if state["writer"] is None:
            state["schema"] = table.schema
            state["writer"] = pq.ParquetWriter(state["temporary"], table.schema, compression="snappy")
        state["writer"].write_table(table.cast(state["schema"]))
        del state["buffer"][:count]

    def finish(self):
        references = {}
        for path, state in self.states.items():
            if state["buffer"]:
                self._flush(state, len(state["buffer"]))
            state["writer"].close()
            state["writer"] = None
            digest = sha256(state["temporary"])
            filename = path.replace("/", "-") + ".parquet"
            destination = self.directory / filename
            try:
                os.link(state["temporary"], destination)
            except FileExistsError:
                if sha256(destination) != digest:
                    raise ValueError("Immutable result table conflict") from None
            references[path] = {
                "file": filename,
                "sha256": digest,
                "rows": state["rows"],
                "json_columns": state["columns"],
            }
        return references

    def __exit__(self, *args):
        for state in self.states.values():
            if state["writer"] is not None:
                state["writer"].close()
            state["temporary"].unlink(missing_ok=True)


def factor_frame(store, identifier, result, *, include_raw=False):
    """Read only the numeric signal/diagnostic inputs, not rich scoring records."""

    columns = ["date", "instrument", "available_at", "composite"] + (["raw"] if include_raw else [])
    if "history" not in result.get("tables", {}):
        return pd.DataFrame(result["history"])[columns]
    table = ds.dataset(table_path(store, "factors", identifier, result, "history"), format="parquet")
    frame = table.to_table(columns=columns).to_pandas()
    if include_raw:
        frame["raw"] = frame.raw.map(json.loads)
    return frame


def factor_predictions(store, identifier, result):
    """Decode raw values a batch at a time, retaining numeric columns only."""

    names = list(result["metadata"]["weights"])
    parts = []
    value_field = "processed" if result["metadata"].get("neutralization", {}).get("mode") == "industry" else "raw"
    columns = ["date", "instrument", "available_at", "composite", value_field]
    if "history" in result.get("tables", {}):
        dataset = ds.dataset(table_path(store, "factors", identifier, result, "history"), format="parquet")
        batches = (batch.to_pandas() for batch in dataset.scanner(columns=columns, batch_size=8192).to_batches())
    else:
        batches = (
            pd.DataFrame(result["history"][i : i + 8192])[columns] for i in range(0, len(result["history"]), 8192)
        )
    for batch in batches:
        raw = [json.loads(row) if isinstance(row, str) else row for row in batch[value_field]]
        numeric = batch.drop(columns=value_field)
        for name in names:
            numeric["raw_" + name] = [row[name] for row in raw]
        parts.append(numeric)
    return (
        pd.concat(parts, ignore_index=True)
        if parts
        else pd.DataFrame(columns=columns[:-1] + ["raw_" + n for n in names])
    )


def publish_tables(store, category, identifier, document, paths):
    directory = store.path(category, identifier).parent
    directory.mkdir(parents=True, exist_ok=True)
    compact = {**document, "tables": {}}
    if category == "labels":
        raise ValueError("Unsupported result category")
    # Only shallow-copy containers along paths, keeping the working data intact.
    if category == "evaluations":
        compact["labels"] = {k: dict(v) for k, v in document["labels"].items()}
    for path in paths:
        original, key = _section(document, path)
        rows = original[key]
        if not rows:
            continue
        filename = path.replace("/", "-") + ".parquet"
        destination = directory / filename
        descriptor, temporary = tempfile.mkstemp(dir=directory, suffix=".parquet")
        os.close(descriptor)
        writer = None
        columns = sorted(JSON_COLUMNS.intersection(rows[0]))
        try:
            for start in range(0, len(rows), 8192):
                batch = []
                for row in rows[start : start + 8192]:
                    if any(isinstance(v, float) and not math.isfinite(v) for v in row.values()):
                        raise ValueError("Nonfinite result table value")
                    batch.append(
                        {
                            k: json.dumps(v, ensure_ascii=False, allow_nan=False) if k in columns else v
                            for k, v in row.items()
                        }
                    )
                table = pa.Table.from_pylist(batch)
                if writer is None:
                    writer = pq.ParquetWriter(temporary, table.schema, compression="snappy")
                writer.write_table(table)
            writer.close()
            writer = None
            digest = sha256(Path(temporary))
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if sha256(destination) != digest:
                    raise ValueError("Immutable result table conflict") from None
            compact["tables"][path] = {"file": filename, "sha256": digest, "rows": len(rows), "json_columns": columns}
            parent, key = _section(compact, path)
            parent[key] = []
            if category == "evaluations":
                parent["observations_storage"] = "external_table"
        finally:
            if writer is not None:
                writer.close()
            Path(temporary).unlink(missing_ok=True)
    store.publish(category, identifier, compact)


def table_path(store, category, identifier, result, path):
    try:
        metadata = result["tables"][path]
        root = store.path(category, identifier).parent.resolve()
        target = (root / metadata["file"]).resolve()
        if not target.is_relative_to(root) or sha256(target) != metadata["sha256"]:
            raise ValueError("checksum mismatch")
        return target
    except (OSError, ValueError, TypeError, KeyError):
        raise ResultTableError(
            "Result table checksum mismatch: 结果明细缺失或损坏，请重新运行该配置；其他结果不受影响"
        ) from None


def read_rows(store, category, identifier, result, path, *, filters=None, offset=0, limit=None):
    filters = filters or {}
    if path not in result.get("tables", {}):
        parent, key = _section(result, path)
        rows = [r for r in parent.get(key, []) if all(r.get(k) == v for k, v in filters.items())]
        return rows[offset : None if limit is None else offset + limit], len(rows)
    metadata = result["tables"][path]
    dataset = ds.dataset(table_path(store, category, identifier, result, path), format="parquet")
    expression = None
    for key, value in filters.items():
        term = ds.field(key) == value
        expression = term if expression is None else expression & term
    total = dataset.count_rows(filter=expression)
    output = []
    skip = offset
    for batch in dataset.scanner(filter=expression, batch_size=8192).to_batches():
        if skip >= len(batch):
            skip -= len(batch)
            continue
        batch = batch.slice(skip)
        skip = 0
        if limit is not None:
            batch = batch.slice(0, min(len(batch), limit - len(output)))
        rows = batch.to_pylist()
        for row in rows:
            for key in metadata["json_columns"]:
                row[key] = json.loads(row[key])
        output.extend(rows)
        if limit is not None and len(output) >= limit:
            break
    return output, total


def hydrate_tables(store, category, identifier, result):
    for path in result["tables"]:
        rows, _ = read_rows(store, category, identifier, result, path)
        parent, key = _section(result, path)
        parent[key] = rows
