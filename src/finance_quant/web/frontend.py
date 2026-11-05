"""Single-page application fallback routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

FRONTEND_ROUTES = {
    "",
    "research",
    "tasks",
    "factors",
    "backtests",
    "experiments",
    "data",
    "evaluations",
}


def mount_frontend(app: FastAPI, frontend_dist: Path | None) -> None:
    from finance_quant.paths import frontend_dist as default_dist

    dist = frontend_dist or default_dist()
    if not (dist / "index.html").exists():

        @app.get("/")
        def index():
            return {
                "message": "前端尚未构建，请在 frontend 目录执行 npm install 和 npm run build",
                "docs": "/docs",
            }

        return

    resolved_dist = dist.resolve()

    @app.get("/{route:path}", include_in_schema=False)
    def frontend_route(route: str):
        candidate = (resolved_dist / route).resolve()
        if candidate.is_relative_to(resolved_dist) and candidate.is_file():
            return FileResponse(candidate)
        if route.split("/")[0] in FRONTEND_ROUTES:
            return FileResponse(resolved_dist / "index.html")
        raise HTTPException(404, "页面不存在")
