"""FastAPI 应用装配：路由、中间件、异常处理器、健康检查与指标端点。"""

from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware import Middleware
from starlette.middleware.exceptions import ExceptionMiddleware

from app.archive import archive
from app.config import get_settings
from app.context import get_correlation_id
from app.dependencies import VALID_MODES, dependency
from app.errors import AppError, normalize_exception, to_error_payload
from app.logging_config import configure_logging, get_logger
from app.metrics import registry
from app.middleware import MetricsMiddleware, ObservabilityMiddleware
from app.resilience import breaker

logger = get_logger("app.request")


class DependencyModeIn(BaseModel):
    mode: str = Field(pattern=f"^({'|'.join(VALID_MODES)})$")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    dependency.set_mode(settings.dependency_mode)
    archive.reset(max_records=settings.archive_max_records)
    breaker.configure(
        failure_threshold=settings.cb_failure_threshold,
        window_s=settings.cb_window_s,
        min_samples=settings.cb_min_samples,
        failure_ratio=settings.cb_failure_ratio,
        open_cooldown_s=settings.cb_open_cooldown_s,
    )

    app = FastAPI(title=settings.service_name)
    # 注意：后 add 的中间件先执行。由外到内：
    # Observability（纯 ASGI，关联标识+档案+响应头）-> Metrics（最内，拿路由模板）
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(ObservabilityMiddleware)

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        # 供观测档案记录最终错误分类与是否走了快速失败路径
        request.state.error_type = exc.error_type
        request.state.dependency_fast_failed = bool(getattr(exc, "fast_failed", False))
        # 异常处理器可能运行在独立上下文（contextvar 取不到值），统一以 request.state 为准
        correlation_id = request.state.correlation_id
        logger.warning(
            "request failed",
            extra={
                "extra_fields": {
                    "event": "request.failed",
                    "error_type": exc.error_type,
                    "status_code": exc.status_code,
                    "fast_failed": request.state.dependency_fast_failed,
                    "correlation_id": correlation_id,
                    "cause": repr(exc.cause) if exc.cause else None,
                }
            },
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=to_error_payload(exc, correlation_id),
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        normalized = normalize_exception(exc)
        request.state.error_type = normalized.error_type
        request.state.dependency_fast_failed = bool(
            getattr(normalized, "fast_failed", False)
        )
        correlation_id = request.state.correlation_id
        logger.error(
            "unexpected error",
            exc_info=exc,
            extra={
                "extra_fields": {
                    "event": "request.unexpected_error",
                    "correlation_id": correlation_id,
                }
            },
        )
        return JSONResponse(
            status_code=normalized.status_code,
            content=to_error_payload(normalized, correlation_id),
        )

    # FastAPI 默认把通用 Exception 处理器挂在最外层 ServerErrorMiddleware 上，
    # 其响应会绕过用户中间件的 ASGI send 包装（导致兜底 500 缺关联标识响应头、
    # 档案也拿不到最终状态）。显式在用户中间件栈最内层（append 到 user_middleware
    # 末尾，构建时紧挨 FastAPI 自带 ExceptionMiddleware）插入一个带通用处理器的
    # ExceptionMiddleware，保证所有错误响应都经过标识回写与档案留存。
    app.user_middleware.append(
        Middleware(ExceptionMiddleware, handlers={Exception: unexpected_error_handler})
    )
    app.exception_handlers.pop(Exception, None)
    app.middleware_stack = None  # 强制按更新后的 user_middleware 重建栈

    @app.get("/")
    def read_root() -> dict:
        return {"Hello": "World"}

    @app.get("/items/{item_id}")
    async def get_item(item_id: int, background_tasks: BackgroundTasks) -> dict:
        cid = get_correlation_id()
        logger.info(
            "item request received",
            extra={"extra_fields": {"event": "item.received", "item_id": item_id}},
        )
        if item_id <= 0:
            from app.errors import BusinessRejected

            raise BusinessRejected(f"item_id must be positive, got {item_id}")
        result = await dependency.fetch(f"item-{item_id}")
        # 后台任务继承当前 contextvars 上下文，关联标识保持不变
        background_tasks.add_task(_audit_log, item_id)
        logger.info(
            "item request completed",
            extra={"extra_fields": {"event": "item.completed", "item_id": item_id}},
        )
        return {"item_id": item_id, "correlation_id": cid, "data": result}

    @app.get("/health")
    async def health() -> JSONResponse:
        mode_health = dependency.health()  # healthy / degraded / down（当下模式）
        dep_health = {"down": "unavailable"}.get(mode_health, mode_health)
        circuit = (
            dependency.circuit_health()
        )  # healthy / degraded / unavailable（近期表现）
        snap = breaker.snapshot()
        # 当下模式（instant）与近期熔断器表现（recent）取更差者：
        # 任一为 unavailable -> 503；否则任一为 degraded -> degraded
        ranking = {"healthy": 0, "degraded": 1, "unavailable": 2}
        merged = dep_health if ranking[dep_health] >= ranking[circuit] else circuit
        status = {
            "healthy": "ok",
            "degraded": "degraded",
            "unavailable": "unavailable",
        }[merged]
        code = 503 if merged == "unavailable" else 200
        return JSONResponse(
            status_code=code,
            content={
                "status": status,
                "dependency": dep_health,
                "circuit": circuit,
                "circuit_detail": snap,
            },
        )

    @app.get("/observability/requests/{correlation_id}")
    async def get_request_record(correlation_id: str) -> JSONResponse:
        """按请求标识回查唯一一条观测档案；不存在（含已淘汰）返回 404。"""
        record = archive.get(correlation_id)
        if record is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "type": "request_record_not_found",
                        "message": f"no retained request record for {correlation_id!r}",
                        "correlation_id": get_correlation_id(),
                        "queried_correlation_id": correlation_id,
                    }
                },
            )
        return JSONResponse(status_code=200, content=record.to_dict())

    @app.get("/metrics")
    async def metrics() -> dict:
        return registry.snapshot()

    @app.post("/admin/dependency-mode")
    async def set_dependency_mode(body: DependencyModeIn) -> dict:
        dependency.set_mode(body.mode)
        logger.info(
            "dependency mode changed",
            extra={
                "extra_fields": {"event": "dependency.mode_changed", "mode": body.mode}
            },
        )
        return {"mode": dependency.mode}

    return app


def _audit_log(item_id: int) -> None:
    """后台任务：验证关联标识在后台任务中保持不变。"""
    logger.info(
        "background audit",
        extra={
            "extra_fields": {
                "event": "item.audit",
                "item_id": item_id,
                "correlation_id": get_correlation_id(),
            }
        },
    )


app = create_app()
