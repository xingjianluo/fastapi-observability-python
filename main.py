"""FastAPI 应用装配。"""
from __future__ import annotations

from fastapi import FastAPI

from app.config import settings
from app.logging_config import configure_logging
from app.middleware import ObservabilityMiddleware, RequestContextMiddleware
from app.routes import router


def create_app() -> FastAPI:
    configure_logging(settings.log_level)
    app = FastAPI(title="fastapi-observability")
    app.add_middleware(ObservabilityMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(router)
    return app


app = create_app()
