"""熔断/降级/快速失败：连续失败、窗口占比、半开探测、快速失败口径与标识一致、恢复。

每个用例打印输入与判定依据（-s 可见）。
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from app.dependencies import dependency
from app.errors import DependencyError, DependencyTimeout
from app.resilience import CircuitBreaker, breaker


class FakeClock:
    """可手动推进的单调时钟，用于确定性地测试窗口与冷却。"""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


# ----------------------- 熔断器单元（时钟注入，确定性）-----------------------


def test_consecutive_failures_open_and_fast_fail():
    clock = FakeClock()
    cb = CircuitBreaker(
        failure_threshold=3, min_samples=100, open_cooldown_s=5.0, now=clock
    )
    print("[输入] 连续记录 3 次 dependency_error（阈值=3）")
    cb.record_failure("dependency_error")
    cb.record_failure("dependency_error")
    assert cb.snapshot()["state"] == "closed"
    cb.record_failure("dependency_error")
    snap = cb.snapshot()
    print(
        f"[判定依据] 第 3 次后应 open，实际 state={snap['state']} consecutive={snap['consecutive_failures']}"
    )
    assert snap["state"] == "open"
    with pytest.raises(DependencyError) as exc:
        cb.before_call()
    print(
        f"[判定依据] open 态 before_call 立即快速失败：status={exc.value.status_code} fast_failed={exc.value.fast_failed}"
    )
    assert exc.value.status_code == 502
    assert exc.value.fast_failed is True


def test_timeout_trip_fails_fast_as_504():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=1, min_samples=100, now=clock)
    print("[输入] 1 次 dependency_timeout 即熔断")
    cb.record_failure("dependency_timeout")
    with pytest.raises(DependencyTimeout) as exc:
        cb.before_call()
    print(
        f"[判定依据] 快速失败必须沿用 504/dependency_timeout 口径，实际 {exc.value.status_code}/{exc.value.error_type}"
    )
    assert exc.value.status_code == 504
    assert exc.value.fast_failed is True


def test_window_failure_ratio_trips_without_consecutive_run():
    clock = FakeClock()
    # 连续失败阈值很高不会触发；靠窗口占比：4 个样本里 3 失败（0.75 >= 0.5）
    cb = CircuitBreaker(
        failure_threshold=999, window_s=30, min_samples=4, failure_ratio=0.5, now=clock
    )
    print("[输入] 失败/成功/失败/失败（非连续），窗口 4 个样本失败占比 0.75")
    cb.record_failure("dependency_error")
    cb.record_success()
    cb.record_failure("dependency_error")
    assert cb.snapshot()["state"] == "closed", "样本不足 min_samples 时不应熔断"
    cb.record_failure("dependency_error")
    print(
        f"[判定依据] 样本达 4 且占比 0.75>=0.5 应 open，实际 {cb.snapshot()['state']}"
    )
    assert cb.snapshot()["state"] == "open"


def test_failures_slide_out_of_window():
    clock = FakeClock()
    cb = CircuitBreaker(
        failure_threshold=999, window_s=30, min_samples=4, failure_ratio=0.75, now=clock
    )
    print("[输入] 记录 4 次失败后时间推进 31s（超出窗口）")
    for _ in range(4):
        cb.record_failure("dependency_error")
    assert cb.snapshot()["state"] == "open"
    clock.advance(31)
    snap = cb.snapshot()
    print(
        f"[判定依据] 窗口样本应滑出为 0（实际 samples={snap['window_samples']}），"
        f"open 态不会自动解除、仍需冷却探测（state={snap['state']}）"
    )
    assert snap["window_samples"] == 0
    assert snap["state"] == "open"


def test_half_open_probe_success_closes():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=1, open_cooldown_s=5.0, now=clock)
    cb.record_failure("dependency_error")
    assert cb.snapshot()["state"] == "open"
    print(
        "[输入] open 后推进 5s 冷却，下一次 before_call 应进入 half_open 放行单个探测"
    )
    clock.advance(5)
    cb.before_call()
    assert cb.snapshot()["state"] == "half_open"
    with pytest.raises(DependencyError):
        cb.before_call()  # 探测未返回期间，其他请求仍快速失败（不抖动地只有一个探测）
    print("[判定依据] half_open 期间仅放行 1 个探测，其余请求继续快速失败")
    cb.record_success()
    print(f"[判定依据] 探测成功 -> closed，实际 {cb.snapshot()['state']}")
    assert cb.snapshot()["state"] == "closed"
    cb.before_call()  # 不再抛


def test_half_open_probe_failure_reopens():
    clock = FakeClock()
    cb = CircuitBreaker(failure_threshold=1, open_cooldown_s=5.0, now=clock)
    cb.record_failure("dependency_error")
    clock.advance(5)
    cb.before_call()  # half_open 探测放行
    cb.record_failure("dependency_error")
    snap = cb.snapshot()
    print(
        f"[判定依据] 探测失败应立即回 open 并重新计时（cooldown_remaining={snap['cooldown_remaining_s']}）"
    )
    assert snap["state"] == "open"
    assert snap["cooldown_remaining_s"] == 5.0


# ----------------------------- HTTP 端到端 -----------------------------


async def test_http_consecutive_failures_then_fast_fail_consistent(client):
    breaker.failure_threshold = 3
    breaker.open_cooldown_s = 30
    breaker.reset()
    dependency.set_mode("error")
    print("[输入] 依赖 error 模式连续请求 3 次（真实 502），第 4 次应快速失败")
    for i in range(3):
        r = await client.get(
            "/items/1", headers={"X-Correlation-ID": f"e2e-real-{i:03d}"}
        )
        assert r.status_code == 502
        assert r.json()["error"]["type"] == "dependency_error"
    t0 = time.perf_counter()
    cid = "e2e-fastfail-1"
    r = await client.get("/items/1", headers={"X-Correlation-ID": cid})
    elapsed_ms = (time.perf_counter() - t0) * 1000
    body = r.json()
    rec_resp = await client.get(f"/observability/requests/{cid}")
    rec = rec_resp.json()
    print(
        f"[判定依据] 第4次: HTTP {r.status_code}/{body['error']['type']} 耗时 {elapsed_ms:.1f}ms（应远小于超时预算200ms）"
    )
    print(
        f"[判定依据] 响应头={r.headers['X-Correlation-ID']} 响应体={body['error']['correlation_id']} 档案={rec['correlation_id']} 必须同一值；"
        f"档案 fast_failed={rec['fast_failed']}"
    )
    assert r.status_code == 502
    assert body["error"]["type"] == "dependency_error"
    assert elapsed_ms < 100
    assert r.headers["X-Correlation-ID"] == cid
    assert body["error"]["correlation_id"] == cid
    assert rec["correlation_id"] == cid
    assert rec["status_code"] == 502
    assert rec["error_type"] == "dependency_error"
    assert rec["fast_failed"] is True
    assert "simulated downstream" not in str(body)
    dependency.set_mode("ok")


async def test_http_fast_fail_id_consistent_in_logs(client, caplog):
    breaker.failure_threshold = 1
    breaker.open_cooldown_s = 30
    breaker.reset()
    dependency.set_mode("error")
    print("[输入] 1 次真实失败即熔断，随后携带固定标识快速失败，抓日志比对标识")
    await client.get("/items/1", headers={"X-Correlation-ID": "warmup-000001"})
    cid = "ff-log-cid-001"
    with caplog.at_level(logging.WARNING, logger="app.request"):
        r = await client.get("/items/1", headers={"X-Correlation-ID": cid})
    failed_logs = [
        x
        for x in caplog.records
        if getattr(x, "extra_fields", {}).get("event") == "request.failed"
    ]
    log_cids = {x.extra_fields["correlation_id"] for x in failed_logs}
    print(
        f"[判定依据] request.failed 日志 correlation_id 集合={log_cids} 应包含 {cid!r}，"
        f"且 fast_failed=True；响应头/体/档案同一标识"
    )
    assert r.status_code == 502
    assert cid in log_cids
    assert any(x.extra_fields.get("fast_failed") for x in failed_logs)
    assert r.headers["X-Correlation-ID"] == cid
    rec = await client.get(f"/observability/requests/{cid}")
    assert rec.json()["correlation_id"] == cid
    dependency.set_mode("ok")


async def test_health_degrades_on_open_and_recovers(client):
    breaker.failure_threshold = 2
    breaker.open_cooldown_s = 0.3
    breaker.reset()
    dependency.set_mode("error")
    print("[输入] 连续 2 次失败后查 /health，再恢复依赖等冷却后探测")
    await client.get("/items/1", headers={"X-Correlation-ID": "health-open-1"})
    await client.get("/items/1", headers={"X-Correlation-ID": "health-open-2"})
    h = await client.get("/health")
    print(
        f"[判定依据] open 期间 /health 应 503/unavailable，实际 {h.status_code}/{h.json()['status']}，"
        f"circuit_detail.state={h.json()['circuit_detail']['state']}"
    )
    assert h.status_code == 503
    assert h.json()["status"] == "unavailable"
    assert h.json()["circuit_detail"]["state"] == "open"
    # 冷却期内快速失败
    r = await client.get("/items/1", headers={"X-Correlation-ID": "health-ff-001"})
    assert r.status_code == 502
    # 恢复依赖并等待冷却，探测成功
    dependency.set_mode("ok")
    await asyncio.sleep(0.35)
    probe = await client.get("/items/1", headers={"X-Correlation-ID": "health-probe-1"})
    assert probe.status_code == 200
    h2 = await client.get("/health")
    print(
        f"[判定依据] 探测成功恢复后 /health 应 200/ok/closed，实际 {h2.status_code}/{h2.json()['status']}/"
        f"{h2.json()['circuit_detail']['state']}"
    )
    assert h2.status_code == 200
    assert h2.json()["status"] == "ok"
    assert h2.json()["circuit_detail"]["state"] == "closed"


async def test_concurrent_requests_stable_while_open(client):
    breaker.failure_threshold = 3
    breaker.open_cooldown_s = 30
    breaker.reset()
    dependency.set_mode("error")
    print("[输入] 先打 3 次触发熔断，再并发 20 个请求")
    for i in range(3):
        await client.get("/items/1", headers={"X-Correlation-ID": f"warm-{i:03d}"})
    cids = [f"ff-burst-{i:04d}" for i in range(20)]
    rs = await asyncio.gather(
        *(client.get("/items/1", headers={"X-Correlation-ID": c}) for c in cids)
    )
    pairs = {(r.status_code, r.json()["error"]["type"]) for r in rs}
    print(
        f"[判定依据] 冷却期并发 20 个结果必须全部相同 {pairs}（不来回抖动），且各自标识一致"
    )
    assert pairs == {(502, "dependency_error")}
    for cid, r in zip(cids, rs):
        assert r.headers["X-Correlation-ID"] == cid
        assert r.json()["error"]["correlation_id"] == cid
    # 每个快速失败请求也恰好留档
    got = await asyncio.gather(
        *(client.get(f"/observability/requests/{c}") for c in cids)
    )
    assert all(g.status_code == 200 and g.json()["fast_failed"] for g in got)
    print("[判定依据] 20 个快速失败请求档案全部可查且 fast_failed=true")
    dependency.set_mode("ok")


async def test_business_rejection_does_not_trip_breaker(client):
    breaker.failure_threshold = 2
    breaker.open_cooldown_s = 30
    breaker.reset()
    dependency.set_mode("ok")
    print("[输入] 连续 5 次业务拒绝（/items/-1），不应计入依赖失败、不应熔断")
    for i in range(5):
        r = await client.get("/items/-1", headers={"X-Correlation-ID": f"biz-{i:04d}"})
        assert r.status_code == 422
    snap = breaker.snapshot()
    print(
        f"[判定依据] 熔断器保持 closed、窗口无依赖失败样本，实际 state={snap['state']} samples={snap['window_samples']}"
    )
    assert snap["state"] == "closed"
    assert snap["window_samples"] == 0
    r = await client.get("/items/1", headers={"X-Correlation-ID": "biz-then-ok-1"})
    assert r.status_code == 200


async def test_breaker_configure_applies_threshold():
    print("[输入] configure(failure_threshold=1) 后记录 1 次失败")
    breaker.configure(failure_threshold=1, min_samples=100, open_cooldown_s=30)
    try:
        breaker.record_failure("dependency_error")
        state = breaker.snapshot()["state"]
        print(f"[判定依据] 1 次失败即应 open（配置阈值生效），实际 state={state}")
        assert state == "open"
    finally:
        # 恢复默认阈值，避免污染其他用例
        from app.config import get_settings

        s = get_settings()
        breaker.configure(
            failure_threshold=s.cb_failure_threshold,
            window_s=s.cb_window_s,
            min_samples=s.cb_min_samples,
            failure_ratio=s.cb_failure_ratio,
            open_cooldown_s=s.cb_open_cooldown_s,
        )
