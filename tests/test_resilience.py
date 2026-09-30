"""熔断降级：连续失败/窗口失败率触发、快速失败口径与耗时、标识一致、半开恢复与健康联动。"""
from __future__ import annotations

import asyncio
import time

from app.dependencies import dependency
from app.resilience import CircuitBreaker, breaker

# ---------- 熔断器单元：状态机 ----------

def test_consecutive_failures_open_and_fast_fail():
    b = CircuitBreaker(fail_threshold=3, window_size=10, fail_ratio=0.9, cooldown_s=60)
    print("[输入] 全新熔断器连续记录 3 次失败（阈值=3）")
    for _ in range(2):
        b.record_failure()
    assert b.state == "closed"
    b.record_failure()
    print(f"[判定依据] 第 3 次失败后 state=open，实际 {b.state!r}；open 期间请求不放行")
    assert b.state == "open"
    assert b.allow_request() is False


def test_window_fail_ratio_opens_only_after_window_fills():
    b = CircuitBreaker(fail_threshold=999, window_size=10, fail_ratio=0.5, cooldown_s=60)
    print("[输入] 连续失败阈值极大，仅靠窗口判定：窗口=10，阈值比例=0.5，交替 5 成功 5 失败")
    outcomes = [False, True, False, True, False, False, True, False, True, False]
    for ok in outcomes[:-1]:
        (b.record_success if ok else b.record_failure)()
    print(f"[判定依据] 窗口未攒满（9 次）时不应降级，state={b.state!r}")
    assert b.state == "closed"
    b.record_failure()
    print(f"[判定依据] 攒满 10 次且失败 5 次（占比 0.5）后 state=open，实际 {b.state!r}")
    assert b.state == "open"


def test_half_open_single_probe_success_closes():
    b = CircuitBreaker(fail_threshold=2, window_size=10, fail_ratio=0.9, cooldown_s=0.02)
    b.record_failure(); b.record_failure()
    assert b.state == "open"
    print("[输入] open 后冷却 0.02s，随后连续两个请求同时到达")
    time.sleep(0.03)
    first = b.allow_request()
    second = b.allow_request()
    print(f"[判定依据] 冷却后恰好放行一个试探请求：first={first} second={second}")
    assert first is True and second is False
    print("[判定依据] half_open 状态可从 snapshot 观察到")
    assert b.snapshot()["state"] == "half_open"
    b.record_success()
    print(f"[判定依据] 试探成功 -> closed 且窗口/连续计数清空，state={b.state!r}, snap={b.snapshot()}")
    assert b.state == "closed"
    assert b.snapshot()["consecutive_failures"] == 0


def test_failed_probe_reopens_and_waits_again():
    b = CircuitBreaker(fail_threshold=1, window_size=10, fail_ratio=0.9, cooldown_s=0.02)
    b.record_failure()
    assert b.state == "open"
    time.sleep(0.03)
    assert b.allow_request() is True  # half_open probe
    b.record_failure()
    print(f"[判定依据] 试探失败立即回到 open（不抖动），state={b.state!r}")
    assert b.state == "open"
    assert b.allow_request() is False  # 冷却未到，继续快速失败
    time.sleep(0.03)
    assert b.allow_request() is True  # 再次冷却后才放行新试探
    print("[判定依据] 再次冷却后才放下一个试探，open 期间不会频繁切换")


def test_fast_fail_does_not_feed_failure_window():
    b = CircuitBreaker(fail_threshold=2, window_size=10, fail_ratio=0.9, cooldown_s=60)
    b.record_failure(); b.record_failure()
    assert b.state == "open"
    for _ in range(50):
        assert b.allow_request() is False  # 快速失败路径不调用 record_failure
    snap = b.snapshot()
    print(f"[判定依据] 50 次快速失败后窗口统计仍只反映真实调用：{snap}")
    assert snap["window_size"] == 2 and snap["window_failures"] == 2


# ---------- 经 HTTP 的集成行为 ----------

async def test_consecutive_dependency_errors_trip_and_fast_fail(client):
    breaker.configure(fail_threshold=5, window_size=20, fail_ratio=0.5, cooldown_s=60)
    dependency.set_mode("error")
    print("[输入] 依赖模式=error，连续发起 6 次请求（阈值=5）")
    statuses, elapsed = [], []
    for i in range(6):
        t0 = time.perf_counter()
        resp = await client.get("/items/1", headers={"X-Correlation-ID": f"trip-err-{i:08d}"})
        elapsed.append((time.perf_counter() - t0) * 1000)
        statuses.append(resp.status_code)
    print(f"[判定依据] 6 次均为 502；前 5 次真实调用，第 6 次快速失败，状态码序列={statuses}")
    assert statuses == [502] * 6
    assert breaker.state == "open"
    print(f"[判定依据] 快速失败请求明显更快：真实调用耗时={elapsed[:5]}，快速失败={elapsed[5]:.3f}ms")
    assert elapsed[5] < max(elapsed[:5])
    dep = dependency.health()
    h = await client.get("/health")
    print(f"[判定依据] open 期间 /health -> 503 unavailable/down，实际 {h.status_code} "
          f"{h.json()['status']}/{dep} circuit={h.json()['circuit']['state']}")
    assert h.status_code == 503
    assert h.json()["status"] == "unavailable"
    dependency.set_mode("ok")


async def test_fast_fail_response_shape_and_correlation_id_consistency(client):
    breaker.configure(fail_threshold=1, window_size=20, fail_ratio=0.5, cooldown_s=60)
    dependency.set_mode("error")
    warm = await client.get("/items/1", headers={"X-Correlation-ID": "ff-warm-0000001"})
    assert warm.status_code == 502 and breaker.state == "open"
    cid = "ff-cid-0000000001"
    print(f"[输入] 熔断已打开，携带标识 {cid!r} 再请求一次（走快速失败）")
    t0 = time.perf_counter()
    resp = await client.get("/items/1", headers={"X-Correlation-ID": cid})
    ms = (time.perf_counter() - t0) * 1000
    body = resp.json()
    print(f"[判定依据] 快速失败 {ms:.3f}ms 内返回 502 dependency_error：{resp.status_code} {body}")
    assert ms < 50
    assert resp.status_code == 502
    assert body["error"]["type"] == "dependency_error"
    print("[判定依据] 不泄露内部细节（无 CircuitOpen/simulated downstream 字样）")
    assert "CircuitOpen" not in str(body) and "simulated downstream" not in str(body)
    lookup = await client.get(f"/observability/requests/{cid}")
    print(f"[判定依据] 响应头/响应体/档案标识一致："
          f"{resp.headers['X-Correlation-ID']!r} / {body['error']['correlation_id']!r} / "
          f"{lookup.json()['correlation_id']!r}")
    assert resp.headers["X-Correlation-ID"] == cid
    assert body["error"]["correlation_id"] == cid
    assert lookup.json()["correlation_id"] == cid
    assert lookup.json()["outcome"] == "dependency_error"
    dependency.set_mode("ok")


async def test_recovery_after_cooldown_with_normal_traffic(client):
    breaker.configure(fail_threshold=3, window_size=20, fail_ratio=0.5, cooldown_s=0.05)
    dependency.set_mode("error")
    for i in range(3):
        await client.get("/items/1", headers={"X-Correlation-ID": f"rec-down-{i:08d}"})
    assert breaker.state == "open"
    print("[输入] 3 次错误后熔断打开；依赖恢复 ok，等待冷却后发送试探流量")
    dependency.set_mode("ok")
    # 冷却期内：快速失败
    ff = await client.get("/items/1", headers={"X-Correlation-ID": "rec-ff-00000001"})
    print(f"[判定依据] 冷却期内仍快速失败：{ff.status_code}，state={breaker.state}")
    assert ff.status_code == 502
    await asyncio.sleep(0.07)
    probe = await client.get("/items/1", headers={"X-Correlation-ID": "rec-probe-00001"})
    print(f"[判定依据] 冷却后试探请求成功：{probe.status_code}，state={breaker.state}")
    assert probe.status_code == 200
    assert breaker.state == "closed"
    after = await client.get("/items/1", headers={"X-Correlation-ID": "rec-after-00001"})
    h = await client.get("/health")
    print(f"[判定依据] 恢复后正常流量 200，/health={h.status_code} {h.json()['status']} "
          f"circuit={h.json()['circuit']['state']}")
    assert after.status_code == 200
    assert h.status_code == 200 and h.json()["status"] == "ok"


async def test_failed_probe_keeps_degraded_until_dependency_recovers(client):
    breaker.configure(fail_threshold=2, window_size=20, fail_ratio=0.5, cooldown_s=0.05)
    dependency.set_mode("error")
    for i in range(2):
        await client.get("/items/1", headers={"X-Correlation-ID": f"fp-down-{i:08d}"})
    assert breaker.state == "open"
    print("[输入] 熔断打开且依赖仍为 error；冷却后试探请求应失败并保持降级")
    await asyncio.sleep(0.07)
    probe = await client.get("/items/1", headers={"X-Correlation-ID": "fp-probe-fail-1"})
    print(f"[判定依据] 试探失败：{probe.status_code}，state={breaker.state}（不抖动回正常）")
    assert probe.status_code == 502
    assert breaker.state == "open"
    dependency.set_mode("ok")
    await asyncio.sleep(0.07)
    probe2 = await client.get("/items/1", headers={"X-Correlation-ID": "fp-probe-ok-001"})
    print(f"[判定依据] 依赖真正恢复后的下一个试探成功并关闭熔断：{probe2.status_code} {breaker.state}")
    assert probe2.status_code == 200
    assert breaker.state == "closed"


async def test_breaker_concurrent_decisions_are_stable(client):
    breaker.configure(fail_threshold=5, window_size=50, fail_ratio=0.9, cooldown_s=60)
    dependency.set_mode("error")
    print("[输入] 20 个请求并发冲击错误依赖（阈值=5），观察熔断判定不抖动")
    cids = [f"conc-trip-{i:08d}" for i in range(20)]
    resps = await asyncio.gather(*(
        client.get("/items/1", headers={"X-Correlation-ID": cid}) for cid in cids
    ))
    assert all(r.status_code == 502 for r in resps)
    print(f"[判定依据] 全部 502，最终状态唯一为 open={breaker.state!r}；500 响应数=0（无抖动到兜底）")
    assert breaker.state == "open"
    assert sum(1 for r in resps if r.status_code == 500) == 0
    dependency.set_mode("ok")
