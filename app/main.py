"""FastAPI 应用装配：路由、中间件、异常处理器、健康检查、指标与档案回查端点。"""
from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.archive import archive
from app.config import get_settings
from app.context import get_correlation_id
from app.dependencies import VALID_MODES, dependency
from app.errors import AppError, normalize_exception, to_error_payload
from app.logging_config import configure_logging, get_logger
from app.metrics import registry
from app.middleware import (
    ArchiveMiddleware,
    CatchAllErrorMiddleware,
    CorrelationIdMiddleware,
    MetricsMiddleware,
)
from app.resilience import breaker

logger = get_logger("app.request")


class DependencyModeIn(BaseModel):
    mode: str = Field(pattern=f"^({'|'.join(VALID_MODES)})$")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    dependency.set_mode(settings.dependency_mode)
    archive.resize(settings.archive_max_records)
    breaker.configure(
        fail_threshold=settings.breaker_fail_threshold,
        window_size=settings.breaker_window_size,
        fail_ratio=settings.breaker_fail_ratio,
        cooldown_s=settings.breaker_cooldown_s,
    )
    breaker.reset()

    app = FastAPI(title=settings.service_name)
    # 执行顺序由外到内：Correlation -> Archive -> Metrics -> CatchAll -> 路由。
    # CatchAll 在标识所在 Context 任务内兜住未预期异常，使 500 兜底响应、
    # 日志、响应头、档案看到的标识完全一致；Metrics/Archive 据其 500 响应归档统计。
    app.add_middleware(CatchAllErrorMiddleware)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(ArchiveMiddleware)
    app.add_middleware(CorrelationIdMiddleware)

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "request failed",
            extra={
                "extra_fields": {
                    "event": "request.failed",
                    "error_type": exc.error_type,
                    "status_code": exc.status_code,
                    "cause": repr(exc.cause) if exc.cause else None,
                }
            },
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=to_error_payload(exc, get_correlation_id()),
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        normalized = normalize_exception(exc)
        logger.error(
            "unexpected error",
            exc_info=exc,
            extra={"extra_fields": {"event": "request.unexpected_error"}},
        )
        return JSONResponse(
            status_code=normalized.status_code,
            content=to_error_payload(normalized, get_correlation_id()),
        )

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
        dep_health = dependency.health()
        status = {"healthy": "ok", "degraded": "degraded", "down": "unavailable"}[dep_health]
        code = 503 if dep_health == "down" else 200
        return JSONResponse(
            status_code=code,
            content={
                "status": status,
                "dependency": dep_health,
                "circuit": breaker.snapshot(),
            },
        )

    @app.get("/metrics")
    async def metrics() -> dict:
        return registry.snapshot()

    @app.get("/observability/requests/{correlation_id}")
    async def get_request_archive(correlation_id: str) -> JSONResponse:
        """按请求标识回查一份观测档案；查无（含已淘汰）返回 404。"""
        record = archive.get(correlation_id)
        if record is None:
            cid = get_correlation_id()
            logger.info(
                "request archive miss",
                extra={
                    "extra_fields": {
                        "event": "request.archive_miss",
                        "lookup_correlation_id": correlation_id,
                    }
                },
            )
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "type": "not_found",
                        "message": f"no archived request for correlation_id={correlation_id}",
                        "correlation_id": cid,
                    }
                },
            )
        return JSONResponse(status_code=200, content=record.to_dict())

    @app.post("/admin/dependency-mode")
    async def set_dependency_mode(body: DependencyModeIn) -> dict:
        dependency.set_mode(body.mode)
        logger.info(
            "dependency mode changed",
            extra={"extra_fields": {"event": "dependency.mode_changed", "mode": body.mode}},
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
