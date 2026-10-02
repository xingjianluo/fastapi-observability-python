"""熔断降级：连续失败/窗口失败率触发、快速失败口径与耗时、标识一致、半开恢复与健康联动。"""
from __future__ import annotations

import asyncio
import logging
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


def test_fast_fail_kind_follows_consecutive_timeout_trip():
    b = CircuitBreaker(fail_threshold=3, window_size=10, fail_ratio=0.9, cooldown_s=60)
    print("[输入] 持续超时：连续记录 3 次 timeout 失败（阈值=3）")
    for _ in range(3):
        b.record_failure("timeout")
    assert b.state == "open"
    allowed, kind = b.check_request()
    print(f"[判定依据] 降级后快速失败口径必须是 timeout（对应 504），实际 allowed={allowed} kind={kind!r}")
    assert (allowed, kind) == (False, "timeout")
    assert b.snapshot()["trip_failure_kind"] == "timeout"
    print("[判定依据] 并发多次判定口径稳定，不出现 timeout/error 抖动")
    decisions = [b.check_request() for _ in range(20)]
    assert all(k == "timeout" for _, k in decisions)


def test_fast_fail_kind_follows_consecutive_error_trip():
    b = CircuitBreaker(fail_threshold=3, window_size=10, fail_ratio=0.9, cooldown_s=60)
    for _ in range(3):
        b.record_failure("error")
    print(f"[输入] 持续出错降级后快速失败口径保持 error（对应 502）：{b.check_request()}")
    assert b.check_request() == (False, "error")


def test_fast_fail_kind_uses_window_majority_when_tripped_by_ratio():
    print("[输入] 连续阈值极大，仅靠窗口判定：窗口=10，比例=0.5，7 次 timeout + 3 次 error")
    b = CircuitBreaker(fail_threshold=999, window_size=10, fail_ratio=0.5, cooldown_s=60)
    for kind in ["timeout"] * 7 + ["error"] * 3:
        b.record_failure(kind)
    assert b.state == "open"
    print(f"[判定依据] 窗口多数为 timeout -> 快速失败口径 timeout：{b.check_request()}")
    assert b.check_request() == (False, "timeout")

    print("[输入] 反向：3 次 timeout + 7 次 error")
    b.reset()
    for kind in ["timeout"] * 3 + ["error"] * 7:
        b.record_failure(kind)
    assert b.state == "open"
    print(f"[判定依据] 窗口多数为 error -> 快速失败口径 error：{b.check_request()}")
    assert b.check_request() == (False, "error")


def test_fast_fail_kind_tie_prefers_latest_failure():
    print("[输入] 窗口平票（5 timeout + 5 error，最后一次为 error），口径需确定可复现")
    b = CircuitBreaker(fail_threshold=999, window_size=10, fail_ratio=0.5, cooldown_s=60)
    for kind in ["timeout"] * 5 + ["error"] * 5:
        b.record_failure(kind)
    assert b.state == "open"
    print(f"[判定依据] 平票取最近一次失败 error：{b.check_request()}")
    assert b.check_request() == (False, "error")


def test_failed_probe_updates_fast_fail_kind():
    print("[输入] timeout 降级后冷却试探；试探以 error 失败，快速失败口径随之切到 error")
    b = CircuitBreaker(fail_threshold=1, window_size=10, fail_ratio=0.9, cooldown_s=0.02)
    b.record_failure("timeout")
    assert b.state == "open" and b.check_request() == (False, "timeout")
    time.sleep(0.03)
    assert b.allow_request() is True  # 唯一试探
    b.record_failure("error")
    assert b.state == "open"
    print(f"[判定依据] 试探失败重新打开后口径为 error：{b.check_request()}")
    assert b.check_request() == (False, "error")


def test_successful_probe_clears_trip_kind():
    b = CircuitBreaker(fail_threshold=1, window_size=10, fail_ratio=0.9, cooldown_s=0.02)
    b.record_failure("timeout")
    time.sleep(0.03)
    assert b.allow_request() is True
    b.record_success()
    print(f"[判定依据] 试探成功恢复 closed，trip_failure_kind 清空：{b.snapshot()}")
    assert b.state == "closed"
    assert b.snapshot()["trip_failure_kind"] is None
    assert b.check_request() == (True, None)


def test_invalid_failure_kind_rejected():
    b = CircuitBreaker(fail_threshold=3, window_size=10, fail_ratio=0.9, cooldown_s=60)
    try:
        b.record_failure("not-a-kind")  # type: ignore[arg-type]
    except ValueError:
        print("[判定依据] 非法失败类别被 ValueError 拒绝")
    else:
        raise AssertionError("非法失败类别应被拒绝")


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


async def test_timeout_trip_fast_fail_keeps_504_everywhere(client, caplog):
    """持续超时触发降级：真实失败与快速失败都是 504 dependency_timeout；
    响应头/响应体/结构化日志/回查档案的标识、分类、状态码完全一致。"""
    breaker.configure(fail_threshold=1, window_size=20, fail_ratio=0.5, cooldown_s=60)
    dependency.set_mode("timeout")
    print("[输入] 依赖模式=timeout，发起 1 次真实超时请求打出熔断")
    warm_cid = "ff-to-warm-00001"
    with caplog.at_level(logging.WARNING, logger="app.request"):
        warm = await client.get("/items/1", headers={"X-Correlation-ID": warm_cid})
    warm_body = warm.json()
    print(f"[判定依据] 真实下游超时：HTTP {warm.status_code} {warm_body['error']['type']}，"
          f"state={breaker.state}")
    assert warm.status_code == 504
    assert warm_body["error"]["type"] == "dependency_timeout"
    assert breaker.state == "open"
    assert breaker.snapshot()["trip_failure_kind"] == "timeout"

    caplog.clear()
    cid = "ff-to-cid-0000001"
    print(f"[输入] 熔断已打开，携带标识 {cid!r} 再请求一次（走快速返回，不再等待下游）")
    t0 = time.perf_counter()
    with caplog.at_level(logging.WARNING, logger="app.request"):
        resp = await client.get("/items/1", headers={"X-Correlation-ID": cid})
    ms = (time.perf_counter() - t0) * 1000
    body = resp.json()
    print(f"[判定依据] 快速返回 {ms:.3f}ms（未等满 200ms 超时预算），"
          f"HTTP {resp.status_code} error.type={body['error']['type']}")
    assert ms < 50
    print("[判定依据] 快速返回口径与真实超时一致：504 dependency_timeout，不跳成 502")
    assert resp.status_code == 504
    assert body["error"]["type"] == "dependency_timeout"
    print("[判定依据] 不泄露下游/内部判断细节（无 CircuitOpen/timed out after/circuit 字样）")
    rendered = str(body)
    assert "CircuitOpen" not in rendered
    assert "circuit" not in rendered
    assert "timed out after" not in rendered

    print("[判定依据] 结构化日志记录的 error_type/status_code 与对外一致")
    failed_logs = [r for r in caplog.records if getattr(r, "extra_fields", {}).get("event") == "request.failed"]
    assert failed_logs, "快速返回应产生一条 request.failed 日志"
    fields = failed_logs[-1].extra_fields
    print(f"[判定依据] 日志字段 error_type={fields.get('error_type')!r} "
          f"status_code={fields.get('status_code')!r}")
    assert fields["error_type"] == "dependency_timeout"
    assert fields["status_code"] == 504

    lookup = await client.get(f"/observability/requests/{cid}")
    rec = lookup.json()
    print(f"[判定依据] 响应头={resp.headers['X-Correlation-ID']!r} 响应体="
          f"{body['error']['correlation_id']!r} 档案={rec['correlation_id']!r}；"
          f"档案 status={rec['status_code']} outcome={rec['outcome']} "
          f"error_type={rec['error_type']}")
    assert resp.headers["X-Correlation-ID"] == cid
    assert body["error"]["correlation_id"] == cid
    assert rec["correlation_id"] == cid
    assert rec["status_code"] == 504
    assert rec["outcome"] == "dependency_timeout"
    assert rec["error_type"] == "dependency_timeout"
    dependency.set_mode("ok")


async def test_error_trip_fast_fail_keeps_502_everywhere(client, caplog):
    """持续出错触发降级：真实失败与快速失败都是 502 dependency_error，口径同样一致。"""
    breaker.configure(fail_threshold=1, window_size=20, fail_ratio=0.5, cooldown_s=60)
    dependency.set_mode("error")
    warm = await client.get("/items/1", headers={"X-Correlation-ID": "ff-err-warm-001"})
    assert warm.status_code == 502 and breaker.state == "open"
    assert breaker.snapshot()["trip_failure_kind"] == "error"
    cid = "ff-err-cid-000001"
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.request"):
        resp = await client.get("/items/1", headers={"X-Correlation-ID": cid})
    body = resp.json()
    print(f"[判定依据] 持续出错降级后快速返回：HTTP {resp.status_code} {body['error']['type']}")
    assert resp.status_code == 502 and body["error"]["type"] == "dependency_error"
    fields = [r for r in caplog.records
              if getattr(r, "extra_fields", {}).get("event") == "request.failed"][-1].extra_fields
    assert fields["error_type"] == "dependency_error" and fields["status_code"] == 502
    assert "simulated downstream" not in str(body)
    rec = (await client.get(f"/observability/requests/{cid}")).json()
    assert (rec["correlation_id"], rec["status_code"], rec["outcome"], rec["error_type"]) == \
        (cid, 502, "dependency_error", "dependency_error")
    print(f"[判定依据] 响应/日志/档案三处口径一致：502 dependency_error，标识 {cid!r}")
    dependency.set_mode("ok")


async def test_window_ratio_timeout_trip_fast_fail_is_504(client):
    """边界：连续失败阈值不可达，仅靠窗口失败率（多数为超时）触发，口径仍跟随真实失败。"""
    breaker.configure(fail_threshold=999, window_size=6, fail_ratio=0.5, cooldown_s=60)
    dependency.set_mode("timeout")
    print("[输入] fail_threshold=999，仅靠窗口（6 次、比例 0.5）：发起 6 次持续超时")
    statuses = []
    for i in range(6):
        r = await client.get("/items/1", headers={"X-Correlation-ID": f"win-to-{i:08d}"})
        statuses.append(r.status_code)
    assert breaker.state == "open"
    print(f"[判定依据] 前 6 次真实超时均为 504：{statuses}，trip_failure_kind="
          f"{breaker.snapshot()['trip_failure_kind']!r}")
    assert statuses == [504] * 6
    resp = await client.get("/items/1", headers={"X-Correlation-ID": "win-to-ff-00001"})
    body = resp.json()
    print(f"[判定依据] 窗口失败率触发后的快速返回仍为 504 dependency_timeout："
          f"{resp.status_code} {body['error']['type']}")
    assert resp.status_code == 504 and body["error"]["type"] == "dependency_timeout"
    rec = (await client.get("/observability/requests/win-to-ff-00001")).json()
    assert rec["status_code"] == 504 and rec["outcome"] == "dependency_timeout"
    dependency.set_mode("ok")


async def test_fast_fail_kind_stable_under_concurrency(client):
    """多个请求并发打到已因超时降级的依赖上：快速返回口径稳定，全部 504，不出现 502。"""
    breaker.configure(fail_threshold=2, window_size=50, fail_ratio=0.9, cooldown_s=60)
    dependency.set_mode("timeout")
    print("[输入] 先 2 次真实超时打出熔断，再 30 个请求并发打到已降级依赖")
    for i in range(2):
        await client.get("/items/1", headers={"X-Correlation-ID": f"conc-to-trip-{i:02d}"})
    assert breaker.state == "open"
    cids = [f"conc-to-ff-{i:08d}" for i in range(30)]
    resps = await asyncio.gather(*(
        client.get("/items/1", headers={"X-Correlation-ID": cid}) for cid in cids
    ))
    codes = sorted({r.status_code for r in resps})
    types = sorted({r.json()["error"]["type"] for r in resps})
    print(f"[判定依据] 30 个并发快速返回状态码集合={codes}，错误分类集合={types}（必须各自唯一）")
    assert codes == [504]
    assert types == ["dependency_timeout"]
    print("[判定依据] 每个并发请求的标识、状态码、分类在响应与档案间逐一一致")
    for cid, resp in zip(cids, resps):
        assert resp.headers["X-Correlation-ID"] == cid
        assert resp.json()["error"]["correlation_id"] == cid
        rec = (await client.get(f"/observability/requests/{cid}")).json()
        assert rec["status_code"] == 504 and rec["error_type"] == "dependency_timeout"
    dependency.set_mode("ok")


async def test_recovery_classification_returns_to_real_failure_then_normal(client):
    """冷却恢复口径不跳变：timeout 降级 -> 冷却期快速返回仍是 504 ->
    依赖仍超时则试探真实失败 504 并重新打开 -> 依赖恢复后试探 200，正常流量复原。"""
    breaker.configure(fail_threshold=2, window_size=20, fail_ratio=0.5, cooldown_s=0.05)
    dependency.set_mode("timeout")
    for i in range(2):
        await client.get("/items/1", headers={"X-Correlation-ID": f"rto-trip-{i:08d}"})
    assert breaker.state == "open"
    print("[输入] 持续超时降级；冷却期内发一次快速返回")
    ff = await client.get("/items/1", headers={"X-Correlation-ID": "rto-ff-0000001"})
    print(f"[判定依据] 冷却期快速返回仍为 504：{ff.status_code}，state={breaker.state}")
    assert ff.status_code == 504 and ff.json()["error"]["type"] == "dependency_timeout"

    print("[输入] 冷却结束但依赖仍为 timeout：试探请求是一次真实调用，应真实失败 504 并重新降级")
    await asyncio.sleep(0.07)
    probe_fail = await client.get("/items/1", headers={"X-Correlation-ID": "rto-probe-bad1"})
    print(f"[判定依据] 试探真实超时：{probe_fail.status_code}，state={breaker.state}；"
          f"其后的快速返回口径不跳变")
    assert probe_fail.status_code == 504
    assert breaker.state == "open"
    ff2 = await client.get("/items/1", headers={"X-Correlation-ID": "rto-ff-0000002"})
    assert ff2.status_code == 504 and ff2.json()["error"]["type"] == "dependency_timeout"

    print("[输入] 依赖恢复 ok，等待冷却后试探成功，错误分类/状态码回到正常表现")
    dependency.set_mode("ok")
    await asyncio.sleep(0.07)
    probe_ok = await client.get("/items/1", headers={"X-Correlation-ID": "rto-probe-ok01"})
    print(f"[判定依据] 恢复试探：{probe_ok.status_code}，state={breaker.state}")
    assert probe_ok.status_code == 200 and breaker.state == "closed"
    after = await client.get("/items/1", headers={"X-Correlation-ID": "rto-after-00001"})
    assert after.status_code == 200
    h = await client.get("/health")
    print(f"[判定依据] 恢复后正常流量 200，/health={h.status_code} {h.json()['status']}")
    assert h.status_code == 200 and h.json()["status"] == "ok"


async def test_probe_failure_kind_switch_on_recovery_attempt(client):
    """边界：因超时降级后，冷却试探时下游实际表现为 error，
    则试探按真实结果给 502，重新打开后的快速返回也随之是 502（口径跟随最近真实失败）。"""
    breaker.configure(fail_threshold=1, window_size=20, fail_ratio=0.5, cooldown_s=0.05)
    dependency.set_mode("timeout")
    warm = await client.get("/items/1", headers={"X-Correlation-ID": "ps-warm-000001"})
    assert warm.status_code == 504 and breaker.state == "open"
    ff = await client.get("/items/1", headers={"X-Correlation-ID": "ps-ff-00000001"})
    assert ff.status_code == 504
    print("[输入] timeout 降级快速返回为 504；冷却后把下游切成 error 再试探")
    dependency.set_mode("error")
    await asyncio.sleep(0.07)
    probe = await client.get("/items/1", headers={"X-Correlation-ID": "ps-probe-err-1"})
    print(f"[判定依据] 试探真实失败为 error：{probe.status_code}，state={breaker.state}")
    assert probe.status_code == 502 and breaker.state == "open"
    ff2 = await client.get("/items/1", headers={"X-Correlation-ID": "ps-ff-00000002"})
    body = ff2.json()
    print(f"[判定依据] 重新打开后的快速返回跟随试探失败口径：{ff2.status_code} {body['error']['type']}")
    assert ff2.status_code == 502 and body["error"]["type"] == "dependency_error"
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
