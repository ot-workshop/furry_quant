"""HTTP access to forward registration records and synchronous creation."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from finance_quant.task_contracts import TaskConflict
from finance_quant.web.forward_reads import registration_detail, registration_page
from finance_quant.web.forward_writes import submit_forward_registration
from finance_quant.web.write_requests import research_json


def forward_registration_router(engine: Engine | None, *, storage_root: Path | None = None) -> APIRouter:
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

    @router.post("")
    async def create_registration(request: Request):
        document = await research_json(request)
        if engine is None or storage_root is None:
            raise HTTPException(503, "前瞻登记服务暂不可用")
        key = request.headers.get("idempotency-key", "")
        try:
            return await run_in_threadpool(submit_forward_registration, engine, storage_root, document, key)
        except TaskConflict as error:
            raise HTTPException(409, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except SQLAlchemyError:
            raise HTTPException(503, "前瞻登记服务暂不可用") from None

    return router
