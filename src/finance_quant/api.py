"""Compose the HTTP application from explicit services and route modules."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from finance_quant.application.tasks import TaskService
from finance_quant.research_api import research_router
from finance_quant.storage import ResultStore
from finance_quant.storage_tables import ResultTableError
from finance_quant.web.backtests import backtest_router
from finance_quant.web.comparisons import comparison_router
from finance_quant.web.evaluations import evaluation_router
from finance_quant.web.experiments import experiment_router
from finance_quant.web.factors import factor_router
from finance_quant.web.frontend import mount_frontend
from finance_quant.web.system import system_router


def create_app(
    *,
    result_store: ResultStore,
    task_service: TaskService,
    frontend_dist: Path | None = None,
    database_probe: Callable[[], dict[str, Any]] | None = None,
    market_status: Callable[[], dict[str, Any]] | None = None,
    market_summary: Callable[[], dict[str, Any]] | None = None,
) -> FastAPI:
    """Build the app from injected services; nothing is selected from environment state."""
    store, service = result_store, task_service

    @asynccontextmanager
    async def lifespan(_app):
        service.start()
        try:
            yield
        finally:
            service.close()

    app = FastAPI(title="量化研究工作台", version="0.3.0", lifespan=lifespan)
    app.state.research_tasks = service
    app.include_router(research_router(service, result_store=store))
    system = system_router(
        store,
        service.data_root,
        database_probe=database_probe,
        market_status=market_status,
        market_summary=market_summary,
    )
    app.include_router(system)
    app.state.invalidate_dashboard_summary = getattr(system, "invalidate_summary_cache")
    app.include_router(factor_router(store))
    app.include_router(backtest_router(store))
    app.include_router(comparison_router(store))
    app.include_router(evaluation_router(store, data_root=service.data_root, runs_root=service.runs_root))
    app.include_router(experiment_router(store))

    @app.exception_handler(ResultTableError)
    async def invalid_table(_request, error):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.get("/api/{unknown:path}")
    def unknown_api(unknown: str):
        raise HTTPException(404, "接口不存在")

    mount_frontend(app, frontend_dist)
    return app
