"""PG result records with large detail tables in immutable Parquet files."""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from finance_quant.checksums import sha256_text
from finance_quant.storage import ResultStore as FileResultStore
from finance_quant.storage_tables import StreamingTables, hydrate_tables

from .database import make_engine
from .market import chunks, file_digest
from .schema import dataset, research_run, run_artifact, run_equity, run_metrics, run_trade


class PgResultStore(FileResultStore):
    """PostgreSQL-backed result repository with an explicit dataset binding."""

    def __init__(
        self,
        root=None,
        *,
        engine=None,
        storage_root: Path | None = None,
        dataset_id: str | None = None,
    ):
        super().__init__(root)
        self.engine = engine or make_engine()
        self.storage_root = (storage_root or self.root.parent).resolve()
        if dataset_id and (len(dataset_id) > 100 or not dataset_id.replace("-", "").isalnum()):
            raise ValueError("Invalid active dataset identifier")
        self.dataset_id = dataset_id

    @staticmethod
    def _validate_dataset(connection, dataset_id):
        if dataset_id is None:
            return
        status = connection.execute(select(dataset.c.status).where(dataset.c.id == dataset_id)).scalar_one_or_none()
        if status != "ready":
            raise ValueError("Research output requires a registered ready dataset")

    def attach_dataset(self, category, run_id, dataset_id):
        """Link reused immutable results without changing their document digest."""
        self.path(category, run_id)
        with self.engine.begin() as connection:
            self._validate_dataset(connection, dataset_id)
            current = connection.execute(
                select(research_run.c.dataset_id).where(
                    research_run.c.id == run_id, research_run.c.category == category
                )
            ).scalar_one_or_none()
            if current is None:
                changed = connection.execute(
                    research_run.update()
                    .where(
                        research_run.c.id == run_id,
                        research_run.c.category == category,
                        research_run.c.dataset_id.is_(None),
                    )
                    .values(dataset_id=dataset_id)
                ).rowcount
                if changed != 1:
                    raise FileNotFoundError("Result does not exist")
            elif current != dataset_id:
                raise ValueError("Result is already linked to a different research dataset")

    def _record(self, category, run_id, connection):
        self.path(category, run_id)  # Validate identifiers before file access.
        row = (
            connection.execute(
                select(research_run).where(research_run.c.id == run_id, research_run.c.category == category)
            )
            .mappings()
            .first()
        )
        if row is None:
            raise FileNotFoundError("结果不存在")
        return row

    def digest(self, category, run_id):
        with self.engine.connect() as connection:
            return self._record(category, run_id, connection)["document_sha256"]

    def exists(self, category, run_id):
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    select(research_run.c.id).where(research_run.c.id == run_id, research_run.c.category == category)
                ).first()
                is not None
            )

    def get(self, category, run_id, *, hydrate=True):
        with self.engine.connect() as connection:
            row = self._record(category, run_id, connection)
            result = copy.deepcopy(row["payload"])
            result["metadata"] = copy.deepcopy(row["metadata"])
            if row["dataset_id"]:
                result["metadata"]["dataset_id"] = row["dataset_id"]
            if category == "backtests":
                result["summary"] = row["summary"]
                result["equity"] = [
                    record["details"]
                    for record in connection.execute(
                        select(run_equity.c.details)
                        .where(run_equity.c.run_id == run_id)
                        .order_by(run_equity.c.trade_date)
                    ).mappings()
                ]
                result["trades"] = [
                    record["details"]
                    for record in connection.execute(
                        select(run_trade.c.details).where(run_trade.c.run_id == run_id).order_by(run_trade.c.sequence)
                    ).mappings()
                ]
        if hydrate and result.get("tables"):
            hydrate_tables(self, category, run_id, result)
        return result

    def project(self, category, run_id):
        """Narrow read: the stored summary/metadata/payload only.

        One row from ``research_run``; equity, trades and Parquet tables are not
        touched, so comparing two large runs stays a bounded read.
        """

        with self.engine.connect() as connection:
            row = self._record(category, run_id, connection)
            result = copy.deepcopy(row["payload"])
            result["metadata"] = copy.deepcopy(row["metadata"])
            if row["dataset_id"]:
                result["metadata"]["dataset_id"] = row["dataset_id"]
            if category == "backtests":
                result["summary"] = copy.deepcopy(row["summary"])
        result.pop("tables", None)
        return result

    def list(self, category):
        self.path(category, "listing-validation")
        with self.engine.connect() as connection:
            ids = (
                connection.execute(
                    select(research_run.c.id)
                    .where(research_run.c.category == category)
                    .order_by(research_run.c.created_at, research_run.c.id)
                )
                .scalars()
                .all()
            )
        return [self.get(category, identifier, hydrate=False) for identifier in ids]

    def list_metadata(self, category):
        self.path(category, "listing-validation")
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(
                        research_run.c.id, research_run.c.summary, research_run.c.metadata, research_run.c.dataset_id
                    )
                    .where(research_run.c.category == category)
                    .order_by(research_run.c.created_at.desc(), research_run.c.id)
                )
                .mappings()
                .all()
            )
        records = []
        for row in rows:
            record = {"metadata": {**row["metadata"], "id": row["id"]}}
            if row["dataset_id"]:
                record["metadata"]["dataset_id"] = row["dataset_id"]
            if category == "backtests":
                record["summary"] = {**row["summary"], "id": row["id"]}
            records.append(record)
        return records, []

    def publish(self, category, run_id, document):
        destination = self.path(category, run_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Validate before publishing either SQL or files; NaN must not enter JSONB.
        encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, allow_nan=False)
        digest = sha256_text(encoded)
        dataset_id = self.dataset_id
        with self.engine.connect() as connection:
            existing = connection.execute(
                select(research_run.c.document_sha256).where(
                    research_run.c.id == run_id, research_run.c.category == category
                )
            ).scalar_one_or_none()
        if existing:
            if digest != existing:
                raise FileExistsError(f"Artifact {run_id} already exists with different content")
            if dataset_id:
                self.attach_dataset(category, run_id, dataset_id)
            return destination
        compact = copy.deepcopy(document)

        external = ["positions"] if category == "backtests" else ["history"] if category == "factors" else []
        with StreamingTables(self, category, run_id) as writer:
            for key in external:
                rows = compact.get(key, [])
                if rows:
                    writer.append(
                        key, sorted(rows, key=lambda item: (item.get("date", ""), item.get("instrument", "")))
                    )
                    compact[key] = []
            compact.setdefault("tables", {}).update(writer.finish())
        artifacts = []
        for name, reference in compact.get("tables", {}).items():
            path = (destination.parent / reference["file"]).resolve()
            if not path.is_relative_to(destination.parent) or file_digest(path) != reference["sha256"]:
                raise ValueError("Result table is missing or corrupt")
            artifacts.append(
                dict(
                    run_id=run_id,
                    category=category,
                    name=name,
                    relative_path=path.relative_to(self.storage_root).as_posix(),
                    row_count=reference["rows"],
                    sha256=reference["sha256"],
                    format="parquet",
                )
            )
        summary = compact.pop("summary", {})
        meta = compact.pop("metadata")
        equities = compact.pop("equity", []) if category == "backtests" else []
        trades = compact.pop("trades", []) if category == "backtests" else []
        # API diagnostic reports remain in the compact payload; numeric execution
        # facts and complete holdings each have one authoritative storage path.
        with self.engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(663420122)"))
            self._validate_dataset(connection, dataset_id)
            existing = connection.execute(
                select(research_run.c.document_sha256).where(
                    research_run.c.id == run_id, research_run.c.category == category
                )
            ).scalar_one_or_none()
            if existing:
                if existing != digest:
                    raise FileExistsError(f"Artifact {run_id} already exists with different content")
                current = connection.execute(
                    select(research_run.c.dataset_id).where(
                        research_run.c.id == run_id, research_run.c.category == category
                    )
                ).scalar_one()
                if dataset_id and current not in (None, dataset_id):
                    raise ValueError("Result is already linked to a different research dataset")
                if dataset_id and current is None:
                    connection.execute(
                        research_run.update()
                        .where(research_run.c.id == run_id, research_run.c.category == category)
                        .values(dataset_id=dataset_id)
                    )
                return destination
            connection.execute(
                research_run.insert().values(
                    id=run_id,
                    category=category,
                    dataset_id=dataset_id,
                    name=summary.get("name", meta.get("name", run_id)),
                    status=summary.get("status", "succeeded"),
                    summary=summary,
                    configuration=meta.get("config", {}),
                    metadata=meta,
                    payload=compact,
                    document_sha256=digest,
                )
            )
            metrics = [
                {"run_id": run_id, "name": key, "value": value}
                for key, value in (summary.get("metrics") or {}).items()
                if value is None or isinstance(value, (int, float)) and not isinstance(value, bool)
            ]
            for batch in chunks(metrics):
                connection.execute(run_metrics.insert(), batch)
            account = {row["date"]: row for row in compact.get("qlib_report", [])}
            points = []
            for row in equities:
                report = account.get(row["date"], {})
                points.append(
                    dict(
                        run_id=run_id,
                        trade_date=date.fromisoformat(row["date"]),
                        equity=row["equity"],
                        benchmark=row.get("benchmark"),
                        cash=report.get("cash"),
                        total_value=report.get("account"),
                        daily_return=report.get("return"),
                        turnover=report.get("turnover"),
                        cost=row.get("cost"),
                        details=row,
                    )
                )
            for batch in chunks(points):
                connection.execute(run_equity.insert(), batch)
            records = []
            for sequence, row in enumerate(trades):
                costs = row.get("cost_breakdown", {})
                records.append(
                    dict(
                        run_id=run_id,
                        sequence=sequence,
                        trade_date=date.fromisoformat(row["date"]),
                        signal_date=date.fromisoformat(row["signal_date"]) if row.get("signal_date") else None,
                        ts_code=row["instrument"],
                        side=row["side"],
                        quantity=row["amount"],
                        price=row["price"],
                        commission=costs.get("commission"),
                        stamp_tax=costs.get("stamp_tax"),
                        impact_cost=costs.get("impact"),
                        total_cost=row.get("cost"),
                        details=row,
                    )
                )
            for batch in chunks(records):
                connection.execute(run_trade.insert(), batch)
            if artifacts:
                connection.execute(run_artifact.insert(), artifacts)
        return destination

    def publish_report(self, run_id, markdown, *, category="evaluations"):
        path = super().publish_report(run_id, markdown, category=category)

        with self.engine.begin() as connection:
            statement = insert(run_artifact).values(
                run_id=run_id,
                category=category,
                name="report",
                relative_path=path.relative_to(self.storage_root).as_posix(),
                sha256=file_digest(path),
                format="markdown",
            )
            connection.execute(statement.on_conflict_do_nothing())
        return path
