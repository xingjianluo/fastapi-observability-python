"""依赖熔断/快速失败与近期失败统计。

判定规则（确定且可解释，全部状态变更在同一把锁内完成，并发下不会来回抖动）：

关闭（closed）：正常放行。每次依赖调用结果写入时间窗口，满足任一条件即熔断为开放态：
  1) 连续失败次数达到 ``failure_threshold``；
  2) 最近 ``window_s`` 秒窗口内样本数达到 ``min_samples``，且失败占比 >= ``failure_ratio``。
开放（open）：``before_call`` 立即快速失败，不再等待下游超时；抛出的错误分类与状态码
  沿用触发熔断时的那类依赖错误（504 dependency_timeout / 502 dependency_error），
  并在异常上标记 ``fast_failed=True``。经过 ``open_cooldown_s`` 冷却后，下一个请求
  把状态推进到半开态（且仅放行一个探测请求）。
半开（half_open）：探测请求成功 -> 回关闭态并重新累计；失败 -> 立即回开放态并重新计时。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from app.errors import DependencyError, DependencyTimeout
from app.logging_config import get_logger

logger = get_logger(__name__)

#: 错误分类 -> 快速失败时抛出的异常类型（保持与真实失败完全一致的对外口径）
_RAISE: dict[str, type[DependencyTimeout | DependencyError]] = {
    "dependency_timeout": DependencyTimeout,
    "dependency_error": DependencyError,
}

#: 快速失败异常的对外 message，不包含任何下游内部细节
_FAST_FAIL_MESSAGE = "dependency temporarily unavailable, request failed fast"


class CircuitBreaker:
    """连续失败/窗口期失败占比驱动的进程内熔断器。"""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        window_s: float = 30.0,
        min_samples: int = 5,
        failure_ratio: float = 0.5,
        open_cooldown_s: float = 5.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.window_s = window_s
        self.min_samples = min_samples
        self.failure_ratio = failure_ratio
        self.open_cooldown_s = open_cooldown_s
        self._monotonic = now or time.monotonic
        self._lock = threading.Lock()
        self._state = self.CLOSED
        #: deque[(timestamp, is_failure)]，仅保留窗口内样本
        self._events: deque[tuple[float, bool]] = deque()
        self._opened_at = 0.0
        #: 触发熔断的错误分类，快速失败沿用同一口径
        self._tripped_error_type: str | None = None
        #: 半开态是否已有探测请求放行
        self._probe_in_flight = False

    def configure(
        self,
        *,
        failure_threshold: int | None = None,
        window_s: float | None = None,
        min_samples: int | None = None,
        failure_ratio: float | None = None,
        open_cooldown_s: float | None = None,
    ) -> None:
        """更新判定参数并复位状态（启动时用配置覆盖模块单例的默认值）。"""
        if failure_threshold is not None:
            self.failure_threshold = failure_threshold
        if window_s is not None:
            self.window_s = window_s
        if min_samples is not None:
            self.min_samples = min_samples
        if failure_ratio is not None:
            self.failure_ratio = failure_ratio
        if open_cooldown_s is not None:
            self.open_cooldown_s = open_cooldown_s
        self.reset()

    # ---- 内部工具（调用方持锁）-------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    def _consecutive_failures(self) -> int:
        count = 0
        for _, is_failure in reversed(self._events):
            if not is_failure:
                break
            count += 1
        return count

    def _open(self, now: float, error_type: str) -> None:
        self._state = self.OPEN
        self._opened_at = now
        self._tripped_error_type = error_type
        self._probe_in_flight = False
        logger.warning(
            "circuit opened: dependency calls will fail fast",
            extra={
                "extra_fields": {
                    "event": "circuit.opened",
                    "tripped_error_type": error_type,
                    "consecutive_failures": self._consecutive_failures(),
                }
            },
        )

    # ---- 对外接口 ---------------------------------------------------------

    def before_call(self) -> None:
        """请求依赖前调用。开放态立即抛出与触发原因同口径的依赖异常；否则放行。"""
        with self._lock:
            if self._state == self.OPEN:
                now = self._monotonic()
                if now - self._opened_at < self.open_cooldown_s:
                    raise self._fast_fail_exception()
                # 冷却结束：推进到半开态，仅放行一个探测请求
                self._state = self.HALF_OPEN
                self._probe_in_flight = True
                logger.warning(
                    "circuit half-open: one probe request allowed",
                    extra={"extra_fields": {"event": "circuit.half_open"}},
                )
            elif self._state == self.HALF_OPEN:
                # 探测请求尚未返回期间，其余请求继续快速失败
                if self._probe_in_flight:
                    raise self._fast_fail_exception()

    def _fast_fail_exception(self) -> Exception:
        error_type = self._tripped_error_type or "dependency_timeout"
        exc = _RAISE.get(error_type, DependencyTimeout)(_FAST_FAIL_MESSAGE)
        exc.fast_failed = True  # type: ignore[attr-defined]
        return exc

    def record_success(self) -> None:
        """记录一次依赖调用成功。"""
        with self._lock:
            now = self._monotonic()
            if self._state == self.HALF_OPEN:
                # 探测成功：恢复正常，窗口重新累计
                self._state = self.CLOSED
                self._events.clear()
                self._tripped_error_type = None
                self._probe_in_flight = False
                logger.warning(
                    "circuit closed: probe succeeded, traffic recovered",
                    extra={"extra_fields": {"event": "circuit.closed"}},
                )
                return
            if self._state == self.CLOSED:
                self._prune(now)
                self._events.append((now, False))

    def record_failure(self, error_type: str) -> None:
        """记录一次依赖调用失败，必要时触发熔断或回退开放态。"""
        with self._lock:
            now = self._monotonic()
            if self._state == self.HALF_OPEN:
                # 探测失败：立即回开放态并重新计时
                self._open(now, error_type)
                return
            self._prune(now)
            self._events.append((now, True))
            if self._state == self.CLOSED and self._should_trip():
                self._open(now, error_type)

    def _should_trip(self) -> bool:
        consecutive = self._consecutive_failures()
        if consecutive >= self.failure_threshold:
            return True
        total = len(self._events)
        if total >= self.min_samples:
            failures = sum(1 for _, is_failure in self._events if is_failure)
            return failures / total >= self.failure_ratio
        return False

    def snapshot(self) -> dict:
        """返回当前熔断器状态的一致性快照（供健康检查与测试断言）。"""
        with self._lock:
            now = self._monotonic()
            self._prune(now)
            total = len(self._events)
            failures = sum(1 for _, is_failure in self._events if is_failure)
            return {
                "state": self._state,
                "consecutive_failures": self._consecutive_failures(),
                "window_samples": total,
                "window_failures": failures,
                "window_failure_ratio": round(failures / total, 3) if total else 0.0,
                "tripped_error_type": self._tripped_error_type,
                "cooldown_remaining_s": round(
                    max(0.0, self.open_cooldown_s - (now - self._opened_at)), 3
                )
                if self._state == self.OPEN
                else 0.0,
            }

    def reset(self) -> None:
        """复位为关闭态并清空窗口（测试隔离用）。"""
        with self._lock:
            self._state = self.CLOSED
            self._events.clear()
            self._opened_at = 0.0
            self._tripped_error_type = None
            self._probe_in_flight = False


breaker = CircuitBreaker()
