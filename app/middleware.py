"""中间件：关联标识传播 + 请求指标统计 + 请求观测档案（纯 ASGI 实现）。

使用纯 ASGI 而非 BaseHTTPMiddleware：
- 下游（路由、异常处理器）在同一任务内直接 await，contextvars 天然可见，
  即使走到最后一层兜底（500），异常处理器也能拿到当前请求标识；
- 包装 send 在 http.response.start 上统一回写响应头，对正常响应和错误响应都生效；
- 避免 BaseHTTPMiddleware 多层嵌套时异常经 TaskGroup 重抛造成的上下文丢失，
  也减少一次任务切换的额外延迟。

CorrelationIdMiddleware 规则（确定且可解释）：
- 调用方提供且匹配合法格式 -> 原样沿用；
- 未提供或格式非法 -> 生成新标识，并在响应头 X-Correlation-ID-Generated: true 标注；
- 解析结果（标识、来源）写入共享 scope，供内层 ArchiveMiddleware 归档；
- 处理结束后 reset 上下文，避免残留。

MetricsMiddleware：
- 按 (method, route 模板, status_code) 计数并记录耗时；
- try/finally 保证每个请求恰好记录一次，异常也计入（状态码 500）。

ArchiveMiddleware：
- 在出口处为每个真正返回了响应的请求留一份观测档案（方法、路由、状态、
  结果归类、错误分类、耗时、标识来源），按响应中的 correlation_id 可精确回查；
- 归档与指标统计互不依赖，档案淘汰不影响指标与健康检查口径。
"""
from __future__ import annotations

import asyncio
import contextvars
import re
import time
from datetime import UTC, datetime

from app.archive import RequestRecord, archive
from app.config import get_settings
from app.context import new_correlation_id, reset_correlation_id, set_correlation_id
from app.errors import classify_outcome, error_type_for_status
from app.logging_config import get_logger
from app.metrics import registry

logger = get_logger(__name__)

#: 外层关联标识中间件写入 scope 的键，供内层档案中间件读取
SCOPE_CORRELATION_ID = "obs_correlation_id"
SCOPE_CORRELATION_GENERATED = "obs_correlation_id_generated"


def _header(scope: dict, name: bytes) -> str | None:
    """从 ASGI scope 读取请求头（name 需小写 bytes），不存在返回 None。"""
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


class CorrelationIdMiddleware:
    """解析/生成关联标识，写入上下文与 scope，并回写到任意响应（含错误响应）头。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        header_name = settings.correlation_id_header
        incoming = _header(scope, header_name.lower().encode("latin-1"))
        generated = False
        if incoming and re.fullmatch(settings.correlation_id_pattern, incoming):
            correlation_id = incoming
        else:
            correlation_id = new_correlation_id()
            generated = True
            if incoming:
                logger.warning(
                    "invalid correlation id rejected",
                    extra={
                        "extra_fields": {
                            "event": "correlation_id.rejected",
                            "incoming": incoming,
                        }
                    },
                )
        token = set_correlation_id(correlation_id)
        scope[SCOPE_CORRELATION_ID] = correlation_id
        scope[SCOPE_CORRELATION_GENERATED] = generated
        header_bytes = header_name.encode("latin-1")
        cid_bytes = correlation_id.encode("latin-1")

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                # 同名头先移除再追加，保证响应中恰好一个 X-Correlation-ID
                message["headers"] = [
                    (k, v) for k, v in message["headers"] if k != header_bytes
                ]
                message["headers"].append((header_bytes, cid_bytes))
                if generated:
                    message["headers"].append(
                        (b"x-correlation-id-generated", b"true")
                    )
            await send(message)

        try:
            # 在复制出的 Context 中运行整个下游（显式 context 的 task）。
            # FastAPI 内部存在 anyio TaskGroup：其中 set 的 ContextVar 不会
            # 回传到 TaskGroup 之外的异常处理帧，导致兜底异常处理器读到 None。
            # 让下游整体跑在「已设置标识」的复制 Context 内后，无论成功、业务
            # 拒绝、依赖失败还是走到最后兜底，异常处理器、日志与后台任务读到的
            # 都是本请求标识；Context 随 task 结束而丢弃，并发请求互不串号。
            request_ctx = contextvars.copy_context()
            request_ctx.run(set_correlation_id, correlation_id)
            await asyncio.create_task(
                self.app(scope, receive, send_with_headers), context=request_ctx
            )
        finally:
            reset_correlation_id(token)


class MetricsMiddleware:
    """按路由模板与状态码统计请求数与耗时，每个请求恰好记录一次。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status_code = 500

        async def send_capture(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_capture)
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            route = scope.get("route")
            path_template = getattr(route, "path", None) or scope.get("path", "")
            registry.record_request(
                scope.get("method", ""), path_template, status_code, latency_ms
            )


class ArchiveMiddleware:
    """出口处归档每个请求，按响应中的 correlation_id 保证可精确回查一条。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started_at = datetime.now(UTC).isoformat()
        start = time.perf_counter()
        status_code = 500

        async def send_capture(message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_capture)
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            route = scope.get("route")
            path_template = getattr(route, "path", None) or scope.get("path", "")
            correlation_id = scope.get(SCOPE_CORRELATION_ID)
            generated = scope.get(SCOPE_CORRELATION_GENERATED, False)
            if correlation_id is not None:
                archive.save(
                    RequestRecord(
                        correlation_id=correlation_id,
                        correlation_id_source="generated" if generated else "client",
                        method=scope.get("method", ""),
                        route=path_template,
                        status_code=status_code,
                        outcome=classify_outcome(status_code),
                        error_type=error_type_for_status(status_code),
                        latency_ms=round(latency_ms, 3),
                        started_at=started_at,
                    )
                )


class CatchAllErrorMiddleware:
    """在请求标识所在 Context 内兜住未预期异常，归一化为 500 响应。

    FastAPI/Starlette 中用户注册的 Exception 处理器实际挂在最外层
    ServerErrorMiddleware：未预期异常逃出用户中间件的任务后才在其帧中处理，
    读不到本请求 Context 内的标识。该层位于 CorrelationIdMiddleware 的
    复制 Context 任务之内，except 帧与下游同 Context，因此错误响应体、日志、
    响应头、档案中的标识完全一致；AppError 等已注册异常在更内层已被处理，
    这里只会兜住真正未预期的异常，口径与 unexpected_error / 500 一致。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            from starlette.responses import JSONResponse

            from app.context import get_correlation_id
            from app.errors import normalize_exception, to_error_payload

            normalized = normalize_exception(exc)
            logger.error(
                "unexpected error",
                exc_info=exc,
                extra={"extra_fields": {"event": "request.unexpected_error"}},
            )
            response = JSONResponse(
                status_code=normalized.status_code,
                content=to_error_payload(normalized, get_correlation_id()),
            )
            await response(scope, receive, send)
