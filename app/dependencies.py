"""进程内模拟的下游依赖：可控 ok / timeout / error 三种状态 + 熔断快速失败。

- ok:      正常返回模拟数据；
- timeout: 模拟耗时超过超时预算，抛出 DependencyTimeout；
- error:   模拟下游返回 5xx，抛出 DependencyError（原始异常挂到 cause）。

状态通过 set_mode 切换，健康检查据此报告 ok / degraded / down。
熔断器（app.resilience）在此之上：连续失败或近期失败率过高时，
fetch 不再真正等待下游，而是立即快速失败。快速失败沿用「触发本轮降级的
那一类失败」口径：因持续超时降级 -> DependencyTimeout/504，因下游出错
降级 -> DependencyError/502，与真正调用下游失败时完全一致，不换分类、
不泄露内部判断细节。
"""
from __future__ import annotations

import asyncio
import threading

from app.errors import DependencyError, DependencyTimeout
from app.resilience import CircuitOpen, breaker

VALID_MODES = ("ok", "timeout", "error")

#: 调用下游的超时预算（秒）
TIMEOUT_BUDGET_S = 0.2
#: timeout 模式下模拟的下游处理耗时（秒），大于超时预算
SLOW_DEPENDENCY_S = 1.0


class SimulatedDependency:
    """模拟下游依赖，线程安全地切换模式。"""

    def __init__(self, mode: str = "ok", latency_ms: float = 5.0) -> None:
        self._lock = threading.Lock()
        self._mode = mode
        self._latency_ms = latency_ms

    @property
    def mode(self) -> str:
        with self._lock:
            return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in VALID_MODES:
            raise ValueError(f"invalid mode: {mode!r}, expected one of {VALID_MODES}")
        with self._lock:
            self._mode = mode

    async def fetch(self, key: str) -> dict:
        """模拟一次下游调用，按当前模式返回或抛出归一化异常。

        熔断打开/半开排队中时立即快速失败，不等待下游、不计入失败统计
        （CircuitOpen 路径不调用 record_*）。快速失败的错误分类与状态码
        沿用触发本轮降级的失败类别：持续超时降级 -> 504 dependency_timeout；
        持续出错降级 -> 502 dependency_error。对调用方而言与真实下游失败
        完全同构，且不暴露「熔断/快速失败」这一内部判断。
        """
        allowed, fast_fail_kind = breaker.check_request()
        if not allowed:
            fast_fail = CircuitOpen("circuit open; fast-fail without calling downstream")
            if fast_fail_kind == "timeout":
                raise DependencyTimeout(
                    "dependency timed out", cause=fast_fail
                ) from fast_fail
            raise DependencyError(
                "dependency returned an error", cause=fast_fail
            ) from fast_fail
        mode = self.mode
        try:
            if mode == "timeout":
                try:
                    await asyncio.wait_for(asyncio.sleep(SLOW_DEPENDENCY_S), timeout=TIMEOUT_BUDGET_S)
                except TimeoutError as exc:
                    raise DependencyTimeout(
                        f"dependency call timed out after {TIMEOUT_BUDGET_S}s", cause=exc
                    ) from exc
            elif mode == "error":
                cause = RuntimeError("simulated downstream returned HTTP 500")
                raise DependencyError("dependency returned an error", cause=cause) from cause
            else:
                await asyncio.sleep(self._latency_ms / 1000.0)
        except DependencyError:
            breaker.record_failure("error")
            raise
        except DependencyTimeout:
            breaker.record_failure("timeout")
            raise
        else:
            breaker.record_success()
        return {"key": key, "value": f"value-for-{key}", "source": "simulated-dependency"}

    def health(self) -> str:
        """返回依赖健康状态：ok -> healthy，error -> down，timeout -> degraded。

        熔断状态优先：open 视为 down，half_open 视为 degraded。
        """
        state = breaker.state
        if state == "open":
            return "down"
        if state == "half_open":
            return "degraded"
        return {"ok": "healthy", "timeout": "degraded", "error": "down"}[self.mode]


dependency = SimulatedDependency()
