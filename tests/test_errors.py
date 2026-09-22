"""异常归一化：四类错误映射到可区分类型与状态码，响应不泄露内部细节。"""
from __future__ import annotations

import logging

from app.dependencies import dependency
from app.errors import ERROR_STATUS_MAP, ErrorType

logger = logging.getLogger("test.errors")


async def test_business_rejected_maps_to_422(client):
    logger.info("输入: key='BAD_KEY'（含大写/下划线）| 判定依据: 业务拒绝 -> %s/%s",
                ErrorType.BUSINESS_REJECTED, ERROR_STATUS_MAP[ErrorType.BUSINESS_REJECTED])
    resp = await client.get("/items/BAD_KEY")
    body = resp.json()
    logger.info("输出: status=%s body=%s", resp.status_code, body)
    assert resp.status_code == 422
    assert body["error"]["type"] == "business_rejected"
    # 不得泄露内部细节（detail 中的正则等）
    assert "detail" not in body["error"]
    assert "must match" not in str(body)


async def test_dependency_failure_maps_to_502(client):
    dependency.set_mode("down")
    logger.info("输入: 依赖状态=down, key='abc' | 判定依据: 依赖异常 -> %s/%s",
                ErrorType.DEPENDENCY_FAILURE, ERROR_STATUS_MAP[ErrorType.DEPENDENCY_FAILURE])
    resp = await client.get("/items/abc")
    body = resp.json()
    logger.info("输出: status=%s body=%s", resp.status_code, body)
    assert resp.status_code == 502
    assert body["error"]["type"] == "dependency_failure"
    assert "simulated downstream" not in str(body), "内部细节泄露"


async def test_dependency_timeout_maps_to_504(client):
    dependency.set_mode("down")
    logger.info("输入: 依赖状态=down, key='x-timeout' | 判定依据: 依赖超时 -> %s/%s",
                ErrorType.DEPENDENCY_TIMEOUT, ERROR_STATUS_MAP[ErrorType.DEPENDENCY_TIMEOUT])
    resp = await client.get("/items/x-timeout")
    body = resp.json()
    logger.info("输出: status=%s body=%s", resp.status_code, body)
    assert resp.status_code == 504
    assert body["error"]["type"] == "dependency_timeout"


async def test_unexpected_error_maps_to_500_without_leak(client):
    logger.info("输入: GET /admin/boom（触发 RuntimeError）| 判定依据: 未预期异常 -> %s/%s 且不泄露内部信息",
                ErrorType.INTERNAL_ERROR, ERROR_STATUS_MAP[ErrorType.INTERNAL_ERROR])
    resp = await client.get("/admin/boom")
    body = resp.json()
    logger.info("输出: status=%s body=%s", resp.status_code, body)
    assert resp.status_code == 500
    assert body["error"]["type"] == "internal_error"
    assert "simulated crash" not in str(body), "原始异常信息泄露"
    assert "RuntimeError" not in str(body)


async def test_error_response_contains_request_id(client):
    logger.info("输入: 带标识的错误请求 | 判定依据: 错误响应须带 request_id 便于关联诊断")
    resp = await client.get("/items/BAD_KEY", headers={"X-Request-ID": "deadbeef12345678"})
    body = resp.json()
    logger.info("输出: error.request_id=%s", body["error"]["request_id"])
    assert body["error"]["request_id"] == "deadbeef12345678"


async def test_original_error_preserved_in_logs(client, caplog):
    logger.info("输入: GET /admin/boom | 判定依据: 日志须保留原始错误 detail 供诊断")
    with caplog.at_level(logging.ERROR, logger="app.middleware"):
        await client.get("/admin/boom")
    error_records = [r for r in caplog.records if getattr(r, "stage", None) == "error"]
    assert error_records, "缺少错误阶段日志"
    detail = getattr(error_records[0], "error_detail", "")
    logger.info("输出: 日志 error_detail=%s", detail)
    assert "RuntimeError" in detail and "simulated crash" in detail
