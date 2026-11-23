"""Read-only HTTP access to forward registration records."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError

from finance_quant.web.forward_reads import registration_detail, registration_page


def forward_registration_router(engine: Engine | None) -> APIRouter:
    router = APIRouter(prefix="/api/v1/forward-registrations")

    def read_connection() -> Iterator[Connection]:
        if engine is None:
            raise HTTPException(503, "前瞻登记暂不可读")
        try:
            with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
                with connection.begin():
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                    yield connection
        except SQLAlchemyError:
            raise HTTPException(503, "前瞻登记暂不可读") from None

    @router.get("")
    def registrations(
        connection: Connection = Depends(read_connection),
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ):
        return registration_page(connection, offset=offset, limit=limit)

    @router.get("/{registration_id}")
    def registration(registration_id: str, connection: Connection = Depends(read_connection)):
        detail = registration_detail(connection, registration_id)
        if detail is None:
            raise HTTPException(404, "前瞻登记不存在")
        return detail

    return router
