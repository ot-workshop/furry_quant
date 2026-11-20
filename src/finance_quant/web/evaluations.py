"""Factor evaluation summaries, labels and report downloads."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from finance_quant.application.selection_context import selection_result
from finance_quant.application.selection_experiments import selection_draft
from finance_quant.data.temporal_audit import evaluation_financial_audit
from finance_quant.research.evaluation_analysis import evaluation_analysis
from finance_quant.research.factor_selection import SelectionParameters
from finance_quant.research.report import evaluation_markdown
from finance_quant.storage_tables import read_rows, table_path
from finance_quant.web.artifacts import ArtifactReader


def selection_parameters(
    horizon: Annotated[int, Query(ge=1, le=252)],
    cutoff: str,
    factor_count: Annotated[int, Query(ge=1, le=20)] = 3,
    min_ic: Annotated[float, Query(ge=0, le=1)] = 0,
    min_ir: Annotated[float, Query(ge=0, le=1e6)] = 0,
    max_correlation: Annotated[float, Query(ge=0, le=1)] = 0.6,
    min_dates: Annotated[int, Query(ge=2, le=25200)] = 20,
    start: str | None = None,
):
    try:
        return SelectionParameters(horizon, cutoff, factor_count, min_ic, min_ir, max_correlation, min_dates, start)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


def evaluation_router(store, *, data_root=None, runs_root=None) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    artifacts = ArtifactReader(store)

    def read_evaluation(run_id):
        return evaluation_financial_audit(artifacts.get("evaluations", run_id))

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
        return read_evaluation(run_id)

    @router.get("/evaluations/{run_id}/analysis")
    def analysis(
        run_id: str,
        horizon: Annotated[int, Query(ge=1, le=252)],
        factor: str = "composite",
        window: Annotated[int, Query(ge=2, le=2520)] = 60,
        min_periods: Annotated[int | None, Query(ge=2, le=2520)] = None,
        start: str | None = None,
        end: str | None = None,
        first: str | None = None,
        second: str | None = None,
    ):
        result = read_evaluation(run_id)
        try:
            return evaluation_analysis(
                result,
                horizon=horizon,
                factor=factor,
                window=window,
                min_periods=min_periods,
                start=start,
                end=end,
                first=first,
                second=second,
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @router.get("/evaluations/{run_id}/selection")
    def select_combination(run_id: str, parameters: Annotated[SelectionParameters, Depends(selection_parameters)]):
        read_evaluation(run_id)
        try:
            return selection_result(store, run_id, parameters)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @router.get("/evaluations/{run_id}/selection/draft")
    def combination_draft(
        run_id: str, parameters: Annotated[SelectionParameters, Depends(selection_parameters)], selection_id: str
    ):
        read_evaluation(run_id)
        try:
            reference = selection_result(store, run_id, parameters)["reference"]
            if reference["selection_id"] != selection_id:
                raise ValueError("来源评估或筛选算法已变化，请重新筛选组合")
            return selection_draft(store, reference, data_root, runs_root)
        except (FileNotFoundError, ValueError) as error:
            raise HTTPException(422, str(error)) from None

    @router.get("/evaluations/{run_id}/labels/{horizon}")
    def label_rows(
        run_id: str,
        horizon: int,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ):
        result = read_evaluation(run_id)
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
        result = read_evaluation(run_id)
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
            read_evaluation(run_id),
            headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'},
        )

    @router.get("/evaluations/{run_id}/report")
    def download_evaluation_report(run_id: str):
        report = evaluation_markdown(read_evaluation(run_id))
        return PlainTextResponse(
            report,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{run_id}.md"'},
        )

    return router
