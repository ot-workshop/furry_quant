"""Atomic current-value publication with revisions and immutable research inputs."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select, text, tuple_
from sqlalchemy.dialects.postgresql import insert

from finance_quant.checksums import file_sha256, sha256_bytes
from finance_quant.data.warehouse_files import warehouse_files

from .database import make_engine
from .schema import MARKET_TABLES, data_release, data_revision, dataset, source_check


def json_value(value):
    """Record business dates without pretending they are publication timestamps."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


def chunks(rows, size=500):
    for offset in range(0, len(rows), size):
        yield rows[offset : offset + size]


file_digest = file_sha256


class MarketRepository:
    def __init__(self, engine=None):
        self.engine = engine or make_engine()

    def publish(self, components, *, source, batch_id, checks=(), allow_source_change=False):
        """Publish an already validated unit. Network/validation belongs to caller.

        Missing rows are never interpreted as deletion. Repeated identical values
        update source checks but do not create a release. A caller must explicitly
        approve replacing another source (for example a verified fallback repair).
        """
        if set(components) - MARKET_TABLES.keys():
            raise ValueError("Unknown market component")
        fetched = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            # Serialize market publishers, independently of the queue process.

            connection.execute(text("SELECT pg_advisory_xact_lock(663420121)"))
            existing_batch = (
                connection.execute(select(data_release).where(data_release.c.batch_id == batch_id)).mappings().first()
            )
            if existing_batch:
                return dict(existing_batch)
            changes = []
            revisions = []
            for name, incoming in components.items():
                table = MARKET_TABLES[name]
                keys = [column.name for column in table.primary_key]
                normalized = []
                seen = set()
                for record in incoming:
                    if set(record) - set(table.c.keys()):
                        raise ValueError(f"Unknown fields for {name}: {sorted(set(record) - set(table.c.keys()))}")
                    row = dict(record)
                    key = tuple(row[field] for field in keys)
                    if key in seen:
                        raise ValueError(f"Duplicate {name} row key")
                    seen.add(key)
                    row.setdefault("source", source)
                    row.setdefault("quality", "single_source_passed")
                    row.setdefault("fetched_at", fetched)
                    row.pop("release_id", None)
                    normalized.append(row)
                for batch in chunks(normalized):
                    old_rows = (
                        connection.execute(
                            select(table).where(
                                tuple_(*(table.c[key] for key in keys)).in_(
                                    [tuple(row[key] for key in keys) for row in batch]
                                )
                            )
                        )
                        .mappings()
                        .all()
                    )
                    previous = {tuple(row[key] for key in keys): dict(row) for row in old_rows}
                    for row in batch:
                        old = previous.get(tuple(row[key] for key in keys))
                        if (
                            old
                            and not allow_source_change
                            and (old["source"] != row["source"] or old.get("provider_id") != row.get("provider_id"))
                        ):
                            raise ValueError(f"{name} source conflict requires explicit validation")
                        fields = [
                            key
                            for key in row
                            if key not in {*keys, "fetched_at", "release_id"}
                            and (old is None or old.get(key) != row[key])
                        ]
                        if old is not None and not fields:
                            continue
                        # Preserve unspecified optional fields, while explicit nulls are changes.
                        merged = {**(old or {}), **row}
                        merged.pop("release_id", None)
                        changes.append((table, keys, merged))
                        revisions.append(
                            dict(
                                table_name=name,
                                row_key=json_value({key: row[key] for key in keys}),
                                old_values=json_value({key: old.get(key) for key in fields}) if old else None,
                                new_values=json_value({key: row[key] for key in fields}),
                                changed_fields=fields,
                                source=row["source"],
                                fetched_at=row["fetched_at"],
                            )
                        )
            for check in checks:
                record = {"source": source, "checked_at": fetched, "details": {}, **check}
                statement = insert(source_check).values(record)
                connection.execute(
                    statement.on_conflict_do_update(
                        index_elements=["source", "component", "partition_key"],
                        set_={key: statement.excluded[key] for key in ("checked_at", "status", "row_count", "details")},
                    )
                )
            if not changes:
                return {"id": None, "batch_id": batch_id, "changed_rows": 0}
            release_id = connection.execute(
                data_release.insert()
                .values(
                    source=source,
                    batch_id=batch_id,
                    changed_rows=len(changes),
                    details={"components": sorted(components)},
                )
                .returning(data_release.c.id)
            ).scalar_one()
            for table_name in components:
                table = MARKET_TABLES[table_name]
                rows = [{**row, "release_id": release_id} for item, keys, row in changes if item is table]
                keys = [column.name for column in table.primary_key]
                # Rows from this unit may contain different optional fields: fill
                # omitted columns from merged old values or schema nullable defaults.
                for batch in chunks(rows):
                    columns = set().union(*(row.keys() for row in batch))
                    batch = [{column: row.get(column) for column in columns} for row in batch]
                    statement = insert(table).values(batch)
                    connection.execute(
                        statement.on_conflict_do_update(
                            index_elements=keys,
                            set_={column: statement.excluded[column] for column in columns - set(keys)},
                        )
                    )
            for batch in chunks(revisions):
                connection.execute(data_revision.insert(), [{**row, "release_id": release_id} for row in batch])
            return {"id": release_id, "batch_id": batch_id, "changed_rows": len(changes)}

    @staticmethod
    def query(name, *, codes=None, start=None, end=None, provider_id=None):
        table = MARKET_TABLES[name]
        query = select(table)
        if codes is not None:
            query = query.where(table.c.ts_code.in_(codes))
        if provider_id is not None:
            query = query.where(table.c.provider_id == provider_id)
        if "trade_date" in table.c:
            if start is not None:
                query = query.where(table.c.trade_date >= start)
            if end is not None:
                query = query.where(table.c.trade_date <= end)
        elif "effective_from" in table.c:
            if start is not None:
                query = query.where((table.c.effective_to == None) | (table.c.effective_to >= start))  # noqa: E711
            if end is not None:
                query = query.where(table.c.effective_from <= end)
        return query.order_by(*table.primary_key)

    def read(self, name, *, connection=None, **filters):
        if connection is not None:
            return [dict(row) for row in connection.execute(self.query(name, **filters)).mappings()]
        with self.engine.connect() as connection:
            return self.read(name, connection=connection, **filters)

    def materialize(self, components, storage_root, *, request):
        """Freeze related tables under one REPEATABLE READ snapshot.

        Files are complete before the ready row is committed. A crash between the
        two leaves an unreferenced directory, never a falsely ready dataset.
        """

        root = Path(storage_root).resolve()
        identifier = "dataset-" + uuid.uuid4().hex
        relative = Path("datasets") / identifier
        destination = root / relative
        temporary = root / "datasets" / ("." + identifier)
        temporary.mkdir(parents=True)
        manifest = {"id": identifier, "components": {}, "request": json_value(request)}
        try:
            with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
                with connection.begin():
                    for name, filters in components.items():
                        filename = name + ".parquet"
                        rows = self.read(name, connection=connection, **filters)
                        if not rows:
                            raise ValueError(f"Required dataset component is empty: {name}")
                        table = pa.Table.from_pylist(rows)
                        pq.write_table(table, temporary / filename, compression="zstd", row_group_size=65536)
                        count = pq.ParquetFile(temporary / filename).metadata.num_rows
                        if count != len(rows):
                            raise ValueError("Incomplete dataset export")
                        manifest["components"][name] = {
                            "file": filename,
                            "rows": count,
                            "sha256": file_digest(temporary / filename),
                            "release_ids": sorted({row["release_id"] for row in rows}),
                        }
            (temporary / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.rename(temporary, destination)
            with self.engine.begin() as connection:
                connection.execute(
                    dataset.insert().values(
                        id=identifier,
                        request=json_value(request),
                        manifest=manifest,
                        relative_path=relative.as_posix(),
                        status="ready",
                    )
                )
            return manifest
        except BaseException:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise

    def register_snapshot(self, snapshot_root, storage_root, *, request):
        """Register an immutable file snapshot as a reproducible research input."""
        record = snapshot_dataset_record(snapshot_root, storage_root, request=request)
        self._register_dataset(record)
        return record

    def freeze_historical_warehouse(self, warehouse_root, storage_root, *, expected_sha, request):
        """Pin one historical-warehouse manifest using hard links when possible.

        The live warehouse is incrementally replaceable.  Research and backups
        therefore use a content-addressed directory whose manifest and assets can
        no longer move when a later download publishes a new warehouse version.
        """
        source = Path(warehouse_root).resolve()
        storage = Path(storage_root).resolve()
        raw, manifest, digest = _historical_manifest(source)
        if digest != expected_sha:
            raise ValueError("Historical warehouse changed before it could be pinned")
        identifier = "historical-" + digest
        relative = Path("datasets") / identifier
        destination = storage / relative
        files = _historical_files(source, manifest)
        if not destination.exists():
            temporary = storage / "datasets" / ("." + identifier + "-" + uuid.uuid4().hex)
            temporary.mkdir(parents=True)
            try:
                for source_path, relative_path, expected in files:
                    target = temporary / relative_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.link(source_path, target)
                    except OSError:
                        shutil.copy2(source_path, target)
                    if file_digest(target) != expected:
                        raise ValueError(f"Pinned historical file is corrupt: {relative_path.as_posix()}")
                (temporary / "manifest.json").write_bytes(raw)
                os.rename(temporary, destination)
            except BaseException:
                if temporary.exists():
                    shutil.rmtree(temporary)
                raise
        pinned_raw, pinned_manifest, pinned_digest = _historical_manifest(destination)
        if pinned_digest != digest or pinned_raw != raw or pinned_manifest != manifest:
            raise ValueError("Pinned historical dataset conflicts with its content identifier")
        record = {
            "id": identifier,
            "request": json_value(request),
            "manifest": {
                "kind": "historical_market_warehouse"
                if manifest["schema_version"] == "historical-market-warehouse-v1"
                else "historical_index_warehouse",
                "source_manifest_sha256": digest,
                "source_id": manifest.get("id"),
                "schema_version": manifest["schema_version"],
                "file_count": len(files) + 1,
                "assets": len(manifest["assets"]),
            },
            "relative_path": relative.as_posix(),
            "status": "ready",
            "path": destination,
        }
        self._register_dataset(record)
        return record

    def _register_dataset(self, record):
        values = {key: record[key] for key in ("id", "request", "manifest", "relative_path", "status")}
        with self.engine.begin() as connection:
            statement = insert(dataset).values(values)
            connection.execute(statement.on_conflict_do_nothing(index_elements=["id"]))
            saved = connection.execute(select(dataset).where(dataset.c.id == record["id"])).mappings().one()
        for key in ("manifest", "relative_path", "status"):
            if saved[key] != values[key]:
                raise ValueError(f"Dataset {record['id']} conflicts with its existing registration")


def snapshot_dataset_record(snapshot_root, storage_root, *, request):
    """Validate and describe a file snapshot without writing to PostgreSQL."""
    root = Path(snapshot_root).resolve()
    storage = Path(storage_root).resolve()
    if not root.is_relative_to(storage):
        raise ValueError("Research snapshot must be inside STORAGE_ROOT")
    manifest_path = root / "manifest.json"
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("Research snapshot has no file manifest")
    verified = {}
    for name, expected in sorted(files.items()):
        relative = Path(name)
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Research snapshot contains an invalid file path")
        actual = file_digest(path)
        if actual != expected:
            raise ValueError(f"Research snapshot checksum mismatch: {name}")
        verified[name] = actual
    digest = sha256_bytes(raw)
    identifier = "snapshot-" + digest
    return {
        "id": identifier,
        "request": json_value(request),
        "manifest": {
            "kind": "market_snapshot",
            "source_manifest_sha256": digest,
            "source_id": manifest.get("id"),
            "schema_version": manifest.get("schema_version"),
            "files": verified,
        },
        "relative_path": root.relative_to(storage).as_posix(),
        "status": "ready",
        "path": root,
    }


def _historical_manifest(root):
    path = root / "manifest.json"
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("schema_version") not in {
        "historical-index-warehouse-v1",
        "historical-market-warehouse-v1",
    } or not manifest.get("assets"):
        raise ValueError("Historical warehouse is not completely published")
    return raw, manifest, sha256_bytes(raw)


def _historical_files(root, manifest):
    files = []
    for path, relative, expected in warehouse_files(root, manifest):
        if file_digest(path) != expected:
            raise ValueError(f"Historical warehouse checksum mismatch: {relative.as_posix()}")
        files.append((path, relative, expected))
    return files
