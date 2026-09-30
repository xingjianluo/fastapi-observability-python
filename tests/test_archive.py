"""请求观测档案：各结果路径可回查、查不回、上限 FIFO 淘汰、并发不丢不重、标识一致。"""
from __future__ import annotations

import asyncio
import threading

from app.archive import RequestArchive, RequestRecord, archive
from app.main import app


def _record(cid: str, status: int = 200, source: str = "client") -> RequestRecord:
    return RequestRecord(
        correlation_id=cid,
        correlation_id_source=source,
        method="GET",
        route="/items/{item_id}",
        status_code=status,
        outcome={200: "success", 422: "business_rejected", 504: "dependency_timeout",
                 502: "dependency_error", 500: "unexpected_error"}.get(status, "client_error"),
        error_type={422: "business_rejected", 504: "dependency_timeout",
                    502: "dependency_error", 500: "unexpected_error"}.get(status),
        latency_ms=1.0,
        started_at="2026-10-01T00:00:00+00:00",
    )


async def test_all_outcomes_archived_and_lookup_by_response_id(client):
    """成功 / 业务拒绝 / 依赖超时 / 兜底异常：响应标识都能恰好查回一条。"""
    from app.dependencies import dependency

    @app.get("/test-only-boom-archive")
    async def boom() -> None:
        raise ValueError("unexpected kaboom")

    # (cid, path, expect, 发起前依赖模式)
    cases: list[tuple[str, str, int, str]] = [
        ("arch-success-0001", "/items/1", 200, "ok"),
        ("arch-biz-000000001", "/items/-1", 422, "ok"),
        ("arch-timeout-0001", "/items/1", 504, "timeout"),
        ("arch-boom-0000001", "/test-only-boom-archive", 500, "ok"),
    ]
    print("[输入] 依次发起覆盖成功/422/504/500 的请求（每次发起前切换依赖模式）")
    seen: list[tuple[str, int, dict]] = []
    for cid, path, expect, mode in cases:
        dependency.set_mode(mode)
        resp = await client.get(path, headers={"X-Correlation-ID": cid})
        assert resp.status_code == expect
        response_cid = resp.headers["X-Correlation-ID"]
        lookup = await client.get(f"/observability/requests/{response_cid}")
        seen.append((cid, resp.status_code, lookup.json()))
        print(f"[判定依据] {cid} -> HTTP {resp.status_code}，"
              f"用响应头标识 {response_cid!r} 回查 HTTP {lookup.status_code}，"
              f"档案 outcome={lookup.json().get('outcome')} error_type={lookup.json().get('error_type')}")
        assert response_cid == cid
        assert lookup.status_code == 200
        rec = lookup.json()
        assert rec["correlation_id"] == cid
        assert rec["status_code"] == expect
        assert rec["method"] == "GET"
        assert rec["route"] in ("/items/{item_id}", "/test-only-boom-archive")
        assert rec["correlation_id_source"] == "client"
        assert rec["latency_ms"] >= 0
    dependency.set_mode("ok")
    print("[判定依据] 四条档案结果归类依次为 success/business_rejected/dependency_timeout/unexpected_error")
    assert [r["outcome"] for _, _, r in seen] == [
        "success", "business_rejected", "dependency_timeout", "unexpected_error"]


async def test_generated_id_is_lookup_too(client):
    print("[输入] 不带标识 + 带非法标识各请求一次，再用响应里补出来的标识回查")
    r1 = await client.get("/items/1")
    r2 = await client.get("/items/1", headers={"X-Correlation-ID": "@@bad@@"})
    for resp, tag in [(r1, "缺失"), (r2, "非法")]:
        cid = resp.headers["X-Correlation-ID"]
        assert resp.headers["X-Correlation-ID-Generated"] == "true"
        lookup = await client.get(f"/observability/requests/{cid}")
        print(f"[判定依据] {tag}标识 -> 服务端补 {cid!r}，回查 HTTP {lookup.status_code}，"
              f"档案 source={lookup.json().get('correlation_id_source')}")
        assert lookup.status_code == 200
        assert lookup.json()["correlation_id"] == cid
        assert lookup.json()["correlation_id_source"] == "generated"


async def test_lookup_miss_returns_404(client):
    missing = "never-existed-id-99"
    print(f"[输入] 用从未出现过的标识 {missing!r} 回查")
    resp = await client.get(f"/observability/requests/{missing}")
    body = resp.json()
    print(f"[判定依据] 期望 404/not_found 且响应携带自身请求标识，实际: {resp.status_code} {body}")
    assert resp.status_code == 404
    assert body["error"]["type"] == "not_found"
    assert body["error"]["correlation_id"]


def test_archive_fifo_eviction():
    store = RequestArchive(max_records=3)
    print("[输入] 容量=3 的档案依次写入 cid-1..cid-5")
    for i in range(1, 6):
        store.save(_record(f"cid-{i}"))
    print(f"[判定依据] 最老的 cid-1/cid-2 已淘汰（get=None），cid-3..cid-5 仍可查，len={len(store)}")
    assert store.get("cid-1") is None
    assert store.get("cid-2") is None
    assert [store.get(f"cid-{i}").correlation_id for i in (3, 4, 5)] == [
        "cid-3", "cid-4", "cid-5"]
    assert len(store) == 3


def test_archive_same_id_kept_exactly_one_and_refreshed_order():
    store = RequestArchive(max_records=2)
    print("[输入] 容量=2，写入 cid-a、cid-b 后用同标识 cid-a 再写一条更新的档案")
    store.save(_record("cid-a", 200))
    store.save(_record("cid-b", 200))
    store.save(_record("cid-a", 500))
    keys = list(store._records.keys())
    print(f"[判定依据] 同一标识恰好一条（cid-a 状态已更新为 500）且排到队尾，顺序={keys}，len={len(store)}")
    assert len(store) == 2
    assert keys == ["cid-b", "cid-a"]
    assert store.get("cid-a").status_code == 500
    print("[判定依据] 再写 cid-c 触发淘汰时，被淘汰的是最老的 cid-b，cid-a 仍可查")
    store.save(_record("cid-c", 200))
    assert store.get("cid-b") is None
    assert store.get("cid-a") is not None
    assert store.get("cid-c") is not None


def test_archive_concurrent_writes_no_loss_no_duplicate():
    store = RequestArchive(max_records=10_000)
    n_threads, per_thread = 16, 125
    n = n_threads * per_thread
    print(f"[输入] {n_threads} 线程 x {per_thread} 条 = {n} 条并发写入，全部使用不同标识")

    def worker(t: int) -> None:
        for i in range(per_thread):
            store.save(_record(f"conc-{t:03d}-{i:05d}"))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    print(f"[判定依据] 留存条数必须等于真实请求数 {n}（不丢、不重），实际 len={len(store)}")
    assert len(store) == n
    missing = [f"conc-{t:03d}-{i:05d}"
               for t in range(n_threads) for i in range(per_thread)
               if store.get(f"conc-{t:03d}-{i:05d}") is None]
    assert not missing, f"丢失 {len(missing)} 条"


async def test_archive_count_matches_concurrent_requests(client):
    archive.resize(10_000)
    n = 60
    print(f"[输入] 经 HTTP 并发发起 {n} 个带不同合法标识的请求")
    cids = [f"http-conc-{i:08x}" for i in range(n)]

    async def call(cid: str) -> str:
        resp = await client.get("/items/1", headers={"X-Correlation-ID": cid})
        return resp.headers["X-Correlation-ID"]

    got = await asyncio.gather(*(call(c) for c in cids))
    found = [cid for cid in got if archive.get(cid) is not None]
    print(f"[判定依据] {n} 个并发请求的响应标识在档案中逐一可查，实际命中 {len(found)} 条")
    assert sorted(found) == sorted(cids)


async def test_correlation_id_consistent_everywhere_for_all_outcomes(client):
    """任何最终结果（含 500 兜底与快速失败）：响应头/响应体/日志/档案标识全一致。"""
    import logging

    from app.context import get_correlation_id
    from app.dependencies import dependency
    from app.resilience import breaker

    @app.get("/test-only-consistency-boom")
    async def boom2() -> None:
        raise RuntimeError("boom for consistency check")

    cid_seen: list[str | None] = []

    class CidCapture(logging.Handler):
        """在日志发出的当下（请求上下文内）捕获 correlation_id。"""

        def emit(self, record: logging.LogRecord) -> None:
            cid_seen.append(get_correlation_id())

    handler = CidCapture()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        breaker.configure(fail_threshold=3, window_size=20, fail_ratio=0.5, cooldown_s=60)
        dependency.set_mode("error")
        print("[输入] 依次触发：3 次真实依赖错误打开熔断 -> 1 次快速失败 -> 1 次 500 兜底")
        cids = ["cons-err-000001", "cons-err-000002", "cons-err-000003",
                "cons-fast-000001", "cons-boom-000001"]
        resps = []
        for cid in cids[:3]:
            resps.append(await client.get("/items/1", headers={"X-Correlation-ID": cid}))
        assert breaker.state == "open"
        resps.append(await client.get("/items/1", headers={"X-Correlation-ID": cids[3]}))
        resps.append(await client.get("/test-only-consistency-boom",
                                      headers={"X-Correlation-ID": cids[4]}))
    finally:
        root.removeHandler(handler)
        dependency.set_mode("ok")

    for cid, resp in zip(cids, resps):
        header_cid = resp.headers["X-Correlation-ID"]
        body_cid = resp.json()["error"]["correlation_id"]
        lookup = await client.get(f"/observability/requests/{cid}")
        archive_cid = lookup.json()["correlation_id"]
        in_logs = cid in cid_seen
        print(f"[判定依据] {cid} HTTP {resp.status_code}: 响应头={header_cid!r} "
              f"响应体={body_cid!r} 档案={archive_cid!r} 该标识日志存在={in_logs}")
        assert header_cid == body_cid == archive_cid == cid
        assert in_logs, f"{cid} 在日志中缺失"
    print("[判定依据] 快速失败请求仍为 502 dependency_error，与真实依赖错误口径一致；兜底为 500")
    assert resps[3].status_code == 502
    assert resps[3].json()["error"]["type"] == "dependency_error"
    assert "simulated downstream" not in str(resps[3].json())
    assert resps[4].status_code == 500
    assert resps[4].json()["error"]["type"] == "unexpected_error"
