"""中间件：请求标识传播、结构化访问日志、异常归一化、请求指标。"""
from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .config import settings
from .context import get_request_id, new_request_id, reset_request_id, set_request_id
from .errors import ERROR_STATUS_MAP, AppError, normalize
from .logging_config import get_logger
from .metrics import registry

logger = get_logger("app.middleware")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """解析/生成请求标识，写入上下文并随响应头返回。

    规则（确定且可解释）：
    - 调用方提供了符合 request_id_pattern 的标识 -> 全链路沿用；
    - 未提供或格式非法 -> 生成新标识（非法时记录告警日志）；
    - 请求结束后按 token 复位，杜绝上下文残留与并发串号。
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        header = settings.request_id_header
        provided = request.headers.get(header)
        if provided and settings.request_id_pattern.match(provided):
            request_id = provided
        else:
            if provided is not None:
                logger.warning(
                    "invalid request id replaced",
                    extra={"stage": "request_id", "provided": provided[:80]},
                )
            request_id = new_request_id()
        token = set_request_id(request_id)
        try:
            response = await call_next(request)
        finally:
            reset_request_id(token)
        response.headers[header] = request_id
        return response


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """异常归一化 + 结构化访问日志 + 请求指标。"""

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.perf_counter()
        route = request.url.path  # 路由模板在 call_next 后回填
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:  # noqa: BLE001 - 统一归一化出口
            app_error: AppError = normalize(exc)
            status_code = ERROR_STATUS_MAP[app_error.error_type]
            # 保留原始错误供诊断（detail 不外泄）
            logger.error(
                "request failed",
                extra={
                    "stage": "error",
                    "error_type": app_error.error_type.value,
                    "error_detail": app_error.detail,
                },
            )
            return JSONResponse(
                status_code=status_code,
                content={
                    "error": {
                        "type": app_error.error_type.value,
                        "message": app_error.message,
                        "request_id": get_request_id(),
                    }
                },
            )
        finally:
            duration = time.perf_counter() - start
            route_obj = request.scope.get("route")
            if route_obj is not None:
                route = getattr(route_obj, "path", route)
            registry.record_request(route, status_code, duration)
            logger.info(
                "request completed",
                extra={
                    "stage": "access",
                    "method": request.method,
                    "route": route,
                    "status_code": status_code,
                    "duration_seconds": round(duration, 6),
                },
            )
