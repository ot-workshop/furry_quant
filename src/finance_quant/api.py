"""Result queries and local queued research, served alongside the frontend."""

from __future__ import annotations

from pathlib import Path
from datetime import date as Date
from typing import Annotated
from contextlib import asynccontextmanager
import sqlite3

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from finance_quant.sample import NOTICE
from finance_quant.storage import ResultStore
from finance_quant.data.catalog import catalog_status
from finance_quant.research.performance import enrich_backtest


def create_app(root: Path | None = None, frontend_dist: Path | None = None, data_root: Path | None = None, *, task_service=None) -> FastAPI:
    store = ResultStore(root)
    data_root = data_root or Path("data/managed")
    from finance_quant.application.tasks import TaskService
    from finance_quant.research_api import research_router
    service = task_service or TaskService(store.root, data_root)

    @asynccontextmanager
    async def lifespan(app):
        service.start()
        try:
            yield
        finally:
            service.close()

    app = FastAPI(title="量化研究工作台", version="0.2.0", lifespan=lifespan)
    app.state.research_tasks = service
    app.include_router(research_router(service))
    from finance_quant.storage_tables import ResultTableError

    @app.exception_handler(ResultTableError)
    async def invalid_table(request,error):
        return JSONResponse(status_code=409,content={'detail':str(error)})

    def artifact(category: str, identifier: str) -> dict:
        try:
            result=store.get(category, identifier,hydrate=False)
            if not isinstance(result,dict) or not isinstance(result.get('metadata'),dict):raise ValueError('Invalid artifact')
            if category=='backtests' and not isinstance(result.get('summary'),dict):raise ValueError('Invalid backtest')
            if category=='backtests':result['summary']={**result['summary'],'id':identifier}
            else:result['metadata']={**result['metadata'],'id':identifier}
            return result
        except FileNotFoundError:
            raise HTTPException(404, "结果不存在，请先生成研究结果") from None
        except ValueError:
            try:exists=store.path(category,identifier).is_file()
            except ValueError:exists=False
            raise HTTPException(409 if exists else 404,'结果文件损坏或格式异常，请重新运行该配置' if exists else '结果不存在，请先生成研究结果') from None

    def page(items: list, offset: int, limit: int) -> dict:
        return {"items": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit}

    def factor_metadata(result):
        metadata=dict(result['metadata'])
        metadata.pop('industry_coverage_by_date',None)
        if result.get('tables'):
            for key in ['membership_by_execution_date','coverage_by_date','availability_by_date','lifecycle_exclusions','industry_coverage_by_date']:
                metadata.pop(key,None)
        return metadata

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "read_only": False, "task_execution": "local_queue"}

    @app.get("/api/v1/data-status")
    def data_status():
        try:
            return catalog_status(data_root)
        except (sqlite3.Error, OSError, ValueError):
            raise HTTPException(503, "数据目录暂时无法读取，请检查本地下载记录") from None

    @app.get("/api/v1/overview")
    def overview():
        factors,unavailable = store.list_metadata("factors")
        backtests,bad_backtests = store.list_metadata("backtests")
        _,bad_evaluations=store.list_metadata('evaluations')
        unavailable+=bad_backtests+bad_evaluations
        instruments = {row["instrument"] for result in factors for row in result['metadata']['universe_members']}
        modes = {bool(r["metadata"]["is_synthetic"]) for r in factors}
        modes.update(bool(r["summary"]["is_synthetic"]) for r in backtests)
        mode = "mixed" if len(modes) > 1 else "synthetic" if modes == {True} else "real" if modes == {False} else "empty"
        notices = {"synthetic": NOTICE, "real": "真实行情按批次保存；请查看所选批次的数据来源、校验等级与模拟假设",
                   "mixed": "真实行情与合成样本分别保存；请以所选评分批次和回测的数据标识为准", "empty": "尚无研究结果"}
        notice=notices[mode]+(f'；有{len(unavailable)}条结果暂不可读，已隔离显示，其他结果仍可查看' if unavailable else '')
        return {"mode": mode, "notice":notice, "factor_runs": len(factors),
                "backtests": len(backtests), "instruments": len(instruments),**({'unavailable_results':unavailable} if unavailable else {})}

    @app.get("/api/v1/instruments")
    def instruments(q: str = "", run_id: str | None = None):
        found = {}
        results = [artifact("factors", run_id)] if run_id else store.list_metadata("factors")[0]
        for result in results:
            for row in result['metadata'].get('universe_members', result.get("history",[])):
                if q.casefold() in (row["instrument"] + row["name"]).casefold():
                    found[row["instrument"]] = {"instrument": row["instrument"], "name": row["name"], "exchange": row["instrument"][:2]}
        return {"items": sorted(found.values(), key=lambda row: row["instrument"]), "total": len(found)}

    @app.get("/api/v1/factor-runs")
    def factor_runs():
        records,unavailable=store.list_metadata('factors')
        items = [factor_metadata(r) for r in records]
        items.sort(key=lambda item: (not item["is_synthetic"], item["as_of"], item.get("created_at", "")), reverse=True)
        return {"items": items, "total": len(items),**({'unavailable':unavailable} if unavailable else {})}

    @app.get("/api/v1/factor-runs/{run_id}/scores")
    def scores(run_id: str, instrument: str | None = None, sort_by: str = "composite", descending: bool = True,
               date: Date | None = None,
               offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=500)] = 50):
        result = artifact("factors", run_id)
        if sort_by not in {"composite", "rank", "instrument", *result["metadata"]["weights"]}:
            raise HTTPException(422, "不支持的排序字段")
        selected_date = date.isoformat() if date else result["metadata"]["as_of"]
        from finance_quant.storage_tables import read_rows
        filters={'date':selected_date,**({'instrument':instrument} if instrument else {})}
        rows,_=read_rows(store,'factors',run_id,result,'history',filters=filters)
        rows.sort(key=lambda r: r["scores"][sort_by] if sort_by in r["scores"] else r[sort_by], reverse=descending)
        excluded,_=read_rows(store,'factors',run_id,result,'diagnostics',filters=filters)
        return {**page(rows, offset, limit), 'diagnostics': excluded,
                "metadata": {**factor_metadata(result), "as_of": selected_date, 'coverage': result['metadata'].get('availability_by_date', {}).get(selected_date),
                             "batch_as_of": result["metadata"]["as_of"]}}

    @app.get('/api/v1/factor-runs/{run_id}/industries')
    def factor_industries(run_id:str,date:Date|None=None):
        from finance_quant.storage_tables import read_rows
        from finance_quant.research.neutralization import factor_industry_diagnostics
        result=artifact('factors',run_id);day=date.isoformat() if date else result['metadata']['as_of']
        rows,_=read_rows(store,'factors',run_id,result,'history',filters={'date':day})
        report=factor_industry_diagnostics(rows,result['metadata'])
        return {**report,'date':day,'coverage':result['metadata'].get('industry_coverage_by_date',{}).get(day)}

    @app.get("/api/v1/instruments/{instrument}/factor-history")
    def factor_history(instrument: str, scheme: str | None = None, run_id: str | None = None):
        results = [artifact("factors", run_id)] if run_id else store.list_metadata("factors")[0]
        if run_id and scheme is not None and results[0]["metadata"]["scheme"] != scheme:
            raise HTTPException(422, "评分方案与指定批次不一致")
        results = [r for r in results if scheme is None or r["metadata"]["scheme"] == scheme]
        if len(results) > 1:
            raise HTTPException(409, "存在多个评分版本，请指定 run_id")
        if not run_id and results:results=[artifact('factors',results[0]['metadata']['id'])]
        found = {}
        for result in results:
            from finance_quant.storage_tables import read_rows
            rows,_=read_rows(store,'factors',result['metadata']['id'],result,'history',filters={'instrument':instrument})
            for row in rows:
                if row["instrument"] == instrument:
                    found[row["date"]] = {"date": row["date"], **row["scores"], "composite": row["composite"], "run_id": result["metadata"]["id"]}
        return {"items": [found[key] for key in sorted(found)], "scheme": results[0]["metadata"]["scheme"] if results else scheme,
                "is_synthetic": bool(results) and all(r["metadata"]["is_synthetic"] for r in results)}

    @app.get("/api/v1/backtests")
    def backtests():
        records,unavailable=store.list_metadata('backtests')
        items = [result["summary"] for result in records]
        items.sort(key=lambda item: item["created_at"], reverse=True)
        return {"items": items, "total": len(items),**({'unavailable':unavailable} if unavailable else {})}

    @app.get("/api/v1/backtests/{run_id}")
    def backtest(run_id: str):
        result = enrich_backtest(artifact("backtests", run_id))
        return {**result["summary"], **result["metadata"], 'performance': result.get('performance')}

    @app.get("/api/v1/backtests/{run_id}/equity")
    def equity(run_id: str):
        return {"items": artifact("backtests", run_id)["equity"]}

    @app.get('/api/v1/backtests/{run_id}/industries')
    def backtest_industries(run_id:str,date:Date|None=None):
        result=artifact('backtests',run_id);report=result.get('industry_exposure')
        if not report:return {'items':[],'available':False,'note':'旧结果未保存历史行业诊断，请复用配置重新运行；原结果保留。'}
        day=date.isoformat() if date else result['equity'][-1]['date']
        selected=next((r for r in report['days'] if r['date']==day),None)
        if selected is None:raise HTTPException(422,'所选日期没有行业暴露记录')
        payload={**{k:v for k,v in report.items() if k!='days'},**selected,'available':True}
        meta=result['metadata']
        code=meta.get('benchmark',{}).get('code') or meta.get('benchmark_code') or meta.get('provenance',{}).get('benchmark') or '000300.SH'
        if code not in {'000300.SH','H00300.CSI'}:
            payload['benchmark_available']=False;payload['benchmark_note']='当前基准无历史行业权重，不进行替代比较。'
            payload['items']=[{**r,'benchmark_weight':None,'active_weight':None} for r in payload['items']]
        return payload

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
        result = enrich_backtest(artifact("backtests", run_id))
        return JSONResponse(result, headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'})

    @app.get('/api/v1/backtests/{run_id}/execution-events')
    def execution_events(run_id: str, offset: Annotated[int,Query(ge=0)]=0, limit: Annotated[int,Query(ge=1,le=500)]=50):
        return page(artifact('backtests',run_id).get('execution_events',[]),offset,limit)

    @app.get("/api/v1/evaluations")
    def evaluations():
        records,unavailable=store.list_metadata('evaluations')
        items = [result["metadata"] for result in records]
        items.sort(key=lambda row: row["created_at"], reverse=True)
        return {"items": items, "total": len(items),**({'unavailable':unavailable} if unavailable else {})}

    @app.get("/api/v1/evaluations/{run_id}")
    def evaluation(run_id: str):
        return artifact("evaluations", run_id)

    @app.get('/api/v1/evaluations/{run_id}/labels/{horizon}')
    def label_rows(run_id:str,horizon:int,offset:Annotated[int,Query(ge=0)]=0,limit:Annotated[int,Query(ge=1,le=500)]=50):
        from finance_quant.storage_tables import read_rows
        result=artifact('evaluations',run_id)
        if str(horizon) not in result.get('labels',{}):raise HTTPException(404,'持有期不存在')
        rows,total=read_rows(store,'evaluations',run_id,result,f'labels/{horizon}/observations',offset=offset,limit=limit)
        return {'items':rows,'total':total,'offset':offset,'limit':limit}

    @app.get('/api/v1/evaluations/{run_id}/labels/{horizon}/download')
    def label_download(run_id:str,horizon:int):
        from finance_quant.storage_tables import table_path
        from fastapi.responses import FileResponse
        result=artifact('evaluations',run_id);path=f'labels/{horizon}/observations'
        if path not in result.get('tables',{}):raise HTTPException(404,'该批次没有独立标签文件')
        return FileResponse(table_path(store,'evaluations',run_id,result,path),filename=f'{run_id}-labels-{horizon}.parquet')

    @app.get('/api/v1/factor-runs/{run_id}/tables/{name}')
    def factor_table(run_id:str,name:str):
        from finance_quant.storage_tables import table_path
        from fastapi.responses import FileResponse
        result=artifact('factors',run_id)
        if name not in {'history','diagnostics'} or name not in result.get('tables',{}):raise HTTPException(404,'明细文件不存在')
        return FileResponse(table_path(store,'factors',run_id,result,name),filename=f'{run_id}-{name}.parquet')

    @app.get("/api/v1/evaluations/{run_id}/download")
    def download_evaluation(run_id: str):
        return JSONResponse(artifact("evaluations", run_id),
                            headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'})

    @app.get("/api/v1/evaluations/{run_id}/report")
    def download_evaluation_report(run_id: str):
        from finance_quant.research.report import evaluation_markdown
        return PlainTextResponse(evaluation_markdown(artifact("evaluations", run_id)), media_type="text/markdown",
                                 headers={"Content-Disposition": f'attachment; filename="{run_id}.md"'})

    @app.get("/api/v1/experiments")
    def experiments():
        items = [result["metadata"] for result in store.list("experiments")]
        items.sort(key=lambda row: row["created_at"], reverse=True)
        return {"items": items, "total": len(items)}

    @app.get("/api/v1/experiments/{run_id}")
    def experiment(run_id: str):
        return artifact("experiments", run_id)

    @app.get("/api/v1/experiments/{run_id}/download")
    def download_experiment(run_id: str):
        return JSONResponse(artifact("experiments", run_id),
                            headers={"Content-Disposition": f'attachment; filename="{run_id}.json"'})

    @app.get("/api/v1/experiments/{run_id}/report")
    def download_experiment_report(run_id: str):
        from finance_quant.research.experiments import experiment_markdown
        return PlainTextResponse(experiment_markdown(artifact("experiments", run_id)), media_type="text/markdown",
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
