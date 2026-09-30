"""中间件：关联标识传播 + 观测档案留存 + 请求指标统计。

由外到内两层纯 ASGI 中间件（避免 BaseHTTPMiddleware 任务组通道导致的
send 分叉，保证所有响应——含异常处理器直接新建的兜底 500——都经过统一包装）：

ObservabilityMiddleware（最外层）：
- 解析/生成关联标识，写入 contextvars 与 scope state；
- 通过包装 ASGI send 回写 X-Correlation-ID（成功、业务拒绝、依赖失败、
  快速失败、兜底 500、404 均一致）；
- 处理结束后 reset 上下文，避免残留；
- try/finally 为每个真正发出响应的请求恰好留存一条观测档案。

MetricsMiddleware（内层）：
- 按 (method, route 模板, status_code) 计数并记录耗时；
- 在 http.response.start 上记录，每个请求恰好一次，异常响应也计入。

档案存储内部独立淘汰，不影响指标计数、耗时统计与健康检查口径。
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime

from app.archive import RequestRecord, archive
from app.config import get_settings
from app.context import new_correlation_id, reset_correlation_id, set_correlation_id
from app.logging_config import get_logger
from app.metrics import registry

logger = get_logger(__name__)


class ObservabilityMiddleware:
    """纯 ASGI 最外层中间件：关联标识 + 响应头回写 + 观测档案留存。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        header_name = settings.correlation_id_header
        header_name_bytes = header_name.lower().encode("latin-1")
        generated_bytes = b"x-correlation-id-generated"

        incoming = None
        for raw_name, raw_value in scope.get("headers", []):
            if raw_name == header_name_bytes:
                incoming = raw_value.decode("latin-1")
                break

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

        # 写入共享状态：异常处理器与档案留存都从这里取，保证四处标识一致
        state = scope.setdefault("state", {})
        state["correlation_id"] = correlation_id
        state["correlation_id_generated"] = generated

        token = set_correlation_id(correlation_id)
        start = time.perf_counter()
        status_code = 500
        started = False
        cid_bytes = correlation_id.encode("latin-1")

        async def send_wrapper(message) -> None:
            nonlocal status_code, started
            if message["type"] == "http.response.start":
                started = True
                status_code = message["status"]
                # 过滤同名头后统一追加，保证响应头中标识恰好一个值
                message_headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k != header_name_bytes and k != generated_bytes
                ]
                message_headers.append((header_name_bytes, cid_bytes))
                if generated:
                    message_headers.append((generated_bytes, b"true"))
                message["headers"] = message_headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            reset_correlation_id(token)
            # 只有真正发出过响应的请求才留档（客户端中途断连等不产生响应的不留）
            if started:
                route = scope.get("route")
                path_template = getattr(route, "path", None) or scope.get("path", "")
                record = RequestRecord(
                    correlation_id=correlation_id,
                    id_source="generated" if generated else "provided",
                    method=scope.get("method", ""),
                    route=path_template,
                    status_code=status_code,
                    error_type=state.get("error_type"),
                    latency_ms=latency_ms,
                    fast_failed=bool(state.get("dependency_fast_failed", False)),
                    recorded_at=datetime.now(UTC).isoformat(),
                )
                archive.save(record)


class MetricsMiddleware:
    """纯 ASGI 内层中间件：按路由模板与状态码统计请求数与耗时，每请求恰好一次。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_code = 500
        counted = False

        async def send_wrapper(message) -> None:
            nonlocal status_code, counted
            if message["type"] == "http.response.start":
                status_code = message["status"]
                if not counted:
                    counted = True
                    latency_ms = (time.perf_counter() - start) * 1000.0
                    route = scope.get("route")
                    path_template = getattr(route, "path", None) or scope.get(
                        "path", ""
                    )
                    registry.record_request(
                        scope.get("method", ""), path_template, status_code, latency_ms
                    )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # 极端情况下未发出 start 却结束（如上游取消），按 500 兜底计一次，保证不丢
            if not counted:
                latency_ms = (time.perf_counter() - start) * 1000.0
                route = scope.get("route")
                path_template = getattr(route, "path", None) or scope.get("path", "")
                registry.record_request(
                    scope.get("method", ""), path_template, 500, latency_ms
                )
