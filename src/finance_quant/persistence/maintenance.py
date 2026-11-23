"""Coordinate offline backups with the worker and API writes."""

from contextlib import contextmanager

from sqlalchemy import text

from finance_quant.persistence.tasks import worker_lock

MAINTENANCE_LOCK = 663420121


@contextmanager
def write_gate(engine, *, exclusive=False):
    function = "pg_try_advisory_lock" if exclusive else "pg_try_advisory_lock_shared"
    unlock = "pg_advisory_unlock" if exclusive else "pg_advisory_unlock_shared"
    with engine.connect() as connection:
        acquired = connection.execute(text(f"SELECT {function}(:key)"), {"key": MAINTENANCE_LOCK}).scalar_one()
        connection.commit()
        if not acquired:
            raise RuntimeError("数据维护正在进行，或仍有写入请求；请稍后重试")
        try:
            yield connection
        finally:
            if not connection.invalidated:
                connection.execute(text(f"SELECT {unlock}(:key)"), {"key": MAINTENANCE_LOCK})
                connection.commit()


@contextmanager
def offline_lock(engine):
    # The worker owns this lock for its lifetime, not just while a task is running.
    with worker_lock(engine), write_gate(engine, exclusive=True):
        yield
