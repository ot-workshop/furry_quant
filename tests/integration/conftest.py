"""Whole-database tests only run against an explicitly named test database server."""

import os
import uuid

import pytest
from sqlalchemy import create_engine, text

from finance_quant.persistence.database import database_url


@pytest.fixture
def empty_databases():
    if os.environ.get("FINANCE_QUANT_TEST_POSTGRES") != "1":
        pytest.skip("requires an isolated PostgreSQL test server")
    url = database_url()
    if not url.database or not (url.database.startswith("fq_") or url.database.endswith("_test")):
        pytest.fail("Refusing database tests: explicit test database required")
    admin = create_engine(url, isolation_level="AUTOCOMMIT", hide_parameters=True)
    with admin.connect() as connection:
        assert connection.execute(text("SELECT current_database()")).scalar_one() == url.database
    names = ["fq_backup_" + uuid.uuid4().hex for _ in range(2)]
    engines = []
    created = []
    try:
        with admin.connect() as connection:
            for name in names:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
                created.append(name)
                engines.append(create_engine(url.set(database=name), hide_parameters=True))
        yield engines
    finally:
        for engine in engines:
            engine.dispose()
        with admin.connect() as connection:
            for name in created:
                assert name.startswith("fq_backup_") and len(name) == 42
                connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()
