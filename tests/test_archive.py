"""观测档案：按标识回查、字段、标识来源、容量淘汰、并发不丢不重、与指标口径隔离。

每个用例打印输入与判定依据（-s 可见）。
"""

from __future__ import annotations

import asyncio

from app.archive import RequestArchive, RequestRecord, archive
from app.dependencies import dependency
from app.metrics import registry


def _record(
    cid: str, status: int = 200, error_type: str | None = None
) -> RequestRecord:
    return RequestRecord(
        correlation_id=cid,
        id_source="provided",
        method="GET",
        route="/items/{item_id}",
        status_code=status,
        error_type=error_type,
        latency_ms=1.0,
        fast_failed=False,
        recorded_at="2026-10-01T00:00:00+00:00",
    )


async def test_success_request_is_archived_and_retrievable(client):
    cid = "archive-success-01"
    print(f"[输入] 携带标识 {cid!r} 请求 GET /items/1（成功路径）")
    resp = await client.get("/items/1", headers={"X-Correlation-ID": cid})
    got = await client.get(f"/observability/requests/{cid}")
    rec = got.json()
    print(
        f"[判定依据] 用响应头标识 {resp.headers['X-Correlation-ID']!r} 回查必须恰好命中一条，"
        f"方法/路由/状态/错误分类/耗时/来源字段齐全，实际: {rec}"
    )
    assert resp.status_code == 200
    assert got.status_code == 200
    assert rec["correlation_id"] == cid
    assert rec["method"] == "GET"
    assert rec["route"] == "/items/{item_id}"
    assert rec["status_code"] == 200
    assert rec["error_type"] is None
    assert rec["fast_failed"] is False
    assert rec["latency_ms"] > 0
    assert rec["recorded_at"]
    assert rec["id_source"] == "provided"


async def test_business_rejected_request_is_retrievable(client):
    print("[输入] GET /items/-1（业务规则拒绝 -> 422），用响应里的标识回查")
    resp = await client.get("/items/-1")
    cid = resp.headers["X-Correlation-ID"]
    got = await client.get(f"/observability/requests/{cid}")
    rec = got.json()
    print(
        f"[判定依据] 档案状态码=422、error_type=business_rejected，实际: {rec['status_code']} / {rec['error_type']}"
    )
    assert resp.status_code == 422
    assert got.status_code == 200
    assert rec["correlation_id"] == cid
    assert rec["status_code"] == 422
    assert rec["error_type"] == "business_rejected"


async def test_dependency_failure_requests_are_retrievable(client):
    print("[输入] 分别在 timeout/error 模式请求 /items，用各自响应标识回查")
    cases = [
        ("timeout", 504, "dependency_timeout", "arch-timeout-01"),
        ("error", 502, "dependency_error", "arch-error-0001"),
    ]
    for mode, status, error_type, cid in cases:
        dependency.set_mode(mode)
        resp = await client.get("/items/1", headers={"X-Correlation-ID": cid})
        got = await client.get(f"/observability/requests/{cid}")
        rec = got.json()
        print(
            f"[判定依据] 模式={mode}: 档案应 status={status} error={error_type}，"
            f"实际: {rec['status_code']} / {rec['error_type']}"
        )
        assert resp.status_code == status
        assert rec["status_code"] == status
        assert rec["error_type"] == error_type
        assert rec["correlation_id"] == cid
    dependency.set_mode("ok")


async def test_fallback_500_request_is_retrievable_with_consistent_id(client):
    from app.main import app

    @app.get("/test-only-archive-boom")
    async def boom() -> None:
        raise RuntimeError("boom secret xyz")

    cid = "arch-boom-000001"
    print(f"[输入] 携带 {cid!r} 请求一个抛未预期异常的路由（走最后兜底 -> 500）")
    resp = await client.get(
        "/test-only-archive-boom", headers={"X-Correlation-ID": cid}
    )
    body = resp.json()
    got = await client.get(f"/observability/requests/{cid}")
    rec = got.json()
    print(
        f"[判定依据] 响应头={resp.headers['X-Correlation-ID']!r}、响应体={body['error']['correlation_id']!r}、"
        f"档案={rec['correlation_id']!r} 必须为同一个标识；档案 status=500 error=unexpected_error"
    )
    assert resp.status_code == 500
    assert resp.headers["X-Correlation-ID"] == cid
    assert body["error"]["correlation_id"] == cid
    assert rec["correlation_id"] == cid
    assert rec["status_code"] == 500
    assert rec["error_type"] == "unexpected_error"
    assert "boom secret xyz" not in str(body)


async def test_generated_id_is_retrievable_and_marked(client):
    print("[输入] 提供非法标识 '!!bad!!'，由服务端补一个新标识")
    resp = await client.get("/items/1", headers={"X-Correlation-ID": "!!bad!!"})
    cid = resp.headers["X-Correlation-ID"]
    print(
        f"[判定依据] 服务端补出 {cid!r}，该标识必须能恰好查回一条且 id_source=generated"
    )
    got = await client.get(f"/observability/requests/{cid}")
    rec = got.json()
    assert got.status_code == 200
    assert rec["correlation_id"] == cid
    assert rec["id_source"] == "generated"


async def test_missing_id_is_generated_and_retrievable(client):
    print("[输入] 完全不提供标识请求 /items/1")
    resp = await client.get("/items/1")
    cid = resp.headers["X-Correlation-ID"]
    got = await client.get(f"/observability/requests/{cid}")
    print(
        f"[判定依据] 补出的 {cid!r} 可回查且 id_source=generated，实际 status={got.status_code}"
    )
    assert got.status_code == 200
    assert got.json()["id_source"] == "generated"
    assert got.json()["correlation_id"] == cid


async def test_unknown_id_returns_404(client):
    cid = "never-seen-cid-1"
    print(f"[输入] 用从未出现过（或已被淘汰）的标识 {cid!r} 回查")
    got = await client.get(f"/observability/requests/{cid}")
    body = got.json()
    print(
        f"[判定依据] 应返回 404 且 error.type=request_record_not_found，实际: {got.status_code} {body['error']['type']}"
    )
    assert got.status_code == 404
    assert body["error"]["type"] == "request_record_not_found"
    assert body["error"]["queried_correlation_id"] == cid


async def test_archive_evicts_oldest_at_capacity():
    print("[输入] 新建容量=2 的档案，依次写入 cid-1/cid-2/cid-3")
    local = RequestArchive(max_records=2)
    local.save(_record("cid-1"))
    local.save(_record("cid-2"))
    local.save(_record("cid-3"))
    print(
        f"[判定依据] 容量恒定为 2：最老 cid-1 淘汰（get 为 None），cid-2/cid-3 仍可查，size={local.size()}"
    )
    assert local.size() == 2
    assert local.get("cid-1") is None
    assert local.get("cid-2") is not None
    assert local.get("cid-3") is not None


async def test_same_id_overwrite_keeps_single_latest_record():
    print("[输入] 容量=2，同一标识 cid-x 先写 502 再写 200（模拟调用方复用标识）")
    local = RequestArchive(max_records=2)
    local.save(_record("cid-x", status=502, error_type="dependency_error"))
    local.save(_record("cid-y"))
    local.save(_record("cid-x", status=200))  # 覆盖并刷新为最新
    rec = local.get("cid-x")
    print(
        f"[判定依据] 按 cid-x 只能查回一条且为最新（status=200），实际: status={rec.status_code} error={rec.error_type}"
    )
    assert rec is not None and rec.status_code == 200 and rec.error_type is None
    print(
        "[判定依据] 容量恒定为 2：cid-x 覆盖不产生重复键；最老的是最初的 cid-x 旧值已被自身覆盖，cid-y 仍在"
    )
    assert local.size() == 2
    assert local.get("cid-y") is not None
    # 再写一条新键，淘汰当前最老 cid-y，cid-x（最新）保留
    local.save(_record("cid-z"))
    assert local.size() == 2
    assert local.get("cid-y") is None
    assert local.get("cid-x") is not None
    assert local.get("cid-z") is not None


async def test_concurrent_requests_archives_exactly_one_each(client):
    n = 60
    cids = [f"conc-archive-{i:05d}" for i in range(n)]
    print(f"[输入] 并发发送 {n} 个携带唯一标识的 GET /items 请求")

    async def one(cid: str) -> str:
        resp = await client.get("/items/1", headers={"X-Correlation-ID": cid})
        return resp.headers["X-Correlation-ID"]

    returned = await asyncio.gather(*(one(c) for c in cids))
    got = await asyncio.gather(
        *(client.get(f"/observability/requests/{cid}") for cid in cids)
    )
    found = [g.json()["correlation_id"] for g in got if g.status_code == 200]
    print(
        f"[判定依据] {n} 个真实请求必须恰好回查出 {n} 条、无丢失无重复、一一对应；"
        f"实际命中 {len(found)} 条，唯一 {len(set(found))} 条"
    )
    assert returned == cids
    assert len(found) == n
    assert len(set(found)) == n
    assert sorted(found) == sorted(cids)


async def test_eviction_does_not_change_metrics_or_health(client):
    print(
        "[输入] 档案容量调到 3，发 6 个请求触发淘汰，同时检查 /metrics 计数与 /health 不受影响"
    )
    archive.reset(max_records=3)
    for i in range(6):
        await client.get("/items/1", headers={"X-Correlation-ID": f"iso-{i:03d}"})
    snap = registry.snapshot()
    row = next(
        r
        for r in snap["requests"]
        if (r["method"], r["route"], r["status_code"])
        == ("GET", "/items/{item_id}", 200)
    )
    health = await client.get("/health")
    print(
        f"[判定依据] 档案只剩 {archive.size()} 条，但指标仍记录全部 6 次（实际 {row['count']}），"
        f"健康检查仍 200/ok（实际 {health.status_code}/{health.json()['status']}）"
    )
    assert archive.size() == 3
    assert row["count"] == 6
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
