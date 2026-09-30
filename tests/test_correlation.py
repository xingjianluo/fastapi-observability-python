"""关联标识：并发隔离、非法标识处理、异步链与后台任务中的传播。"""

from __future__ import annotations

import asyncio
import re

from app.config import get_settings

VALID_PATTERN = re.compile(get_settings().correlation_id_pattern)


async def test_valid_correlation_id_is_propagated(client):
    cid = "order-abc-12345678"
    print(f"[输入] 请求头 X-Correlation-ID={cid!r}（符合 {VALID_PATTERN.pattern}）")
    resp = await client.get("/items/1", headers={"X-Correlation-ID": cid})
    print(
        "[判定依据] 状态码=200 且响应体与响应头中的 correlation_id 均等于输入值 -> 标识被原样沿用"
    )
    assert resp.status_code == 200
    assert resp.json()["correlation_id"] == cid
    assert resp.headers["X-Correlation-ID"] == cid
    assert "X-Correlation-ID-Generated" not in resp.headers


async def test_invalid_correlation_id_is_replaced(client):
    bad = "!!not valid!!"
    print(
        f"[输入] 请求头 X-Correlation-ID={bad!r}（含非法字符，不匹配 {VALID_PATTERN.pattern}）"
    )
    resp = await client.get("/items/1", headers={"X-Correlation-ID": bad})
    new_cid = resp.json()["correlation_id"]
    print(
        f"[判定依据] 返回新标识 {new_cid!r}：不等于非法输入、符合合法格式、且响应头标注 Generated=true"
    )
    assert resp.status_code == 200
    assert new_cid != bad
    assert VALID_PATTERN.fullmatch(new_cid)
    assert resp.headers["X-Correlation-ID-Generated"] == "true"


async def test_missing_correlation_id_is_generated(client):
    print("[输入] 不提供 X-Correlation-ID 请求头")
    resp = await client.get("/items/1")
    new_cid = resp.json()["correlation_id"]
    print(f"[判定依据] 服务端生成合法标识 {new_cid!r} 并标注 Generated=true")
    assert VALID_PATTERN.fullmatch(new_cid)
    assert resp.headers["X-Correlation-ID-Generated"] == "true"


async def test_concurrent_requests_keep_ids_isolated(client):
    cids = [f"concurrent-cid-{i:04d}" for i in range(20)]
    print(f"[输入] 并发发送 {len(cids)} 个请求，各自携带不同合法标识: {cids[:3]} ...")

    async def call(cid: str, item_id: int) -> tuple[str, str]:
        resp = await client.get(f"/items/{item_id}", headers={"X-Correlation-ID": cid})
        return cid, resp.json()["correlation_id"]

    results = await asyncio.gather(*(call(cid, i + 1) for i, cid in enumerate(cids)))
    print(
        "[判定依据] 每个请求响应体中的标识必须等于其自身输入标识（逐一比对，串号即失败）"
    )
    for sent, got in results:
        assert got == sent, f"串号: 发送 {sent} 收到 {got}"


async def test_background_task_keeps_correlation_id(client, caplog):
    import logging

    cid = "bg-task-cid-0001"
    print(f"[输入] 携带标识 {cid!r} 请求 /items/1（触发后台审计任务）")
    with caplog.at_level(logging.INFO, logger="app.request"):
        resp = await client.get("/items/1", headers={"X-Correlation-ID": cid})
    assert resp.status_code == 200
    audit = [
        r
        for r in caplog.records
        if getattr(r, "extra_fields", {}).get("event") == "item.audit"
    ]
    got = [r.extra_fields["correlation_id"] for r in audit]
    print(
        f"[判定依据] 后台任务日志 item.audit 恰有一条，且其 correlation_id={got} 必须等于 {cid!r}"
    )
    assert len(audit) == 1
    assert got[0] == cid


async def test_no_context_residue_between_requests(client):
    print("[输入] 先携带标识请求一次，再不带标识请求一次")
    first = await client.get(
        "/items/1", headers={"X-Correlation-ID": "residue-check-01"}
    )
    second = await client.get("/items/1")
    cid1, cid2 = first.json()["correlation_id"], second.json()["correlation_id"]
    print(
        f"[判定依据] 第二次请求必须获得全新标识（{cid2!r} != {cid1!r}），无上一请求残留"
    )
    assert cid2 != cid1
    assert VALID_PATTERN.fullmatch(cid2)
