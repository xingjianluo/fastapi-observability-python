"""降级快速失败口径专项：快速返回与真实失败的分类/状态码必须一致。

覆盖业务：
- 持续超时打穿降级 -> 快速失败仍是 dependency_timeout/504；
- 持续出错打穿降级 -> 快速失败仍是 dependency_error/502；
- 连续失败触发与窗口失败率触发两种降级路径都沿用真实失败类别，不固定成某一种；
- 同一次快速失败：响应头/响应体/结构化日志/回查档案的标识、分类、状态码完全一致；
- 并发打到已降级依赖时口径稳定，不出现一会儿 504 一会儿 502；
- 冷却后半开试探：非试探请求、试探失败、重新快速失败口径都不跳变；
  依赖真正恢复后正常流量回到 200 的既有表现；
- 快速失败不泄露 CircuitOpen、下游内部原因等细节。
"""
from __future__ import annotations

import asyncio
import logging
import time

import pytest

from app.context import get_correlation_id
from app.dependencies import dependency
from app.resilience import (
    FAILURE_ERROR,
    FAILURE_TIMEOUT,
    CircuitBreaker,
    breaker,
)

#: 两类业务故障的既有口径：真实失败与快速失败必须共用同一套
FAILURE_CASES = {
    "timeout": {"mode": "timeout", "status": 504, "error_type": "dependency_timeout"},
    "error": {"mode": "error", "status": 502, "error_type": "dependency_error"},
}


class FailedRequestLogCapture(logging.Handler):
    """捕获 request.failed 结构化日志在请求上下文内的标识/分类/状态码。"""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict) and extra.get("event") == "request.failed":
            self.records.append(
                {
                    "correlation_id": get_correlation_id(),
                    "error_type": extra.get("error_type"),
                    "status_code": extra.get("status_code"),
                }
            )


def _attach_capture() -> FailedRequestLogCapture:
    handler = FailedRequestLogCapture()
    logging.getLogger().addHandler(handler)
    return handler


async def _assert_fast_fail_consistent(
    client, cid: str, expected_status: int, expected_type: str, logs: list[dict]
) -> dict:
    """发一次快速失败请求，断言响应头/体/日志/档案四处标识+分类+状态码完全一致。"""
    t0 = time.perf_counter()
    resp = await client.get("/items/1", headers={"X-Correlation-ID": cid})
    elapsed_ms = (time.perf_counter() - t0) * 1000
    body = resp.json()
    lookup = await client.get(f"/observability/requests/{cid}")
    assert lookup.status_code == 200
    rec = lookup.json()

    assert elapsed_ms < 50, f"快速失败应毫秒级返回，实际 {elapsed_ms:.1f}ms"
    # 响应状态码与错误分类
    assert resp.status_code == expected_status
    assert body["error"]["type"] == expected_type
    # 四处标识是同一个
    assert resp.headers["X-Correlation-ID"] == cid
    assert body["error"]["correlation_id"] == cid
    assert rec["correlation_id"] == cid
    # 档案的状态码/归类/错误分类与对外响应同一套
    assert rec["status_code"] == expected_status
    assert rec["outcome"] == expected_type
    assert rec["error_type"] == expected_type
    # 结构化日志同一套口径
    matched = [r for r in logs if r["correlation_id"] == cid]
    assert matched, f"请求 {cid} 缺少 request.failed 日志"
    for entry in matched:
        assert entry["error_type"] == expected_type
        assert entry["status_code"] == expected_status
    # 不泄露熔断/下游内部细节
    assert "CircuitOpen" not in str(body)
    assert "circuit" not in str(body).lower()
    assert "simulated downstream" not in str(body)
    print(
        f"[判定依据] 快速失败 {cid}：{elapsed_ms:.2f}ms 内 HTTP {resp.status_code} "
        f"{body['error']['type']!r}；响应头/响应体/日志/档案标识一致，"
        f"档案 status={rec['status_code']} outcome={rec['outcome']}"
    )
    return body


@pytest.mark.parametrize("case", ["timeout", "error"], ids=["持续超时", "持续出错"])
async def test_consecutive_failure_fast_fail_matches_real_failure(client, case):
    """业务：持续超时/持续出错打满连续失败阈值后，快速失败与真实失败同口径。"""
    spec = FAILURE_CASES[case]
    breaker.configure(fail_threshold=3, window_size=20, fail_ratio=0.5, cooldown_s=60)
    dependency.set_mode(spec["mode"])
    capture = _attach_capture()
    try:
        print(f"[业务=持续{case}] 依赖模式={spec['mode']}，连续 3 次真实失败打开熔断")
        real_bodies = []
        for i in range(3):
            cid = f"cf-{case}-real-{i:04d}"
            resp = await client.get("/items/1", headers={"X-Correlation-ID": cid})
            assert resp.status_code == spec["status"]
            body = resp.json()
            assert body["error"]["type"] == spec["error_type"]
            real_bodies.append(body)
        assert breaker.state == "open"
        assert breaker.last_failure_kind == (
            FAILURE_TIMEOUT if case == "timeout" else FAILURE_ERROR
        )
        print(f"[判定依据] 熔断 open，记录的真实失败类别={breaker.last_failure_kind!r}")

        fast_cid = f"cf-{case}-fast-0001"
        fast_body = await _assert_fast_fail_consistent(
            client, fast_cid, spec["status"], spec["error_type"], capture.records
        )
        # 快速失败前后同一类失败的响应文案也逐字一致
        assert fast_body["error"]["message"] == real_bodies[-1]["error"]["message"]
    finally:
        logging.getLogger().removeHandler(capture)
        dependency.set_mode("ok")


@pytest.mark.parametrize("case", ["timeout", "error"], ids=["窗口-持续超时", "窗口-持续出错"])
async def test_window_ratio_fast_fail_matches_real_failure(client, case):
    """业务：仅靠滑动窗口失败率（非连续阈值）触发降级时，类别同样沿用真实失败。"""
    spec = FAILURE_CASES[case]
    # 连续失败阈值设到极大，只让窗口失败率条件生效
    breaker.configure(fail_threshold=999, window_size=6, fail_ratio=0.5, cooldown_s=60)
    dependency.set_mode(spec["mode"])
    capture = _attach_capture()
    try:
        print(f"[业务=窗口失败率/{case}] 窗口=6、阈值比例=0.5，连续 6 次真实失败攒满窗口")
        for i in range(5):
            resp = await client.get(
                "/items/1", headers={"X-Correlation-ID": f"wr-{case}-{i:04d}"}
            )
            assert resp.status_code == spec["status"]
        assert breaker.state == "closed", "窗口未攒满不应降级"
        resp = await client.get("/items/1", headers={"X-Correlation-ID": f"wr-{case}-0006"})
        assert resp.status_code == spec["status"]
        assert breaker.state == "open"
        print(f"[判定依据] 窗口攒满触发 open，类别={breaker.last_failure_kind!r}（不固定为 error）")

        await _assert_fast_fail_consistent(
            client, f"wr-{case}-fast-01", spec["status"], spec["error_type"], capture.records
        )
    finally:
        logging.getLogger().removeHandler(capture)
        dependency.set_mode("ok")


@pytest.mark.parametrize("case", ["timeout", "error"], ids=["并发-持续超时", "并发-持续出错"])
async def test_fast_fail_classification_stable_under_concurrency(client, case):
    """业务：多个请求并发打到已降级依赖，快速失败口径稳定不抖动。"""
    spec = FAILURE_CASES[case]
    breaker.configure(fail_threshold=3, window_size=20, fail_ratio=0.5, cooldown_s=60)
    dependency.set_mode(spec["mode"])
    capture = _attach_capture()
    try:
        print(f"[业务=并发快速失败/{case}] 先顺序打 3 次真实失败打开熔断")
        for i in range(3):
            resp = await client.get("/items/1", headers={"X-Correlation-ID": f"cc-{case}-{i:04d}"})
            assert resp.status_code == spec["status"]
        assert breaker.state == "open"

        n = 16
        cids = [f"cc-{case}-fast-{i:04d}" for i in range(n)]
        print(f"[输入] 熔断已 open，{n} 个快速失败请求并发到达")
        resps = await asyncio.gather(
            *(client.get("/items/1", headers={"X-Correlation-ID": cid}) for cid in cids)
        )
        statuses = sorted({r.status_code for r in resps})
        types = sorted({r.json()["error"]["type"] for r in resps})
        print(f"[判定依据] {n} 个并发快速失败的状态码集合={statuses}，分类集合={types}（各必须唯一）")
        assert statuses == [spec["status"]]
        assert types == [spec["error_type"]]
        for cid in cids:
            lookup = await client.get(f"/observability/requests/{cid}")
            rec = lookup.json()
            assert rec["status_code"] == spec["status"]
            assert rec["outcome"] == spec["error_type"]
            assert rec["error_type"] == spec["error_type"]
        print("[判定依据] 每条并发快速失败的档案与响应同口径，无 500、无 504/502 混杂")
        assert all(r.status_code != 500 for r in resps)
    finally:
        logging.getLogger().removeHandler(capture)
        dependency.set_mode("ok")


@pytest.mark.parametrize("case", ["timeout", "error"], ids=["半开并发-超时", "半开并发-出错"])
async def test_half_open_probe_and_rejected_requests_keep_kind(client, case):
    """业务：冷却后半开试探——1 个真实试探 + 其余快速失败，失败时口径不跳变。"""
    spec = FAILURE_CASES[case]
    breaker.configure(fail_threshold=2, window_size=20, fail_ratio=0.5, cooldown_s=0.05)
    dependency.set_mode(spec["mode"])
    capture = _attach_capture()
    try:
        for i in range(2):
            resp = await client.get("/items/1", headers={"X-Correlation-ID": f"ho-{case}-{i:04d}"})
            assert resp.status_code == spec["status"]
        assert breaker.state == "open"
        await asyncio.sleep(0.07)  # 等过冷却
        print(f"[业务=半开试探/{case}] 冷却后 {10} 个请求并发：恰好 1 个真实试探，其余快速失败")
        cids = [f"ho-{case}-race-{i:04d}" for i in range(10)]
        resps = await asyncio.gather(
            *(client.get("/items/1", headers={"X-Correlation-ID": cid}) for cid in cids)
        )
        statuses = sorted({r.status_code for r in resps})
        print(
            f"[判定依据] 试探失败 + 半开快速失败口径一致：状态码集合={statuses}，"
            f"试探后熔断状态={breaker.state!r}，类别={breaker.last_failure_kind!r}"
        )
        assert statuses == [spec["status"]]  # 试探真实失败与快速失败同口径，无跳变
        assert breaker.state == "open"  # 试探失败重新 open
        for cid in cids:
            rec = (await client.get(f"/observability/requests/{cid}")).json()
            assert rec["status_code"] == spec["status"]
            assert rec["error_type"] == spec["error_type"]
        # 重新 open 后的下一个请求仍是快速失败、同口径
        await _assert_fast_fail_consistent(
            client, f"ho-{case}-again-01", spec["status"], spec["error_type"], capture.records
        )
    finally:
        logging.getLogger().removeHandler(capture)
        dependency.set_mode("ok")


async def test_timeout_recovery_no_junction_and_normal_traffic_restored(client):
    """业务：超时降级后冷却——冷却期 504、试探失败仍 504、恢复后正常流量回到 200。"""
    breaker.configure(fail_threshold=3, window_size=20, fail_ratio=0.5, cooldown_s=0.05)
    dependency.set_mode("timeout")
    capture = _attach_capture()
    try:
        print("[业务=超时降级恢复] 3 次真实超时打开熔断")
        for i in range(3):
            resp = await client.get("/items/1", headers={"X-Correlation-ID": f"rt-to-{i:04d}"})
            assert resp.status_code == 504
        assert breaker.state == "open"
        # 冷却期内快速失败仍是 504（不跳成 502）
        await _assert_fast_fail_consistent(
            client, "rt-ff-cooling-01", 504, "dependency_timeout", capture.records
        )
        # 依赖仍超时：冷却后试探真实失败 -> 504，重新 open，快速失败还是 504
        await asyncio.sleep(0.07)
        probe = await client.get("/items/1", headers={"X-Correlation-ID": "rt-probe-fail1"})
        print(f"[判定依据] 依赖未恢复时试探失败：HTTP {probe.status_code}，state={breaker.state}")
        assert probe.status_code == 504
        assert breaker.state == "open"
        await _assert_fast_fail_consistent(
            client, "rt-ff-after-fail", 504, "dependency_timeout", capture.records
        )
        # 依赖真正恢复：下一次冷却试探成功 -> closed，正常流量回到 200 的既有表现
        dependency.set_mode("ok")
        await asyncio.sleep(0.07)
        probe2 = await client.get("/items/1", headers={"X-Correlation-ID": "rt-probe-ok-001"})
        print(f"[判定依据] 依赖恢复后试探成功：HTTP {probe2.status_code}，state={breaker.state}")
        assert probe2.status_code == 200
        assert breaker.state == "closed"
        assert breaker.last_failure_kind is None
        after = await client.get("/items/1", headers={"X-Correlation-ID": "rt-normal-0001"})
        health = await client.get("/health")
        print(
            f"[判定依据] 恢复后正常流量 HTTP {after.status_code}，/health={health.status_code} "
            f"{health.json()['status']} circuit={health.json()['circuit']['state']}"
        )
        assert after.status_code == 200
        assert health.status_code == 200 and health.json()["status"] == "ok"
    finally:
        logging.getLogger().removeHandler(capture)
        dependency.set_mode("ok")


# ---------- 熔断器单元：类别记录与原子判定 ----------


def test_check_request_returns_fast_fail_kind_atomically():
    b = CircuitBreaker(fail_threshold=2, window_size=10, fail_ratio=0.9, cooldown_s=60)
    print("[输入] 全新熔断器：closed 下放行且无快速失败类别")
    assert b.check_request() == (True, None)
    b.record_failure(FAILURE_TIMEOUT)
    b.record_failure(FAILURE_TIMEOUT)
    assert b.state == "open"
    print(f"[判定依据] 超时打穿后 check_request -> (False, 'timeout')，snapshot={b.snapshot()}")
    assert b.check_request() == (False, FAILURE_TIMEOUT)
    assert b.snapshot()["last_failure_kind"] == "timeout"

    b.reset()
    b.record_failure(FAILURE_ERROR)
    b.record_failure(FAILURE_ERROR)
    print("[判定依据] 出错打穿后 check_request -> (False, 'error')，类别不固定")
    assert b.check_request() == (False, FAILURE_ERROR)


def test_half_open_rejected_requests_keep_opening_kind():
    """半开期间排队中的快速失败沿用打开时的类别，与下游当前模式无关。"""
    b = CircuitBreaker(fail_threshold=1, window_size=10, fail_ratio=0.9, cooldown_s=0.02)
    b.record_failure(FAILURE_TIMEOUT)
    assert b.state == "open"
    time.sleep(0.03)
    assert b.check_request() == (True, None)  # 唯一试探放行
    print("[判定依据] 半开期间其余请求快速失败，类别仍是打开时的 timeout")
    assert b.state == "half_open"
    assert b.check_request() == (False, FAILURE_TIMEOUT)


def test_failed_probe_updates_fast_fail_kind_without_junction():
    """试探失败按本次真实失败更新类别：试探是 502，后续快速失败也必须是 502。"""
    b = CircuitBreaker(fail_threshold=1, window_size=10, fail_ratio=0.9, cooldown_s=0.02)
    b.record_failure(FAILURE_TIMEOUT)  # 超时打穿 -> 504 口径
    time.sleep(0.03)
    assert b.check_request() == (True, None)  # 试探放行
    b.record_failure(FAILURE_ERROR)  # 试探时真实遇到下游出错 -> 真实结果 502
    print("[判定依据] 试探失败后重新 open，快速失败类别同步为 error（与试探真实结果一致）")
    assert b.state == "open"
    assert b.last_failure_kind == FAILURE_ERROR
    assert b.check_request() == (False, FAILURE_ERROR)


def test_record_failure_rejects_unknown_kind():
    b = CircuitBreaker(fail_threshold=5, window_size=10, fail_ratio=0.9, cooldown_s=60)
    print("[输入] record_failure 传入未知类别应抛 ValueError，避免口径缺失")
    with pytest.raises(ValueError):
        b.record_failure("not-a-kind")  # type: ignore[arg-type]
