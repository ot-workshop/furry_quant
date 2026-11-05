"""Research submission, data download control and task status endpoints."""

import json
import os
import uuid
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from filelock import Timeout
from starlette.concurrency import run_in_threadpool

from finance_quant.application.research_requests import research_options
from finance_quant.application.tasks import TaskConflict

RESULT_TASK_KINDS = {"research", "experiment"}


def research_router(service):
    router = APIRouter(prefix="/api/v1/research")

    async def body(request: Request):
        host = request.headers.get("host", "")
        try:
            local_host = urlsplit("http://" + host).hostname in {"localhost", "127.0.0.1", "::1"}
        except ValueError:
            local_host = False
        if not local_host:
            raise HTTPException(403, "研究任务仅支持本机访问")
        origin = request.headers.get("origin")
        if origin:
            try:
                parsed = urlsplit(origin)
                same_origin = origin == f"{request.url.scheme}://{host}"
                development = (
                    parsed.hostname in {"localhost", "127.0.0.1"} and parsed.scheme == "http" and parsed.port == 5173
                )
                configured = {item.strip() for item in os.environ.get("ALLOWED_ORIGINS", "").split(",") if item.strip()}
            except ValueError:
                raise HTTPException(403, "请求来源无效") from None
            if not (origin in configured if configured else same_origin or development):
                raise HTTPException(403, "请从本机研究工作台提交任务")
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "请从本机研究工作台提交任务")
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
        from finance_quant.application.market_download import shared_root, status

        try:
            return status(shared_root(service.data_root))
        except (OSError, ValueError, KeyError):
            raise HTTPException(503, "共享下载状态暂不可读，请稍后刷新；现有数据版本保持不变") from None

    @router.post("/market-download/{action}")
    async def market_download_action(action: str, request: Request):
        document = await body(request)
        from finance_quant.application import market_download as downloads

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

    @router.get("/index-download")
    def index_status():
        from finance_quant.application.index_download_manager import status

        return status()

    @router.get("/index-download/report")
    @router.get("/index-download/report/download")
    def index_handling_report(request: Request):
        from starlette.responses import JSONResponse

        from finance_quant.application.index_download_manager import warehouse_root
        from finance_quant.application.index_download_reports import refresh_handling_report

        try:
            report = refresh_handling_report(warehouse_root())
        except Timeout:
            raise HTTPException(409, "处理报告正在生成，请稍后刷新") from None
        except (OSError, ValueError, KeyError):
            raise HTTPException(503, "处理报告读取失败，原下载状态未改变，请检查本机报告及来源文件") from None
        headers = {"Cache-Control": "no-store"}
        if request.url.path.endswith("/download"):
            headers["Content-Disposition"] = 'attachment; filename="index-handling-report.json"'
        return JSONResponse(report, headers=headers)

    @router.post("/index-download/{action}")
    async def index_action(action: str, request: Request):
        document = await body(request)
        if document != {} or action not in {"start", "pause"}:
            raise HTTPException(422, "仅支持开始/续传或暂停，不接受额外参数")
        from finance_quant.application import index_download_manager as manager

        try:
            if action == "pause":
                return await run_in_threadpool(manager.pause)
            root = manager.warehouse_root()
            current = manager.status(root)
            if current.get("worker_running"):
                return current
            service.store.create_download(
                "index_download",
                {"name": "沪深300历史下载"},
                {"root": str(root.resolve())},
                "index-" + uuid.uuid4().hex,
            )
            return {**current, "status": "queued"}
        except Timeout:
            raise HTTPException(409, "下载管理正在处理，请稍后重试") from None
        except (RuntimeError, OSError):
            raise HTTPException(503, "无法提交下载任务，请检查服务日志") from None

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
        return await queue(request, service.submit_experiment)

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
