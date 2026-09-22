"""请求标识：合法沿用、非法/缺失替换、并发隔离、后台任务传播。"""
from __future__ import annotations

import asyncio
import logging
import uuid

import pytest

from app.config import settings
from app.routes import background_observations

logger = logging.getLogger("test.request_id")


async def test_valid_request_id_is_propagated(client):
    rid = "abcdef0123456789"
    logger.info("输入: header X-Request-ID=%s | 判定依据: 合法标识须原样沿用", rid)
    resp = await client.get("/items/abc", headers={"X-Request-ID": rid})
    body = resp.json()
    logger.info("输出: status=%s body.request_id=%s resp.header=%s",
                resp.status_code, body["request_id"], resp.headers.get("x-request-id"))
    assert resp.status_code == 200
    assert body["request_id"] == rid
    assert resp.headers["x-request-id"] == rid


async def test_missing_request_id_generates_one(client):
    logger.info("输入: 无 X-Request-ID | 判定依据: 须生成合法新标识并返回")
    resp = await client.get("/items/abc")
    rid = resp.headers["x-request-id"]
    logger.info("输出: 生成标识=%s", rid)
    assert settings.request_id_pattern.match(rid)
    assert resp.json()["request_id"] == rid


@pytest.mark.parametrize("bad", ["", "!!!", "a b c", "zzzzzzzz", "x" * 65])
async def test_invalid_request_id_is_replaced(client, bad):
    logger.info("输入: 非法标识=%r | 判定依据: 不匹配 %s 须替换为新生成标识",
                bad, settings.request_id_pattern.pattern)
    resp = await client.get("/items/abc", headers={"X-Request-ID": bad})
    rid = resp.headers["x-request-id"]
    logger.info("输出: 替换后标识=%s", rid)
    assert rid != bad
    assert settings.request_id_pattern.match(rid)


async def test_concurrent_requests_ids_are_isolated(client):
    n = 32
    rids = [uuid.uuid4().hex for _ in range(n)]
    logger.info("输入: %d 个并发请求各带独立标识 | 判定依据: 每个响应须带回自己的标识，不得串号", n)

    async def call(rid: str) -> tuple[str, str, str]:
        resp = await client.get("/items/abc", headers={"X-Request-ID": rid})
        return rid, resp.json()["request_id"], resp.headers["x-request-id"]

    results = await asyncio.gather(*(call(r) for r in rids))
    for sent, body_rid, header_rid in results:
        logger.info("判定: sent=%s body=%s header=%s", sent, body_rid, header_rid)
        assert body_rid == sent and header_rid == sent
    # 后台任务观察到的标识也必须与请求一一对应
    observed = {o["request_id"] for o in background_observations}
    logger.info("后台任务观察集合与发送集合一致: %s", observed == set(rids))
    assert observed == set(rids)


async def test_background_task_keeps_request_id(client):
    rid = uuid.uuid4().hex
    logger.info("输入: rid=%s | 判定依据: 后台任务中读取到的标识须与请求一致", rid)
    await client.get("/items/bg1", headers={"X-Request-ID": rid})
    await asyncio.sleep(0)  # 让后台任务执行
    assert background_observations, "后台任务未执行"
    observed = background_observations[-1]
    logger.info("输出: 后台任务观察到 request_id=%s", observed["request_id"])
    assert observed["request_id"] == rid


async def test_no_context_leak_between_requests(client):
    logger.info("输入: 连续两请求，第一个带标识第二个不带 | 判定依据: 第二个不得残留第一个的标识")
    first = uuid.uuid4().hex
    await client.get("/items/abc", headers={"X-Request-ID": first})
    second = await client.get("/items/abc")
    second_rid = second.json()["request_id"]
    logger.info("输出: first=%s second=%s", first, second_rid)
    assert second_rid != first
