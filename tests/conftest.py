"""测试公共夹具：每个用例前重置指标、档案、熔断器与依赖模式，保证隔离。"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.archive import archive
from app.dependencies import dependency
from app.main import app
from app.metrics import registry
from app.resilience import breaker


@pytest.fixture(autouse=True)
def clean_state():
    registry.reset()
    archive.reset()
    breaker.reset()
    # 测试中使用更短冷却，加快 half_open 恢复相关用例
    breaker.configure(
        fail_threshold=5, window_size=20, fail_ratio=0.5, cooldown_s=0.05
    )
    dependency.set_mode("ok")
    yield
    registry.reset()
    archive.reset()
    breaker.reset()
    dependency.set_mode("ok")


@pytest.fixture
async def client():
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
