"""Experiment catalogue and immutable export routes."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse

from finance_quant.research.experiments import experiment_markdown
from finance_quant.web.artifacts import ArtifactReader


def experiment_router(store) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    artifacts = ArtifactReader(store)

    @router.get("/experiments")
    def experiments():
        items = [result["metadata"] for result in store.list("experiments")]
        items.sort(key=lambda row: row["created_at"], reverse=True)
        return {"items": items, "total": len(items)}

    @router.get("/experiments/{run_id}")
    def experiment(run_id: str):
        return artifacts.get("experiments", run_id)

    @router.get("/experiments/{run_id}/download")
    def download_experiment(run_id: str):
        return JSONResponse(
            artifacts.get("experiments", run_id),
            headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'},
        )

    @router.get("/experiments/{run_id}/report")
    def download_experiment_report(run_id: str):
        report = experiment_markdown(artifacts.get("experiments", run_id))
        return PlainTextResponse(
            report,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{run_id}.md"'},
        )

    return router
