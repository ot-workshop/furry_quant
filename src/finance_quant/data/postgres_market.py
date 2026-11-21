"""Current market facts in PostgreSQL; frozen files are research inputs, not a second database."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import pandas as pd
from sqlalchemy import and_, func, select, text

from finance_quant.checksums import file_sha256, sha256_text
from finance_quant.data.symbols import to_qlib
from finance_quant.data.temporal_audit import restore_valuation_dates
from finance_quant.persistence.market import MarketRepository
from finance_quant.persistence.schema import (
    MARKET_TABLES,
    adjustment,
    daily_bar,
    data_release,
    data_revision,
    dataset,
    source_check,
    trading_status,
    valuation,
)

# Request-scoped REPEATABLE READ connection; a ContextVar keeps concurrent API requests isolated.
_snapshot = ContextVar("market_read_snapshot", default=None)


class PostgresMarketStore:
    """The production :class:`finance_quant.data.shared_market.MarketStore`."""

    def inventory(self, root):
        return inventory()

    def covered_dates(self, document, code, sessions, provider_id):
        return covered_dates(code, sessions, provider_id)

    def publish_segment(self, root, record, path, *, batch_id):

        frame = restore_valuation_dates(pd.read_parquet(path))
        return publish_frame(frame, record, batch_id=batch_id)

    def read_asset(self, root, code, expected, provider_id):
        return freeze_asset(root, code, expected, provider_id)

    def publish_references(self, components, *, batch_id, checks):
        return repository().publish(components, source="tushare_official", batch_id=batch_id, checks=checks)

    def consistent_read(self):
        return consistent_read()


def repository():

    return MarketRepository()


@contextmanager
def consistent_read():
    """One database snapshot for all assets in a published research input."""
    if _snapshot.get() is not None:
        yield
        return
    with repository().engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        with connection.begin():
            token = _snapshot.set(connection)
            try:
                yield
            finally:
                _snapshot.reset(token)


@contextmanager
def connection():
    existing = _snapshot.get()
    if existing is not None:
        yield existing
    else:
        with repository().engine.connect() as active:
            yield active


def inventory():

    with connection() as active:
        codes = active.execute(select(daily_bar.c.ts_code).distinct()).scalars().all()
    return {"schema_version": "postgres-market-v1", "segments": {code: [] for code in codes}}


def status():
    """Small read-only operational summary for the web data overview."""

    with connection() as active:
        rows = {
            name: active.execute(select(func.count()).select_from(table)).scalar_one()
            for name, table in MARKET_TABLES.items()
        }
        releases = active.execute(select(func.count()).select_from(data_release)).scalar_one()
        revisions = active.execute(select(func.count()).select_from(data_revision)).scalar_one()
        datasets = active.execute(select(func.count()).select_from(dataset)).scalar_one()
        latest = (
            active.execute(select(data_release).order_by(data_release.c.created_at.desc()).limit(1)).mappings().first()
        )
        checks = active.execute(select(source_check.c.status, func.count()).group_by(source_check.c.status)).all()

        coverage = active.execute(
            select(
                func.count(func.distinct(daily_bar.c.ts_code)),
                func.min(daily_bar.c.trade_date),
                func.max(daily_bar.c.trade_date),
            )
        ).one()
    return {
        "schema_version": "postgres-market-v1",
        "row_counts": rows,
        "releases": releases,
        "revisions": revisions,
        "datasets": datasets,
        "securities": coverage[0],
        "coverage_start": coverage[1].isoformat() if coverage[1] else None,
        "coverage_end": coverage[2].isoformat() if coverage[2] else None,
        "checks": {key: value for key, value in checks},
        "latest_release": (
            {
                "id": latest["id"],
                "source": latest["source"],
                "changed_rows": latest["changed_rows"],
                "created_at": latest["created_at"].isoformat(),
            }
            if latest
            else None
        ),
    }


_DISTINCT_CODES_SKIP_SCAN = text(
    """
    WITH RECURSIVE distinct_codes AS (
        (SELECT ts_code FROM daily_bar ORDER BY ts_code LIMIT 1)
        UNION ALL
        SELECT (
            SELECT d.ts_code
            FROM daily_bar AS d
            WHERE d.ts_code > dc.ts_code
            ORDER BY d.ts_code
            LIMIT 1
        )
        FROM distinct_codes AS dc
        WHERE dc.ts_code IS NOT NULL
    )
    SELECT COUNT(*) FROM distinct_codes WHERE ts_code IS NOT NULL
    """
)


def market_summary():
    """Lightweight market coverage summary without 8M-row table counts."""

    min_date_q = select(daily_bar.c.trade_date).order_by(daily_bar.c.trade_date.asc()).limit(1).scalar_subquery()
    max_date_q = select(daily_bar.c.trade_date).order_by(daily_bar.c.trade_date.desc()).limit(1).scalar_subquery()
    with connection() as active:
        min_date, max_date = active.execute(select(min_date_q, max_date_q)).one()
        if min_date is None and max_date is None:
            securities = 0
        elif active.dialect.name == "postgresql":
            securities = int(active.execute(_DISTINCT_CODES_SKIP_SCAN).scalar_one())
        else:
            securities = int(active.execute(select(func.count(func.distinct(daily_bar.c.ts_code)))).scalar_one())
    return {
        "securities": securities,
        "coverage_start": min_date.isoformat() if min_date else None,
        "coverage_end": max_date.isoformat() if max_date else None,
    }


def covered_dates(code, sessions, provider_id):
    """Coverage is the actual intersection of four components, never min/max dates."""

    if sessions.empty:
        return pd.DatetimeIndex([])
    query = select(daily_bar.c.trade_date)
    for table in (adjustment, valuation, trading_status):
        query = query.join(
            table, and_(table.c.ts_code == daily_bar.c.ts_code, table.c.trade_date == daily_bar.c.trade_date)
        )
    query = query.where(
        daily_bar.c.ts_code == code,
        daily_bar.c.provider_id == provider_id,
        daily_bar.c.trade_date.between(sessions.min().date(), sessions.max().date()),
    )
    with connection() as active:
        dates = pd.DatetimeIndex(active.execute(query).scalars().all())
    return sessions.intersection(dates).sort_values()


def read_frame(code, expected, provider_id):

    tables = (daily_bar, adjustment, valuation, trading_status)
    ignored = {"source", "quality", "fetched_at", "published_at", "release_id", "provider_id"}
    columns = [daily_bar.c.ts_code, daily_bar.c.trade_date]
    for table in tables:
        columns.extend(c for c in table.c if c.name not in ignored | {"ts_code", "trade_date"})
    query = select(*columns).select_from(daily_bar)
    for table in tables[1:]:
        query = query.join(
            table, and_(table.c.ts_code == daily_bar.c.ts_code, table.c.trade_date == daily_bar.c.trade_date)
        )
    query = query.where(
        daily_bar.c.ts_code == code,
        daily_bar.c.provider_id == provider_id,
        daily_bar.c.trade_date.between(expected.min().date(), expected.max().date()),
    )
    with connection() as active:
        rows = active.execute(query.order_by(daily_bar.c.trade_date)).mappings().all()
    if not rows:
        raise ValueError(f"{code}没有可用共享行情")
    frame = pd.DataFrame(rows).rename(columns={"trade_date": "date", "suspended": "is_suspended"})
    frame["date"] = pd.to_datetime(frame.date)
    frame = frame.loc[frame.date.isin(expected)].copy()
    frame["instrument"] = frame.pop("ts_code").map(to_qlib)
    if not pd.DatetimeIndex(frame.date).equals(expected):
        raise ValueError(f"{code}共享行情仍有缺口")
    for column in frame:
        if column.endswith("_asof"):
            frame[column] = pd.to_datetime(frame[column])
    optional_times = [
        c for c in frame if (c.endswith("_asof") or c.endswith("_available_at")) and frame[c].isna().all()
    ]
    frame = frame.drop(columns=optional_times)
    return frame.reset_index(drop=True)


def _value(value):
    if value is None or pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    return value.item() if hasattr(value, "item") else value


class FullHistoryRequired(ValueError):
    def __init__(self, code, start, end):
        self.code, self.start, self.end = code, pd.Timestamp(start), pd.Timestamp(end)
        super().__init__(f"{code}复权修订需要重新核验完整已存历史 {start} 至 {end}")


def publish_frame(frame, record, *, batch_id):

    code = record["code"]
    frame = frame.drop(
        columns=[
            c
            for c in frame
            if c
            in {
                "index_member",
                "selection_member",
                "eligible_history",
                "has_required_pre_history",
                "history_sessions_before",
            }
        ]
    )
    # A changed adjustment scale must not be patched into only the newest twenty rows.
    with connection() as active:
        bounds = active.execute(
            select(func.min(adjustment.c.trade_date), func.max(adjustment.c.trade_date)).where(
                adjustment.c.ts_code == code
            )
        ).one()
        previous = active.execute(
            select(adjustment.c.trade_date, adjustment.c.adj_factor).where(
                adjustment.c.ts_code == code,
                adjustment.c.trade_date.between(frame.date.min().date(), frame.date.max().date()),
            )
        ).all()
        old_sources = (
            active.execute(select(daily_bar.c.price_source).where(daily_bar.c.ts_code == code).distinct())
            .scalars()
            .all()
        )
    changed_adjustment = any(
        abs(float(frame.set_index("date").loc[pd.Timestamp(day), "adj_factor"]) - old) > abs(old) * 1e-10 + 1e-10
        for day, old in previous
        if pd.Timestamp(day) in set(frame.date)
    )
    changed_source = bool(old_sources) and set(frame.price_source) != set(old_sources)
    if (changed_adjustment or changed_source) and bounds[0] is not None:
        if frame.date.min().date() > bounds[0] or frame.date.max().date() < bounds[1]:
            raise FullHistoryRequired(code, bounds[0], bounds[1])
    components = {}
    for name in ("daily_bar", "adjustment", "valuation", "trading_status"):
        table = MARKET_TABLES[name]
        rows = []
        for raw in frame.to_dict("records"):
            raw = {
                **raw,
                "ts_code": code,
                "trade_date": pd.Timestamp(raw["date"]).date(),
                "suspended": raw.get("is_suspended"),
                "provider_id": record["provider_id"],
                "quality": raw.get("validation_level", record["validation_level"]),
            }
            rows.append({key: _value(value) for key, value in raw.items() if key in table.c})
        components[name] = rows
    checks = [
        {
            "component": name,
            "partition_key": code,
            "status": "complete",
            "row_count": len(frame),
            "details": {"start": frame.date.min().date().isoformat(), "end": frame.date.max().date().isoformat()},
        }
        for name in components
    ]
    return repository().publish(components, source="tushare_official", batch_id=batch_id, checks=checks)


def freeze_asset(root, code, expected, provider_id):
    """Content-addressed projections contain no external or Windows paths."""
    frame = read_frame(code, expected, provider_id)
    encoded = frame.to_json(orient="table", date_format="iso", double_precision=15)
    key = sha256_text(encoded)
    path = Path(root) / "frozen-market" / f"{key}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temporary = path.with_suffix(".pending.parquet")
        frame.to_parquet(temporary, index=False)
        temporary.replace(path)
    levels = set(frame.validation_level)
    level = (
        "single_source_passed"
        if "single_source_passed" in levels
        else "secondary_fallback_passed"
        if "secondary_fallback_passed" in levels
        else "dual_source_verified"
    )
    return frame, [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": file_sha256(path),
            "validation_level": level,
            "start": expected.min().strftime("%Y%m%d"),
            "end": expected.max().strftime("%Y%m%d"),
        }
    ]
