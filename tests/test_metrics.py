"""指标：并发下计数不丢失不重复、按路由与状态码分组、耗时被记录。"""
from __future__ import annotations

import asyncio

from app.dependencies import dependency
from app.metrics import registry


def _find(snapshot: dict, method: str, route: str, status: int) -> dict | None:
    for r in snapshot["requests"]:
        if (r["method"], r["route"], r["status_code"]) == (method, route, status):
            return r
    return None


async def test_concurrent_requests_counted_exactly_once(client):
    n = 50
    print(f"[输入] 并发发送 {n} 个 GET /items/<id> 请求（依赖模式=ok）")
    await asyncio.gather(*(client.get(f"/items/{i + 1}") for i in range(n)))
    snap = registry.snapshot()
    row = _find(snap, "GET", "/items/{item_id}", 200)
    print(f"[判定依据] /items 200 计数必须恰好等于 {n}（并发下不丢失、不重复），实际: {row}")
    assert row is not None
    assert row["count"] == n
    print(f"[判定依据] 耗时统计完整：total={row['latency_ms']['total']}ms 应 >= max={row['latency_ms']['max']}ms 且 avg>0")
    assert row["latency_ms"]["total"] >= row["latency_ms"]["max"] > 0
    assert row["latency_ms"]["avg"] > 0


async def test_metrics_grouped_by_status_code(client):
    dependency.set_mode("error")
    print("[输入] 依赖模式=error 下请求 3 次 /items（->502），再请求 2 次 /items/-1（->422）")
    for _ in range(3):
        await client.get("/items/1")
    dependency.set_mode("ok")
    for _ in range(2):
        await client.get("/items/-1")
    snap = registry.snapshot()
    row_502 = _find(snap, "GET", "/items/{item_id}", 502)
    row_422 = _find(snap, "GET", "/items/{item_id}", 422)
    print(f"[判定依据] 同一路由按状态码分组计数：502 应有 3 次、422 应有 2 次，实际: {row_502} / {row_422}")
    assert row_502["count"] == 3
    assert row_422["count"] == 2
    assert snap["total"] == 5


async def test_metrics_endpoint_returns_snapshot(client):
    print("[输入] 请求 2 次 /items/1 后调用 GET /metrics")
    await client.get("/items/1")
    await client.get("/items/1")
    resp = await client.get("/metrics")
    body = resp.json()
    row = _find(body, "GET", "/items/{item_id}", 200)
    print(f"[判定依据] /metrics 快照中 /items 200 计数=2，实际: {row}")
    assert resp.status_code == 200
    assert row["count"] == 2


async def test_health_reflects_dependency_state(client):
    print("[输入] 依次切换依赖模式 ok -> timeout -> error，每步查询 /health")
    cases = [("ok", 200, "ok"), ("timeout", 200, "degraded"), ("error", 503, "unavailable")]
    for mode, expected_code, expected_status in cases:
        dependency.set_mode(mode)
        resp = await client.get("/health")
        print(f"[判定依据] 模式={mode}: 期望 HTTP {expected_code} / status={expected_status}，实际 {resp.status_code} / {resp.json()['status']}")
        assert resp.status_code == expected_code
        assert resp.json()["status"] == expected_status
