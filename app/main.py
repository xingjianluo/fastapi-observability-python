"""FastAPI 应用装配：路由、中间件、异常处理器、健康检查与指标端点。"""
from __future__ import annotations

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.context import get_correlation_id
from app.dependencies import VALID_MODES, dependency
from app.errors import AppError, normalize_exception, to_error_payload
from app.logging_config import configure_logging, get_logger
from app.metrics import registry
from app.middleware import CorrelationIdMiddleware, MetricsMiddleware

logger = get_logger("app.request")


class DependencyModeIn(BaseModel):
    mode: str = Field(pattern=f"^({'|'.join(VALID_MODES)})$")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    dependency.set_mode(settings.dependency_mode)

    app = FastAPI(title=settings.service_name)
    # 注意：后 add 的中间件先执行，Metrics 在内层保证拿到路由模板
    app.add_middleware(MetricsMiddleware)
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
            content={"status": status, "dependency": dep_health},
        )

    @app.get("/metrics")
    async def metrics() -> dict:
        return registry.snapshot()

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
