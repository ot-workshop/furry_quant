"""HTTP access to forward registration records and synchronous creation."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from finance_quant.application.forward_verification import VerificationInputError
from finance_quant.persistence.forward import AttemptActive
from finance_quant.task_contracts import TaskConflict
from finance_quant.web.forward_actions import (
    registration_score_options,
    submit_forward_score,
    submit_forward_verification,
)
from finance_quant.web.forward_reads import registration_detail, registration_page
from finance_quant.web.forward_writes import registration_source, registration_sources, submit_forward_registration
from finance_quant.web.write_requests import ResearchMaintenanceRejected, research_json


def forward_registration_router(
    engine: Engine | None, *, storage_root: Path | None = None, data_root: Path | None = None
) -> APIRouter:
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

    @router.get("/sources")
    def sources(
        connection: Connection = Depends(read_connection),
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=25)] = 20,
    ):
        if storage_root is None:
            raise HTTPException(503, "受管来源暂不可读")
        return registration_sources(connection, storage_root, offset=offset, limit=limit)

    @router.get("/sources/{dataset_id}")
    def source(dataset_id: str, connection: Connection = Depends(read_connection)):
        if storage_root is None:
            raise HTTPException(503, "受管来源暂不可读")
        option = registration_source(connection, storage_root, dataset_id)
        if option is None:
            raise HTTPException(404, "所选来源已不可用")
        return option

    @router.get("/{registration_id}/score-options")
    def score_options(registration_id: str, connection: Connection = Depends(read_connection)):
        if data_root is None:
            raise HTTPException(503, "评分快照暂不可读")
        try:
            return registration_score_options(connection, data_root, registration_id)
        except (OSError, ValueError):
            raise HTTPException(503, "评分快照暂不可读") from None

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
        except ResearchMaintenanceRejected as error:
            return JSONResponse({"code": "maintenance", "detail": error.detail}, status_code=error.status_code)
        except TaskConflict as error:
            raise HTTPException(409, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except SQLAlchemyError:
            raise HTTPException(503, "前瞻登记服务暂不可用") from None

    async def enqueue(request: Request, registration_id: str, submit):
        document = await research_json(request)
        if engine is None or data_root is None:
            raise HTTPException(503, "前瞻提交服务暂不可用")
        try:
            binding = await run_in_threadpool(
                submit, engine, data_root, registration_id, document, request.headers.get("idempotency-key", "")
            )
        except (AttemptActive, TaskConflict) as error:
            raise HTTPException(409, str(error)) from None
        except VerificationInputError as error:
            reason = str(error)
            if reason in {"before-verification-maturity", "registration-unsealed", "invalid-sealed-score"}:
                raise HTTPException(409, reason) from None
            raise HTTPException(422, reason) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except (OSError, SQLAlchemyError):
            raise HTTPException(503, "前瞻提交服务暂不可用") from None
        if binding is None:
            return JSONResponse({"status": "awaiting_quote"})
        return {"status": "accepted", **binding}

    @router.post("/{registration_id}/score", status_code=202)
    async def score(registration_id: str, request: Request):
        return await enqueue(request, registration_id, submit_forward_score)

    @router.post("/{registration_id}/verify", status_code=202)
    async def verify(registration_id: str, request: Request):
        return await enqueue(request, registration_id, submit_forward_verification)

    return router
