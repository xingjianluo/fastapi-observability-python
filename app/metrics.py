"""进程内指标后端：按路由与状态码统计请求数与耗时。"""
from __future__ import annotations

import threading
from collections import defaultdict


class MetricsRegistry:
    """线程安全的进程内指标存储。

    - 计数与耗时按 (route, status_code) 维度聚合；
    - 全部写入走同一把锁，并发下不丢失、不重复；
    - snapshot 返回副本，调用方修改不影响内部状态。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: dict[tuple[str, int], int] = defaultdict(int)
        self._durations: dict[tuple[str, int], float] = defaultdict(float)

    def record_request(self, route: str, status_code: int, duration: float) -> None:
        """记录一次请求；每次调用只计一次，并发写入安全。"""
        with self._lock:
            key = (route, status_code)
            self._requests[key] += 1
            self._durations[key] += duration

    def snapshot(self) -> dict:
        """返回当前指标快照（只读副本）。"""
        with self._lock:
            requests = {
                f"{route}|{status}": count
                for (route, status), count in sorted(self._requests.items())
            }
            durations = {
                f"{route}|{status}": round(total, 6)
                for (route, status), total in sorted(self._durations.items())
            }
        return {
            "requests_total": requests,
            "duration_seconds_sum": durations,
        }

    def reset(self) -> None:
        """清空指标（测试用）。"""
        with self._lock:
            self._requests.clear()
            self._durations.clear()


registry = MetricsRegistry()
