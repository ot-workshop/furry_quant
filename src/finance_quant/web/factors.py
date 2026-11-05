"""Factor catalogue, score and industry diagnostic routes."""

from __future__ import annotations

from datetime import date as Date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from finance_quant.research.neutralization import factor_industry_diagnostics
from finance_quant.storage_tables import read_rows, table_path
from finance_quant.web.artifacts import ArtifactReader, factor_metadata, page


def factor_router(store) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    artifacts = ArtifactReader(store)

    @router.get("/instruments")
    def instruments(q: str = "", run_id: str | None = None):
        found = {}
        results = [artifacts.get("factors", run_id)] if run_id else store.list_metadata("factors")[0]
        for result in results:
            rows = result["metadata"].get("universe_members", result.get("history", []))
            for row in rows:
                if q.casefold() in (row["instrument"] + row["name"]).casefold():
                    found[row["instrument"]] = {
                        "instrument": row["instrument"],
                        "name": row["name"],
                        "exchange": row["instrument"][:2],
                    }
        items = sorted(found.values(), key=lambda row: row["instrument"])
        return {"items": items, "total": len(items)}

    @router.get("/factor-runs")
    def factor_runs():
        records, unavailable = store.list_metadata("factors")
        items = [factor_metadata(result) for result in records]
        items.sort(
            key=lambda item: (
                not item["is_synthetic"],
                item["as_of"],
                item.get("created_at", ""),
            ),
            reverse=True,
        )
        return {
            "items": items,
            "total": len(items),
            **({"unavailable": unavailable} if unavailable else {}),
        }

    @router.get("/factor-runs/{run_id}/scores")
    def scores(
        run_id: str,
        instrument: str | None = None,
        sort_by: str = "composite",
        descending: bool = True,
        date: Date | None = None,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ):
        result = artifacts.get("factors", run_id)
        if sort_by not in {"composite", "rank", "instrument", *result["metadata"]["weights"]}:
            raise HTTPException(422, "不支持的排序字段")
        selected_date = date.isoformat() if date else result["metadata"]["as_of"]
        filters = {"date": selected_date, **({"instrument": instrument} if instrument else {})}
        rows, _ = read_rows(store, "factors", run_id, result, "history", filters=filters)
        rows.sort(
            key=lambda row: row["scores"].get(sort_by, row.get(sort_by)),
            reverse=descending,
        )
        excluded, _ = read_rows(store, "factors", run_id, result, "diagnostics", filters=filters)
        metadata = {
            **factor_metadata(result),
            "as_of": selected_date,
            "coverage": result["metadata"].get("availability_by_date", {}).get(selected_date),
            "batch_as_of": result["metadata"]["as_of"],
        }
        return {**page(rows, offset, limit), "diagnostics": excluded, "metadata": metadata}

    @router.get("/factor-runs/{run_id}/industries")
    def factor_industries(run_id: str, date: Date | None = None):
        result = artifacts.get("factors", run_id)
        day = date.isoformat() if date else result["metadata"]["as_of"]
        rows, _ = read_rows(store, "factors", run_id, result, "history", filters={"date": day})
        report = factor_industry_diagnostics(rows, result["metadata"])
        return {
            **report,
            "date": day,
            "coverage": result["metadata"].get("industry_coverage_by_date", {}).get(day),
        }

    @router.get("/instruments/{instrument}/factor-history")
    def factor_history(instrument: str, scheme: str | None = None, run_id: str | None = None):
        results = [artifacts.get("factors", run_id)] if run_id else store.list_metadata("factors")[0]
        if run_id and scheme is not None and results[0]["metadata"]["scheme"] != scheme:
            raise HTTPException(422, "评分方案与指定批次不一致")
        results = [result for result in results if scheme is None or result["metadata"]["scheme"] == scheme]
        if len(results) > 1:
            raise HTTPException(409, "存在多个评分版本，请指定 run_id")
        if not run_id and results:
            results = [artifacts.get("factors", results[0]["metadata"]["id"])]
        found = {}
        for result in results:
            result_id = result["metadata"]["id"]
            rows, _ = read_rows(
                store,
                "factors",
                result_id,
                result,
                "history",
                filters={"instrument": instrument},
            )
            for row in rows:
                if row["instrument"] == instrument:
                    found[row["date"]] = {
                        "date": row["date"],
                        **row["scores"],
                        "composite": row["composite"],
                        "run_id": result_id,
                    }
        return {
            "items": [found[key] for key in sorted(found)],
            "scheme": results[0]["metadata"]["scheme"] if results else scheme,
            "is_synthetic": bool(results) and all(result["metadata"]["is_synthetic"] for result in results),
        }

    @router.get("/factor-runs/{run_id}/tables/{name}")
    def factor_table(run_id: str, name: str):
        result = artifacts.get("factors", run_id)
        if name not in {"history", "diagnostics"} or name not in result.get("tables", {}):
            raise HTTPException(404, "明细文件不存在")
        return FileResponse(
            table_path(store, "factors", run_id, result, name),
            filename=f"{run_id}-{name}.parquet",
        )

    return router
