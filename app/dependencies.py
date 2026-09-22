"""进程内模拟的下游依赖：可切换 ok/degraded/down 状态。"""
from __future__ import annotations

import asyncio

from .config import settings
from .errors import DependencyFailure, DependencyTimeout


class SimulatedDependency:
    """用进程内状态模拟下游依赖的健康与响应。

    - ok:       正常返回；
    - degraded: 可返回但延迟显著升高（健康检查反映为降级）；
    - down:     调用抛出 DependencyFailure / DependencyTimeout。
    """

    #: down 状态下用于模拟"依赖超时"的挂起时长（远大于 timeout 阈值）
    HANG_SECONDS = 5.0

    def __init__(self) -> None:
        self.mode: str = settings.dependency_mode
        self.latency: float = settings.dependency_latency
        self.timeout: float = settings.dependency_timeout
        self.calls: int = 0

    def set_mode(self, mode: str) -> None:
        """切换依赖状态：ok / degraded / down。"""
        if mode not in ("ok", "degraded", "down"):
            raise ValueError(f"unknown mode: {mode}")
        self.mode = mode

    def health(self) -> str:
        """返回依赖健康状态，供健康检查聚合。"""
        return self.mode

    async def fetch(self, key: str) -> dict:
        """模拟一次下游调用，按当前状态产生正常/超时/异常。"""
        self.calls += 1
        if self.mode == "down":
            # 交替模拟两类依赖异常：超时与返回异常，便于分别验证归一化
            if key.endswith("timeout"):
                try:
                    await asyncio.wait_for(
                        asyncio.sleep(self.HANG_SECONDS), timeout=self.timeout
                    )
                except TimeoutError as exc:
                    raise DependencyTimeout(
                        "dependency timed out",
                        detail=f"fetch({key!r}) exceeded {self.timeout}s",
                    ) from exc
            raise DependencyFailure(
                "dependency unavailable",
                detail=f"simulated downstream is down, key={key!r}",
            )
        delay = self.latency * (10 if self.mode == "degraded" else 1)
        await asyncio.sleep(delay)
        return {"key": key, "value": f"value-for-{key}", "dependency_mode": self.mode}


dependency = SimulatedDependency()
