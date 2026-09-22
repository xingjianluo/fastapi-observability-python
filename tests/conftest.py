"""测试公共夹具：每个用例前重置指标与依赖模式，保证隔离。"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies import dependency
from app.main import app
from app.metrics import registry


@pytest.fixture(autouse=True)
def clean_state():
    registry.reset()
    dependency.set_mode("ok")
    yield
    registry.reset()
    dependency.set_mode("ok")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
