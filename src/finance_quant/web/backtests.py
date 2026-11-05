"""Backtest summaries, account series and execution detail routes."""

from __future__ import annotations

from datetime import date as Date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from finance_quant.research.performance import enrich_backtest
from finance_quant.storage_tables import read_rows
from finance_quant.web.artifacts import ArtifactReader, page


def backtest_router(store) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    artifacts = ArtifactReader(store)

    @router.get("/backtests")
    def backtests():
        records, unavailable = store.list_metadata("backtests")
        items = [result["summary"] for result in records]
        items.sort(key=lambda item: item["created_at"], reverse=True)
        return {
            "items": items,
            "total": len(items),
            **({"unavailable": unavailable} if unavailable else {}),
        }

    @router.get("/backtests/{run_id}")
    def backtest(run_id: str):
        result = enrich_backtest(artifacts.get("backtests", run_id))
        return {
            **result["summary"],
            **result["metadata"],
            "performance": result.get("performance"),
        }

    @router.get("/backtests/{run_id}/equity")
    def equity(run_id: str):
        return {"items": artifacts.get("backtests", run_id)["equity"]}

    @router.get("/backtests/{run_id}/industries")
    def backtest_industries(run_id: str, date: Date | None = None):
        result = artifacts.get("backtests", run_id)
        report = result.get("industry_exposure")
        if not report:
            return {
                "items": [],
                "available": False,
                "note": "旧结果未保存历史行业诊断，请复用配置重新运行；原结果保留。",
            }
        day = date.isoformat() if date else result["equity"][-1]["date"]
        selected = next((row for row in report["days"] if row["date"] == day), None)
        if selected is None:
            raise HTTPException(422, "所选日期没有行业暴露记录")
        payload = {
            **{key: value for key, value in report.items() if key != "days"},
            **selected,
            "available": True,
        }
        metadata = result["metadata"]
        code = (
            metadata.get("benchmark", {}).get("code")
            or metadata.get("benchmark_code")
            or metadata.get("provenance", {}).get("benchmark")
            or "000300.SH"
        )
        if code not in {"000300.SH", "H00300.CSI"}:
            payload["benchmark_available"] = False
            payload["benchmark_note"] = "当前基准无历史行业权重，不进行替代比较。"
            payload["items"] = [{**row, "benchmark_weight": None, "active_weight": None} for row in payload["items"]]
        return payload

    @router.get("/backtests/{run_id}/positions")
    def positions(
        run_id: str,
        date: Date | None = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ):
        result = artifacts.get("backtests", run_id)
        rows, total = read_rows(
            store,
            "backtests",
            run_id,
            result,
            "positions",
            filters={"date": date.isoformat()} if date else None,
            offset=offset,
            limit=limit,
        )
        return {"items": rows, "total": total, "offset": offset, "limit": limit}

    @router.get("/backtests/{run_id}/trades")
    def trades(
        run_id: str,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ):
        return page(artifacts.get("backtests", run_id)["trades"], offset, limit)

    @router.get("/backtests/{run_id}/download")
    def download(run_id: str):
        result = enrich_backtest(artifacts.get("backtests", run_id))
        return JSONResponse(
            result,
            headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'},
        )

    @router.get("/backtests/{run_id}/execution-events")
    def execution_events(
        run_id: str,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ):
        events = artifacts.get("backtests", run_id).get("execution_events", [])
        return page(events, offset, limit)

    return router
