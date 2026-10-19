"""Read-only result API. Computation runs separately through the CLI."""

from __future__ import annotations

from pathlib import Path
from datetime import date as Date
from typing import Annotated
import sqlite3

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from finance_quant.sample import NOTICE
from finance_quant.storage import ResultStore
from finance_quant.data.catalog import catalog_status


def create_app(root: Path | None = None, frontend_dist: Path | None = None, data_root: Path | None = None) -> FastAPI:
    app = FastAPI(title="量化研究工作台", version="0.1.0")
    store = ResultStore(root)
    data_root = data_root or Path("data/managed")

    def artifact(category: str, identifier: str) -> dict:
        try:
            return store.get(category, identifier)
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, "结果不存在，请先生成研究结果") from None

    def page(items: list, offset: int, limit: int) -> dict:
        return {"items": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit}

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "read_only": True}

    @app.get("/api/v1/data-status")
    def data_status():
        try:
            return catalog_status(data_root)
        except (sqlite3.Error, OSError, ValueError):
            raise HTTPException(503, "数据目录暂时无法读取，请检查本地下载记录") from None

    @app.get("/api/v1/overview")
    def overview():
        factors = store.list("factors")
        backtests = store.list("backtests")
        instruments = {row["instrument"] for result in factors for row in result["history"]}
        modes = {bool(r["metadata"]["is_synthetic"]) for r in factors}
        modes.update(bool(r["summary"]["is_synthetic"]) for r in backtests)
        mode = "mixed" if len(modes) > 1 else "synthetic" if modes == {True} else "real" if modes == {False} else "empty"
        notices = {"synthetic": NOTICE, "real": "已接入第三方真实行情；评分和回测均可追溯数据版本与模拟假设",
                   "mixed": "真实行情与合成样本分别保存；请以所选评分批次和回测的数据标识为准", "empty": "尚无研究结果"}
        return {"mode": mode, "notice": notices[mode], "factor_runs": len(factors),
                "backtests": len(backtests), "instruments": len(instruments)}

    @app.get("/api/v1/instruments")
    def instruments(q: str = ""):
        found = {}
        for result in store.list("factors"):
            for row in result["history"]:
                if q.casefold() in (row["instrument"] + row["name"]).casefold():
                    found[row["instrument"]] = {"instrument": row["instrument"], "name": row["name"], "exchange": row["instrument"][:2]}
        return {"items": sorted(found.values(), key=lambda row: row["instrument"]), "total": len(found)}

    @app.get("/api/v1/factor-runs")
    def factor_runs():
        items = [{**r["metadata"], "available_dates": sorted({row["date"] for row in r["history"]})}
                 for r in store.list("factors")]
        items.sort(key=lambda item: (not item["is_synthetic"], item["as_of"], item.get("created_at", "")), reverse=True)
        return {"items": items, "total": len(items)}

    @app.get("/api/v1/factor-runs/{run_id}/scores")
    def scores(run_id: str, instrument: str | None = None, sort_by: str = "composite", descending: bool = True,
               date: Date | None = None,
               offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=500)] = 50):
        result = artifact("factors", run_id)
        if sort_by not in {"composite", "rank", "instrument", *result["metadata"]["weights"]}:
            raise HTTPException(422, "不支持的排序字段")
        selected_date = date.isoformat() if date else result["metadata"]["as_of"]
        rows = [r for r in result["history"] if r["date"] == selected_date and (not instrument or r["instrument"] == instrument)]
        rows.sort(key=lambda r: r["scores"][sort_by] if sort_by in r["scores"] else r[sort_by], reverse=descending)
        return {**page(rows, offset, limit), "metadata": {**result["metadata"], "as_of": selected_date,
                                                          "batch_as_of": result["metadata"]["as_of"]}}

    @app.get("/api/v1/instruments/{instrument}/factor-history")
    def factor_history(instrument: str, scheme: str | None = None, run_id: str | None = None):
        results = [artifact("factors", run_id)] if run_id else store.list("factors")
        if run_id and scheme is not None and results[0]["metadata"]["scheme"] != scheme:
            raise HTTPException(422, "评分方案与指定批次不一致")
        results = [r for r in results if scheme is None or r["metadata"]["scheme"] == scheme]
        if len(results) > 1:
            raise HTTPException(409, "存在多个评分版本，请指定 run_id")
        found = {}
        for result in results:
            for row in result["history"]:
                if row["instrument"] == instrument:
                    found[row["date"]] = {"date": row["date"], **row["scores"], "composite": row["composite"], "run_id": result["metadata"]["id"]}
        return {"items": [found[key] for key in sorted(found)], "scheme": results[0]["metadata"]["scheme"] if results else scheme,
                "is_synthetic": bool(results) and all(r["metadata"]["is_synthetic"] for r in results)}

    @app.get("/api/v1/backtests")
    def backtests():
        items = [result["summary"] for result in store.list("backtests")]
        items.sort(key=lambda item: item["created_at"], reverse=True)
        return {"items": items, "total": len(items)}

    @app.get("/api/v1/backtests/{run_id}")
    def backtest(run_id: str):
        result = artifact("backtests", run_id)
        return {**result["summary"], **result["metadata"]}

    @app.get("/api/v1/backtests/{run_id}/equity")
    def equity(run_id: str):
        return {"items": artifact("backtests", run_id)["equity"]}

    @app.get("/api/v1/backtests/{run_id}/positions")
    def positions(run_id: str, date: Date | None = None, offset: Annotated[int, Query(ge=0)] = 0,
                  limit: Annotated[int, Query(ge=1, le=500)] = 50):
        rows = artifact("backtests", run_id)["positions"]
        return page([r for r in rows if date is None or r["date"] == date.isoformat()], offset, limit)

    @app.get("/api/v1/backtests/{run_id}/trades")
    def trades(run_id: str, offset: Annotated[int, Query(ge=0)] = 0,
               limit: Annotated[int, Query(ge=1, le=500)] = 50):
        return page(artifact("backtests", run_id)["trades"], offset, limit)

    @app.get("/api/v1/backtests/{run_id}/download")
    def download(run_id: str):
        result = artifact("backtests", run_id)
        return JSONResponse(result, headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'})

    @app.get("/api/v1/evaluations")
    def evaluations():
        items = [result["metadata"] for result in store.list("evaluations")]
        items.sort(key=lambda row: row["created_at"], reverse=True)
        return {"items": items, "total": len(items)}

    @app.get("/api/v1/evaluations/{run_id}")
    def evaluation(run_id: str):
        return artifact("evaluations", run_id)

    @app.get("/api/v1/evaluations/{run_id}/download")
    def download_evaluation(run_id: str):
        return JSONResponse(artifact("evaluations", run_id),
                            headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'})

    @app.get("/api/v1/evaluations/{run_id}/report")
    def download_evaluation_report(run_id: str):
        from finance_quant.research.report import evaluation_markdown
        return PlainTextResponse(evaluation_markdown(artifact("evaluations", run_id)), media_type="text/markdown",
                                 headers={"Content-Disposition": f'attachment; filename="{run_id}.md"'})

    @app.get("/api/{unknown:path}")
    def unknown_api(unknown: str):
        raise HTTPException(404, "接口不存在")

    dist = frontend_dist or Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    else:
        @app.get("/")
        def index():
            return {"message": "前端尚未构建，请在 frontend 目录执行 npm install 和 npm run build", "docs": "/docs"}
    return app
