"""Factor evaluation summaries, labels and report downloads."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from finance_quant.research.report import evaluation_markdown
from finance_quant.storage_tables import read_rows, table_path
from finance_quant.web.artifacts import ArtifactReader


def evaluation_router(store) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    artifacts = ArtifactReader(store)

    @router.get("/evaluations")
    def evaluations():
        records, unavailable = store.list_metadata("evaluations")
        items = [result["metadata"] for result in records]
        items.sort(key=lambda row: row["created_at"], reverse=True)
        return {
            "items": items,
            "total": len(items),
            **({"unavailable": unavailable} if unavailable else {}),
        }

    @router.get("/evaluations/{run_id}")
    def evaluation(run_id: str):
        return artifacts.get("evaluations", run_id)

    @router.get("/evaluations/{run_id}/labels/{horizon}")
    def label_rows(
        run_id: str,
        horizon: int,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ):
        result = artifacts.get("evaluations", run_id)
        if str(horizon) not in result.get("labels", {}):
            raise HTTPException(404, "持有期不存在")
        rows, total = read_rows(
            store,
            "evaluations",
            run_id,
            result,
            f"labels/{horizon}/observations",
            offset=offset,
            limit=limit,
        )
        return {"items": rows, "total": total, "offset": offset, "limit": limit}

    @router.get("/evaluations/{run_id}/labels/{horizon}/download")
    def label_download(run_id: str, horizon: int):
        result = artifacts.get("evaluations", run_id)
        path = f"labels/{horizon}/observations"
        if path not in result.get("tables", {}):
            raise HTTPException(404, "该批次没有独立标签文件")
        return FileResponse(
            table_path(store, "evaluations", run_id, result, path),
            filename=f"{run_id}-labels-{horizon}.parquet",
        )

    @router.get("/evaluations/{run_id}/download")
    def download_evaluation(run_id: str):
        return JSONResponse(
            artifacts.get("evaluations", run_id),
            headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'},
        )

    @router.get("/evaluations/{run_id}/report")
    def download_evaluation_report(run_id: str):
        report = evaluation_markdown(artifacts.get("evaluations", run_id))
        return PlainTextResponse(
            report,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{run_id}.md"'},
        )

    return router
