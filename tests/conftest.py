"""测试公共夹具：应用客户端、指标与依赖状态复位。"""
from __future__ import annotations

import logging

import httpx
import pytest

from app.dependencies import dependency
from app.metrics import registry
from app.routes import background_observations
from main import create_app

logger = logging.getLogger("test")


@pytest.fixture(autouse=True)
def _clean_state():
    """每个用例前后复位进程内状态，避免相互污染。"""
    registry.reset()
    background_observations.clear()
    dependency.set_mode("ok")
    yield
    registry.reset()
    background_observations.clear()
    dependency.set_mode("ok")


@pytest.fixture
async def client():
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
