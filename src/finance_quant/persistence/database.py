"""Connection configuration; credentials are never interpolated into SQL/logs."""

from __future__ import annotations

import os
from functools import lru_cache

from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Engine

SCHEMA_REVISION = "0001_postgres"


def database_url(environment=None):
    env = os.environ if environment is None else environment
    return URL.create(
        "postgresql+psycopg",
        username=env.get("POSTGRES_USER", "finance_quant"),
        password=env.get("POSTGRES_PASSWORD", ""),
        host=env.get("POSTGRES_HOST", "db"),
        port=int(env.get("POSTGRES_PORT", "5432")),
        database=env.get("POSTGRES_DB", "finance_quant"),
    )


@lru_cache(maxsize=1)
def make_engine() -> Engine:
    return create_engine(
        database_url(),
        pool_pre_ping=True,
        hide_parameters=True,
        pool_size=3,
        max_overflow=2,
        connect_args={"connect_timeout": 10, "options": "-c timezone=UTC"},
    )


def check_schema(engine: Engine | None = None):
    """Fail explicitly until the one-shot migration has completed."""
    with (engine or make_engine()).connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        if revision != SCHEMA_REVISION:
            raise RuntimeError("Database schema differs from application; run the migration service")
    return {"database": "postgresql", "revision": revision}
