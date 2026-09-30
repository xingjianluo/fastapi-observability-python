"""测试公共夹具：每个用例前重置指标、档案、熔断器与依赖模式，保证隔离。"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.archive import archive
from app.config import get_settings
from app.dependencies import dependency
from app.main import app
from app.metrics import registry
from app.resilience import breaker

settings = get_settings()


@pytest.fixture(autouse=True)
def clean_state():
    registry.reset()
    archive.reset(max_records=settings.archive_max_records)
    breaker.reset()
    dependency.set_mode("ok")
    yield
    registry.reset()
    archive.reset(max_records=settings.archive_max_records)
    breaker.reset()
    dependency.set_mode("ok")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
