"""指标：并发下计数不丢失不重复，按路由与状态码聚合，耗时被记录。"""
from __future__ import annotations

import asyncio
import logging

from app.metrics import registry

logger = logging.getLogger("test.metrics")


async def test_concurrent_requests_counted_exactly(client):
    n = 64
    logger.info("输入: %d 个并发成功请求 | 判定依据: /items/{{key}|200 计数须精确等于 %d", n, n)
    await asyncio.gather(*(client.get("/items/abc") for _ in range(n)))
    snap = registry.snapshot()
    count = snap["requests_total"].get("/items/{key}|200")
    logger.info("输出: 计数=%s 耗时总和=%s", count, snap["duration_seconds_sum"].get("/items/{key}|200"))
    assert count == n
    assert snap["duration_seconds_sum"]["/items/{key}|200"] > 0


async def test_mixed_status_codes_counted_by_route_and_status(client):
    ok_n, reject_n = 10, 5
    logger.info("输入: %d 个成功 + %d 个业务拒绝 | 判定依据: 两个维度分别精确计数", ok_n, reject_n)
    await asyncio.gather(
        *(client.get("/items/abc") for _ in range(ok_n)),
        *(client.get("/items/BAD") for _ in range(reject_n)),
    )
    snap = registry.snapshot()["requests_total"]
    logger.info("输出: %s", snap)
    assert snap["/items/{key}|200"] == ok_n
    assert snap["/items/{key}|422"] == reject_n


async def test_each_request_counted_once(client):
    logger.info("输入: 单个请求 | 判定依据: 中间件只记录一次，不得重复计数")
    await client.get("/items/abc")
    snap = registry.snapshot()["requests_total"]
    logger.info("输出: %s", snap)
    assert snap["/items/{key}|200"] == 1


async def test_metrics_endpoint_returns_snapshot(client):
    logger.info("输入: 3 个请求后查询 /metrics | 判定依据: 端点返回的计数与内部一致")
    for _ in range(3):
        await client.get("/items/abc")
    resp = await client.get("/metrics")
    body = resp.json()
    logger.info("输出: %s", body["requests_total"])
    assert body["requests_total"]["/items/{key}|200"] == 3
