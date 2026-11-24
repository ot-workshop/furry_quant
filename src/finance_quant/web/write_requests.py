"""Shared admission checks for the research write endpoints."""

import json
import os
from contextlib import contextmanager, nullcontext
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from finance_quant.persistence.maintenance import MaintenanceBusy, write_gate


class ResearchMaintenanceRejected(HTTPException):
    def __init__(self):
        super().__init__(409, "数据维护期间暂停写入，请稍后重试")


@contextmanager
def research_write_gate(engine):
    try:
        with write_gate(engine) if engine is not None else nullcontext() as connection:
            yield connection
    except MaintenanceBusy as error:
        raise ResearchMaintenanceRejected() from error


async def research_json(request: Request):
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
