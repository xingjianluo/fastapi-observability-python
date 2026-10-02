"""下游依赖熔断器：连续失败 / 窗口失败率过高时降级，冷却后半开试探恢复。

状态机（所有判定在同一把锁内完成，并发下不抖动）：
- closed（正常）：放行所有请求；记录每次成功/失败；
  满足任一条件即切到 open：
    1) 连续失败次数 >= fail_threshold；
    2) 最近 window_size 次真实调用中失败占比 >= fail_ratio（窗口攒满才判定，避免小样本抖动）。
- open（降级）：快速失败，不再真正调用下游；
  冷却 cooldown_s 后只放行进队的「一个」试探请求，切到 half_open。
- half_open（试探）：除那一个试探请求外全部快速失败；
  试探成功 -> closed（清空计数/窗口）；试探失败 -> 重新 open 并重新计时。

快速失败口径：熔断器记住「打开本轮降级的那一类失败」（超时 / 依赖出错），
open 与 half_open 排队期间的快速失败沿用同一口径（504 dependency_timeout 或
502 dependency_error），与没有快速失败、真正调用下游失败时完全一致；
试探失败重新打开时，改记本次试探失败的类别。

快速失败不计入失败统计（没有真实调用），因此不会自我强化、也不会干扰恢复判定。
"""
from __future__ import annotations

import threading
import time
from collections import Counter, deque

from app.logging_config import get_logger

logger = get_logger("app.resilience")

#: 失败类别：timeout -> DependencyTimeout/504；error -> DependencyError/502
FAILURE_KINDS = ("timeout", "error")
DEFAULT_FAILURE_KIND = "error"


class CircuitOpen(Exception):
    """熔断打开时的快速失败信号（仅作为内部 cause，对外按触发降级的失败类别归一化）。"""


class CircuitBreaker:
    """连续失败阈值 + 滑动窗口失败率双条件，半开单次试探防抖动。"""

    def __init__(
        self,
        fail_threshold: int,
        window_size: int,
        fail_ratio: float,
        cooldown_s: float,
    ) -> None:
        if fail_threshold < 1 or window_size < 1:
            raise ValueError("fail_threshold and window_size must be >= 1")
        if not 0.0 < fail_ratio <= 1.0:
            raise ValueError("fail_ratio must be in (0, 1]")
        if cooldown_s < 0:
            raise ValueError("cooldown_s must be >= 0")
        self._lock = threading.Lock()
        self._fail_threshold = fail_threshold
        self._window_size = window_size
        self._fail_ratio = fail_ratio
        self._cooldown_s = cooldown_s
        self._state = "closed"
        self._consecutive_failures = 0
        #: 滑动窗口，元素 None=成功，"timeout"/"error"=对应类别的失败
        self._window: deque[str | None] = deque(maxlen=window_size)
        self._opened_at: float | None = None
        #: 触发本轮降级的失败类别（快速失败口径据此决定），closed 下为 None
        self._trip_failure_kind: str | None = None

    @property
    def state(self) -> str:
        """当前状态：closed / open / half_open。"""
        with self._lock:
            return self._state

    def snapshot(self) -> dict:
        """返回判定依据的一致性快照（健康检查与测试用）。"""
        with self._lock:
            failures = sum(1 for kind in self._window if kind is not None)
            return {
                "state": self._state,
                "consecutive_failures": self._consecutive_failures,
                "window_size": len(self._window),
                "window_failures": failures,
                "window_fail_ratio": round(failures / len(self._window), 3) if self._window else 0.0,
                "trip_failure_kind": self._trip_failure_kind,
                "cooldown_s": self._cooldown_s,
            }

    def check_request(self) -> tuple[bool, str | None]:
        """原子判定本次请求是否放行真实下游调用。

        返回 (allowed, fast_fail_kind)：
        - (True, None)：放行（closed 常态，或 open 冷却后唯一的 half_open 试探）；
        - (False, kind)：快速失败，kind 为触发本轮降级的失败类别
          （"timeout" -> 504 dependency_timeout；"error" -> 502 dependency_error）。
        状态翻转与类别读取在同一把锁内完成，并发下口径稳定不抖动。
        """
        now = time.monotonic()
        with self._lock:
            if self._state == "closed":
                return True, None
            if self._state == "open":
                if self._opened_at is not None and now - self._opened_at >= self._cooldown_s:
                    self._state = "half_open"
                    logger.info(
                        "circuit half-open: one probe allowed",
                        extra={"extra_fields": {"event": "circuit.half_open"}},
                    )
                    return True, None
                return False, self._fast_fail_kind()
            # half_open：试探请求在途，其余一律快速失败
            return False, self._fast_fail_kind()

    def allow_request(self) -> bool:
        """是否放行一次真实下游调用；open 超时后恰好放一个试探请求。"""
        allowed, _ = self.check_request()
        return allowed

    def _fast_fail_kind(self) -> str:
        """快速失败沿用的失败类别（仅在锁内调用），理论上不会落到默认值。"""
        return self._trip_failure_kind or DEFAULT_FAILURE_KIND

    def record_success(self) -> None:
        """记录一次真实调用成功；试探成功则关闭熔断并恢复正常。"""
        with self._lock:
            was_half_open = self._state == "half_open"
            self._consecutive_failures = 0
            self._window.append(None)
            if was_half_open:
                self._state = "closed"
                self._window.clear()
                self._opened_at = None
                self._trip_failure_kind = None
                logger.info(
                    "circuit closed after successful probe",
                    extra={"extra_fields": {"event": "circuit.closed"}},
                )

    def record_failure(self, kind: str = DEFAULT_FAILURE_KIND) -> None:
        """记录一次真实调用失败；试探失败重新打开，closed 下按双条件判定是否降级。

        kind 为失败类别（"timeout" / "error"）：它既是本次真实失败的口径，
        也会成为本轮降级后快速失败沿用的口径，保证快速失败前后分类一致。
        """
        if kind not in FAILURE_KINDS:
            raise ValueError(f"invalid failure kind: {kind!r}, expected one of {FAILURE_KINDS}")
        with self._lock:
            if self._state == "half_open":
                self._state = "open"
                self._opened_at = time.monotonic()
                # 试探本身是一次真实调用：重新打开后按试探失败的口径快速失败
                self._trip_failure_kind = kind
                logger.warning(
                    "circuit re-opened after failed probe",
                    extra={"extra_fields": {
                        "event": "circuit.reopened",
                        "trip_failure_kind": kind,
                    }},
                )
                return
            self._consecutive_failures += 1
            self._window.append(kind)
            if self._state == "closed" and self._should_trip():
                self._state = "open"
                self._opened_at = time.monotonic()
                self._trip_failure_kind = self._trip_kind()
                logger.warning(
                    "circuit opened due to dependency failures",
                    extra={
                        "extra_fields": {
                            "event": "circuit.opened",
                            "consecutive_failures": self._consecutive_failures,
                            "window_size": len(self._window),
                            "window_failures": sum(1 for k in self._window if k is not None),
                            "trip_failure_kind": self._trip_failure_kind,
                        }
                    },
                )

    def _should_trip(self) -> bool:
        """双条件：连续失败达阈值，或攒满窗口后失败占比达阈值。"""
        if self._consecutive_failures >= self._fail_threshold:
            return True
        if len(self._window) >= self._window_size:
            failures = sum(1 for kind in self._window if kind is not None)
            if failures / len(self._window) >= self._fail_ratio:
                return True
        return False

    def _trip_kind(self) -> str:
        """决定本轮降级快速失败沿用的失败类别（仅在触发降级时、锁内调用）。

        - 由连续失败阈值触发：沿用这一段连续失败的类别；
        - 由窗口失败率触发：取窗口内占多数的失败类别，平票时取最近一次失败
          （即窗口中更靠后的类别），纯超时/纯错误场景下结果与真实失败完全一致。
        """
        if self._consecutive_failures >= self._fail_threshold:
            for kind in reversed(self._window):
                if kind is not None:
                    return kind
        failures = [kind for kind in self._window if kind is not None]
        if not failures:
            return DEFAULT_FAILURE_KIND
        counts = Counter(failures)
        top = max(counts.values())
        winners = {k for k, v in counts.items() if v == top}
        if len(winners) == 1:
            return next(iter(winners))
        for kind in reversed(failures):
            if kind in winners:
                return kind
        return DEFAULT_FAILURE_KIND

    def reset(self) -> None:
        """清空全部判定状态（测试隔离用）。"""
        with self._lock:
            self._state = "closed"
            self._consecutive_failures = 0
            self._window.clear()
            self._opened_at = None
            self._trip_failure_kind = None

    def configure(
        self,
        fail_threshold: int,
        window_size: int,
        fail_ratio: float,
        cooldown_s: float,
    ) -> None:
        """更新判定参数并重置状态（启动装配 / 测试用）。"""
        if fail_threshold < 1 or window_size < 1:
            raise ValueError("fail_threshold and window_size must be >= 1")
        if not 0.0 < fail_ratio <= 1.0:
            raise ValueError("fail_ratio must be in (0, 1]")
        if cooldown_s < 0:
            raise ValueError("cooldown_s must be >= 0")
        with self._lock:
            self._fail_threshold = fail_threshold
            self._window_size = window_size
            self._fail_ratio = fail_ratio
            self._cooldown_s = cooldown_s
            self._window = deque(maxlen=window_size)
            self._state = "closed"
            self._consecutive_failures = 0
            self._opened_at = None
            self._trip_failure_kind = None


breaker = CircuitBreaker(
    fail_threshold=5,
    window_size=20,
    fail_ratio=0.5,
    cooldown_s=2.0,
)
