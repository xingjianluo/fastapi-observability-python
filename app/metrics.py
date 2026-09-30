"""进程内指标后端：按 (method, route, status_code) 统计请求数与耗时。

- 用锁保证并发写入不丢失、不重复计数；
- snapshot() 返回一致性快照，供 /metrics 端点与测试断言；
- reset() 供测试隔离使用。
"""

from __future__ import annotations

import threading


class MetricsRegistry:
    """线程安全的进程内指标注册表。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key: (method, route, status_code) -> count
        self._counters: dict[tuple[str, str, int], int] = {}
        # key: (method, route, status_code) -> (total_ms, max_ms)
        self._latency: dict[tuple[str, str, int], tuple[float, float]] = {}

    def record_request(
        self, method: str, route: str, status_code: int, latency_ms: float
    ) -> None:
        """记录一次请求。每次调用恰好计数一次，重复调用需调用方保证幂等。"""
        key = (method, route, status_code)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1
            total, max_ms = self._latency.get(key, (0.0, 0.0))
            self._latency[key] = (total + latency_ms, max(max_ms, latency_ms))

    def snapshot(self) -> dict:
        """返回当前指标的一致性快照。"""
        with self._lock:
            requests = []
            for (method, route, status), count in sorted(self._counters.items()):
                total_ms, max_ms = self._latency[(method, route, status)]
                requests.append(
                    {
                        "method": method,
                        "route": route,
                        "status_code": status,
                        "count": count,
                        "latency_ms": {
                            "total": round(total_ms, 3),
                            "avg": round(total_ms / count, 3),
                            "max": round(max_ms, 3),
                        },
                    }
                )
            return {"requests": requests, "total": sum(self._counters.values())}

    def reset(self) -> None:
        """清空全部指标（测试隔离用）。"""
        with self._lock:
            self._counters.clear()
            self._latency.clear()


registry = MetricsRegistry()
