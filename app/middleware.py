"""中间件：关联标识传播 + 请求指标统计。

CorrelationIdMiddleware 规则（确定且可解释）：
- 调用方提供且匹配合法格式 -> 原样沿用；
- 未提供或格式非法 -> 生成新标识，并在响应头 X-Correlation-ID-Generated: true 标注；
- 处理结束后 reset 上下文，避免残留。

MetricsMiddleware：
- 按 (method, route 模板, status_code) 计数并记录耗时；
- try/finally 保证每个请求恰好记录一次，异常也计入（状态码 500）。
"""
from __future__ import annotations

import re
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.config import get_settings
from app.context import new_correlation_id, reset_correlation_id, set_correlation_id
from app.logging_config import get_logger
from app.metrics import registry

logger = get_logger(__name__)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """解析/生成关联标识，写入上下文并回写到响应头。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        settings = get_settings()
        header_name = settings.correlation_id_header
        incoming = request.headers.get(header_name)
        generated = False
        if incoming and re.fullmatch(settings.correlation_id_pattern, incoming):
            correlation_id = incoming
        else:
            correlation_id = new_correlation_id()
            generated = True
            if incoming:
                logger.warning(
                    "invalid correlation id rejected",
                    extra={"extra_fields": {"event": "correlation_id.rejected", "incoming": incoming}},
                )
        token = set_correlation_id(correlation_id)
        try:
            response = await call_next(request)
        finally:
            reset_correlation_id(token)
        response.headers[header_name] = correlation_id
        if generated:
            response.headers["X-Correlation-ID-Generated"] = "true"
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    """按路由模板与状态码统计请求数与耗时，每个请求恰好记录一次。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            latency_ms = (time.perf_counter() - start) * 1000.0
            route = request.scope.get("route")
            path_template = getattr(route, "path", None) or request.url.path
            registry.record_request(request.method, path_template, status_code, latency_ms)
