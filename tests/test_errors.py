"""异常归一化：业务拒绝 / 依赖超时 / 依赖异常 / 未预期异常的映射与防泄露。"""
from __future__ import annotations

import logging

from app.dependencies import dependency
from app.main import app


async def test_business_rejected_maps_to_422(client):
    print("[输入] GET /items/-1（item_id 非正数，触发业务规则拒绝）")
    resp = await client.get("/items/-1")
    body = resp.json()
    print(f"[判定依据] 状态码=422 且 error.type=business_rejected，实际: {resp.status_code} {body}")
    assert resp.status_code == 422
    assert body["error"]["type"] == "business_rejected"
    assert "must be positive" in body["error"]["message"]
    assert body["error"]["correlation_id"]


async def test_dependency_timeout_maps_to_504(client):
    dependency.set_mode("timeout")
    print("[输入] 依赖模式=timeout，GET /items/1（下游耗时超过超时预算）")
    resp = await client.get("/items/1")
    body = resp.json()
    print(f"[判定依据] 状态码=504 且 error.type=dependency_timeout，实际: {resp.status_code} {body}")
    assert resp.status_code == 504
    assert body["error"]["type"] == "dependency_timeout"


async def test_dependency_error_maps_to_502(client):
    dependency.set_mode("error")
    print("[输入] 依赖模式=error，GET /items/1（下游返回 5xx）")
    resp = await client.get("/items/1")
    body = resp.json()
    print(f"[判定依据] 状态码=502 且 error.type=dependency_error，实际: {resp.status_code} {body}")
    assert resp.status_code == 502
    assert body["error"]["type"] == "dependency_error"
    print("[判定依据] 响应体不得包含模拟下游内部细节 'simulated downstream returned HTTP 500'")
    assert "simulated downstream" not in str(body)


async def test_unexpected_error_maps_to_500_without_leak(client, caplog):
    secret = "db-password-s3cret"

    @app.get("/test-only-boom")
    async def boom() -> None:
        raise ValueError(f"connection failed, password={secret}")

    print(f"[输入] GET /test-only-boom（抛出含敏感信息 {secret!r} 的 ValueError）")
    with caplog.at_level(logging.ERROR, logger="app.request"):
        resp = await client.get("/test-only-boom")
    body = resp.json()
    print(f"[判定依据] 状态码=500、error.type=unexpected_error、响应不含敏感信息，实际: {resp.status_code} {body}")
    assert resp.status_code == 500
    assert body["error"]["type"] == "unexpected_error"
    assert secret not in str(body)
    print("[判定依据] 服务端日志保留原始异常（exc_info 含 ValueError）供诊断")
    errors = [r for r in caplog.records if r.exc_info and r.exc_info[0] is ValueError]
    assert errors, "服务端日志应保留原始 ValueError"


async def test_error_response_carries_correlation_id(client):
    cid = "err-corr-id-00001"
    print(f"[输入] 携带标识 {cid!r} 请求触发业务拒绝")
    resp = await client.get("/items/-5", headers={"X-Correlation-ID": cid})
    print(f"[判定依据] 错误响应中的 correlation_id 必须等于 {cid!r}，便于按标识串联日志")
    assert resp.json()["error"]["correlation_id"] == cid
