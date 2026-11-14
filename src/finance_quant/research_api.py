"""Research submission, data download control and task status endpoints."""

import json
import os
import uuid
from contextlib import nullcontext
from functools import partial
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from filelock import Timeout
from starlette.concurrency import run_in_threadpool

from finance_quant.application import backup_jobs, data_updates, financial_download, industry_download
from finance_quant.application import market_download as downloads
from finance_quant.application.market_download import shared_root, status
from finance_quant.application.research_requests import research_options
from finance_quant.data.financials import financial_root
from finance_quant.data.industries import industry_root
from finance_quant.persistence.maintenance import write_gate
from finance_quant.task_contracts import TaskConflict

RESULT_TASK_KINDS = {"research", "experiment"}


def research_router(service, *, result_store=None):
    def write_guard(request: Request):

        engine = getattr(service.store, "engine", None)
        try:
            with write_gate(engine) if engine is not None and request.method == "POST" else nullcontext():
                yield
        except RuntimeError as error:
            raise HTTPException(409, "数据维护期间暂停写入，请稍后重试") from error

    router = APIRouter(prefix="/api/v1/research", dependencies=[Depends(write_guard)])

    async def body(request: Request):
        host = request.headers.get("host", "")
        configured = {item.strip() for item in os.environ.get("ALLOWED_ORIGINS", "").split(",") if item.strip()}
        try:
            local_host = urlsplit("http://" + host).hostname in {"localhost", "127.0.0.1", "::1"}
            configured_host = f"{request.url.scheme}://{host}" in configured
        except ValueError:
            local_host = False
            configured_host = False
        if not (local_host or configured_host):
            raise HTTPException(403, "当前访问地址未配置，请设置 ALLOWED_ORIGINS")
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                same_origin = origin == f"{request.url.scheme}://{host}"
                development = (
                    parsed.hostname in {"localhost", "127.0.0.1"} and parsed.scheme == "http" and parsed.port == 5173
                )
            except ValueError:
                raise HTTPException(403, "请求来源无效") from None
            if not (origin in configured if configured else same_origin or development):
                raise HTTPException(403, "请从已配置的研究工作台提交任务")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "请从已配置的研究工作台提交任务")
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise HTTPException(415, "请使用 JSON 提交研究配置")
        encoded = await request.body()
        if len(encoded) > 65536:
            raise HTTPException(413, "研究配置过大")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate")
                result[key] = value
            return result

        try:
            return json.loads(encoded, object_pairs_hook=unique)
        except (ValueError, UnicodeError):
            raise HTTPException(422, "研究配置必须是有效 JSON，且不能包含重复字段") from None

    @router.get("/options")
    def options():
        return research_options(service.data_root, service.runs_root)

    @router.get("/tasks")
    def tasks():
        """Research and experiment tasks; data downloads are reported by their own panels."""
        result = service.store.list()
        items = [r for r in result["items"] if r.get("kind", "research") in RESULT_TASK_KINDS]
        return {"items": items, "total": len(items)}

    @router.get("/market-download")
    def market_download_status():

        try:
            return status(shared_root(service.data_root))
        except (OSError, ValueError, KeyError):
            raise HTTPException(503, "共享下载状态暂不可读，请稍后刷新；现有数据版本保持不变") from None

    @router.get("/data-updates")
    def data_updates_status():

        return data_updates.status(service.data_root)

    @router.get("/backups")
    def backups_status():

        return backup_jobs.status(service.store)

    @router.post("/backups")
    async def backup_operation(request: Request):
        document = await body(request)

        try:
            return await run_in_threadpool(backup_jobs.start, service.store, document)
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error) if isinstance(error, ValueError) else "备份操作参数类型无效") from None

    @router.post("/data-updates/{action}")
    async def data_updates_action(action: str, request: Request):
        document = await body(request)

        try:
            if action == "preview":
                return await run_in_threadpool(data_updates.preview, service.data_root, document)
            if action == "schedule":
                return data_updates.save_schedule(service.data_root, document)
            if action == "start" and isinstance(document, dict) and set(document) == {"plan_id"}:
                return data_updates.start_task(service.data_root, service.store, document["plan_id"])
            if action == "pause" and document == {}:
                data_updates.pause(service.data_root)
                return {"status": "pause_requested"}
            raise ValueError("不支持的统一更新操作")
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error) if isinstance(error, ValueError) else "更新参数类型无效") from None
        except (RuntimeError, OSError, Timeout):
            raise HTTPException(409, "数据更新暂不可用，请等待当前任务完成或查看日志") from None

    @router.post("/market-download/{action}")
    async def market_download_action(action: str, request: Request):
        document = await body(request)

        root = downloads.shared_root(service.data_root)
        try:
            if action == "preview":
                return await run_in_threadpool(downloads.prepare, root, document)
            if action == "start" and isinstance(document, dict) and set(document) == {"plan_id"}:
                if downloads.active(root):
                    raise ValueError("已有共享下载任务运行，请先暂停或等待完成")
                _, plan = downloads.load_plan(root, document["plan_id"])
                result = service.store.create_download(
                    "market_download",
                    {"name": "共享行情下载", "plan_id": document["plan_id"]},
                    {"root": str(root.resolve()), "plan_id": document["plan_id"]},
                    "download-" + uuid.uuid4().hex,
                    total=len(plan["tasks"]),
                )
                return {"id": document["plan_id"], "task_id": result["id"], "status": "queued"}
            if action == "pause" and document == {}:
                root.mkdir(parents=True, exist_ok=True)
                (root / "PAUSE").write_text("pause", encoding="utf-8")
                return {"status": "pause_requested"}
            raise ValueError("仅支持预览、确认下载、续传和暂停")
        except Timeout:
            raise HTTPException(409, "共享数据管理正在处理，请稍后重试") from None
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error) if isinstance(error, ValueError) else "下载参数类型无效") from None
        except (OSError, RuntimeError):
            raise HTTPException(503, "共享下载暂不可用，请查看本机日志；旧版本保持可用") from None

    @router.get("/financial-download")
    def financial_download_status():

        try:
            return financial_download.status(service.data_root)
        except (OSError, ValueError, KeyError):
            raise HTTPException(503, "财务数据状态暂不可读，请稍后刷新；现有版本保持不变") from None

    @router.post("/financial-download/{action}")
    async def financial_download_action(action: str, request: Request):
        document = await body(request)

        try:
            if action == "preview":
                return await run_in_threadpool(financial_download.preview, service.data_root, document)
            if action == "start" and isinstance(document, dict) and set(document) == {"plan_id"}:
                if financial_download.active(service.data_root):
                    raise ValueError("已有财务数据下载任务运行，请先暂停或等待完成")
                result = await run_in_threadpool(
                    financial_download.start_task, service.data_root, service.store, document["plan_id"]
                )
                return {"id": document["plan_id"], "task_id": result["id"], "status": "queued"}
            if action == "pause" and document == {}:
                root = financial_root(service.data_root)
                root.mkdir(parents=True, exist_ok=True)
                (root / "PAUSE").write_text("pause", encoding="utf-8")
                return {"status": "pause_requested"}
            raise ValueError("仅支持预览、确认下载、续传和暂停")
        except Timeout:
            raise HTTPException(409, "财务数据管理正在处理，请稍后重试") from None
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error) if isinstance(error, ValueError) else "下载参数类型无效") from None
        except (OSError, RuntimeError):
            raise HTTPException(503, "财务数据下载暂不可用，请查看本机日志；旧版本保持可用") from None

    @router.get("/industry-download")
    def industry_download_status():

        try:
            return industry_download.status()
        except (OSError, ValueError, KeyError):
            raise HTTPException(503, "行业数据状态暂不可读，请稍后刷新；现有版本保持不变") from None

    @router.post("/industry-download/{action}")
    async def industry_download_action(action: str, request: Request):
        document = await body(request)

        try:
            if action == "preview":
                return await run_in_threadpool(industry_download.preview, document)
            if action == "start" and isinstance(document, dict) and set(document) == {"plan_id"}:
                result = await run_in_threadpool(
                    industry_download.start_task, service.store, plan_id=document["plan_id"]
                )
                return {"id": document["plan_id"], "task_id": result["id"], "status": "queued"}
            if action == "pause" and document == {}:
                root = industry_root()
                root.mkdir(parents=True, exist_ok=True)
                (root / "PAUSE").write_text("pause", encoding="utf-8")
                return {"status": "pause_requested"}
            raise ValueError("仅支持预览、确认下载、续传和暂停")
        except Timeout:
            raise HTTPException(409, "行业数据管理正在处理，请稍后重试") from None
        except (ValueError, TypeError) as error:
            raise HTTPException(422, str(error) if isinstance(error, ValueError) else "下载参数类型无效") from None
        except (OSError, RuntimeError):
            raise HTTPException(503, "行业数据下载暂不可用，请查看本机日志；旧版本保持可用") from None

    @router.get("/tasks/{identifier}")
    def task(identifier: str):
        try:
            return service.store.get(identifier)
        except FileNotFoundError:
            raise HTTPException(404, "研究任务不存在") from None

    async def queue(request: Request, submit):
        document = await body(request)
        key = request.headers.get("idempotency-key", "")
        try:
            return await run_in_threadpool(submit, document, key)
        except TaskConflict as error:
            raise HTTPException(409, str(error)) from None
        except (ValueError, TypeError) as error:
            # Only validation messages from our parameter layer are returned;
            # TypeError is kept generic for malformed JSON types.
            raise HTTPException(422, str(error) if isinstance(error, ValueError) else "配置字段类型不正确") from None
        except RuntimeError:
            raise HTTPException(503, "任务服务暂不可用，请检查数据库连接") from None

    @router.post("/tasks", status_code=202)
    async def submit(request: Request):
        return await queue(request, service.submit)

    @router.post("/experiments", status_code=202)
    async def submit_experiment(request: Request):
        return await queue(request, partial(service.submit_experiment, result_store=result_store))

    @router.post("/tasks/{identifier}/cancel")
    async def cancel(identifier: str, request: Request):
        document = await body(request)
        if document != {}:
            raise HTTPException(422, "取消任务不接受额外参数")
        try:
            return await run_in_threadpool(service.store.cancel, identifier)
        except FileNotFoundError:
            raise HTTPException(404, "研究任务不存在") from None
        except TaskConflict as error:
            raise HTTPException(409, str(error)) from None

    return router
