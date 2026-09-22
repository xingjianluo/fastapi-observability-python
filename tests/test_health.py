"""健康检查：区分 ok / degraded / down，并反映模拟依赖状态变化。"""
from __future__ import annotations

import logging

import pytest

logger = logging.getLogger("test.health")


@pytest.mark.parametrize("mode,expected", [
    ("ok", "ok"),
    ("degraded", "degraded"),
    ("down", "down"),
])
async def test_health_reflects_dependency_mode(client, mode, expected):
    logger.info("输入: 切换依赖状态为 %s | 判定依据: /health 的 status 须为 %s", mode, expected)
    resp = await client.post(f"/admin/dependency/{mode}")
    assert resp.status_code == 200
    health = (await client.get("/health")).json()
    logger.info("输出: %s", health)
    assert health["status"] == expected
    assert health["components"]["dependency"] == mode


async def test_health_transition_sequence(client):
    logger.info("输入: ok -> degraded -> down -> ok 连续切换 | 判定依据: 每次健康检查须反映最新状态")
    for mode in ("ok", "degraded", "down", "ok"):
        await client.post(f"/admin/dependency/{mode}")
        status = (await client.get("/health")).json()["status"]
        logger.info("判定: mode=%s -> status=%s", mode, status)
        assert status == mode


async def test_invalid_mode_rejected(client):
    logger.info("输入: 切换为未知状态 'broken' | 判定依据: 非法状态须被拒绝且不改变当前状态")
    resp = await client.post("/admin/dependency/broken")
    logger.info("输出: status=%s", resp.status_code)
    assert resp.status_code == 422
    health = (await client.get("/health")).json()
    assert health["status"] == "ok"
