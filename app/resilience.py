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

快速失败不计入失败统计（没有真实调用），因此不会自我强化、也不会干扰恢复判定。

快速失败的对外口径与「触发降级的那一类真实失败」保持一致：超时打穿的降级快速
失败仍是 dependency_timeout / 504，下游出错打穿的降级快速失败仍是
dependency_error / 502。熔断器在同一把锁内记住最近一次真实失败的类别，
check_request 把「是否放行 + 快速失败应呈现的类别」作为一次原子判定返回，
并发下所有快速失败请求拿到同一类别，不会在 504/502 之间抖动。
"""
from __future__ import annotations

import threading
import time
from collections import deque

from app.logging_config import get_logger

logger = get_logger("app.resilience")

#: 真实下游失败类别：超时（对外 dependency_timeout / 504）
FAILURE_TIMEOUT = "timeout"
#: 真实下游失败类别：下游出错（对外 dependency_error / 502）
FAILURE_ERROR = "error"
_VALID_FAILURE_KINDS = (FAILURE_TIMEOUT, FAILURE_ERROR)


class CircuitOpen(Exception):
    """熔断打开时的快速失败信号（仅作为内部 cause）。

    对外不归一化为固定类别：响应分类由触发降级的真实失败类别决定
    （超时 -> dependency_timeout/504，出错 -> dependency_error/502）。
    """


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
        self._window: deque[bool] = deque(maxlen=window_size)
        self._opened_at: float | None = None
        #: 最近一次真实失败的类别（timeout/error），快速失败沿用同一对外口径
        self._last_failure: str | None = None

    @property
    def state(self) -> str:
        """当前状态：closed / open / half_open。"""
        with self._lock:
            return self._state

    @property
    def last_failure_kind(self) -> str | None:
        """最近一次真实失败类别（timeout/error），未发生过真实失败为 None。"""
        with self._lock:
            return self._last_failure

    def snapshot(self) -> dict:
        """返回判定依据的一致性快照（健康检查与测试用）。"""
        with self._lock:
            failures = sum(1 for ok in self._window if not ok)
            return {
                "state": self._state,
                "consecutive_failures": self._consecutive_failures,
                "window_size": len(self._window),
                "window_failures": failures,
                "window_fail_ratio": round(failures / len(self._window), 3) if self._window else 0.0,
                "cooldown_s": self._cooldown_s,
                "last_failure_kind": self._last_failure,
            }

    def allow_request(self) -> bool:
        """是否放行一次真实下游调用；open 超时后恰好放一个试探请求。"""
        allowed, _ = self.check_request()
        return allowed

    def check_request(self) -> tuple[bool, str | None]:
        """原子判定一次请求该放行还是快速失败。

        返回 (allowed, fast_fail_kind)：
        - allowed=True：放行真实下游调用，fast_fail_kind 为 None；
        - allowed=False：应快速失败，fast_fail_kind 为快速失败对外应呈现的
          失败类别（timeout/error），即触发降级/最近一次真实失败的类别。

        「放行与否 + 呈现类别」在同一把锁内一次性给出，避免并发下先问
        allow_request、再读失败类别的两步竞态（恰好在两步之间发生状态
        迁移或冷却试探失败时，可能读到不一致的类别）。
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
                return False, self._last_failure
            # half_open：试探请求在途，其余一律快速失败；类别沿用打开降级时的
            # 真实失败类别（试探失败重新 open 时该类别也会同步更新）
            return False, self._last_failure

    def record_success(self) -> None:
        """记录一次真实调用成功；试探成功则关闭熔断并恢复正常。"""
        with self._lock:
            was_half_open = self._state == "half_open"
            self._consecutive_failures = 0
            self._window.append(True)
            if was_half_open:
                self._state = "closed"
                self._window.clear()
                self._opened_at = None
                self._last_failure = None
                logger.info(
                    "circuit closed after successful probe",
                    extra={"extra_fields": {"event": "circuit.closed"}},
                )

    def record_failure(self, kind: str = FAILURE_ERROR) -> None:
        """记录一次真实调用失败；试探失败重新打开，closed 下按双条件判定是否降级。

        kind 为本次真实失败的类别（timeout/error），会被记住并用于后续快速
        失败的对外口径；半开试探失败时也同步更新，保证重新 open 后的快速
        失败与「刚发生的这次真实试探失败」口径一致，不跳变。
        """
        if kind not in _VALID_FAILURE_KINDS:
            raise ValueError(f"invalid failure kind: {kind!r}, expected one of {_VALID_FAILURE_KINDS}")
        with self._lock:
            self._last_failure = kind
            if self._state == "half_open":
                self._state = "open"
                self._opened_at = time.monotonic()
                logger.warning(
                    "circuit re-opened after failed probe",
                    extra={"extra_fields": {"event": "circuit.reopened", "failure_kind": kind}},
                )
                return
            self._consecutive_failures += 1
            self._window.append(False)
            if self._state == "closed" and self._should_trip():
                self._state = "open"
                self._opened_at = time.monotonic()
                logger.warning(
                    "circuit opened due to dependency failures",
                    extra={
                        "extra_fields": {
                            "event": "circuit.opened",
                            "failure_kind": kind,
                            "consecutive_failures": self._consecutive_failures,
                            "window_size": len(self._window),
                            "window_failures": sum(1 for ok in self._window if not ok),
                        }
                    },
                )

    def _should_trip(self) -> bool:
        """双条件：连续失败达阈值，或攒满窗口后失败占比达阈值。"""
        if self._consecutive_failures >= self._fail_threshold:
            return True
        if len(self._window) >= self._window_size:
            failures = sum(1 for ok in self._window if not ok)
            if failures / len(self._window) >= self._fail_ratio:
                return True
        return False

    def reset(self) -> None:
        """清空全部判定状态（测试隔离用）。"""
        with self._lock:
            self._state = "closed"
            self._consecutive_failures = 0
            self._window.clear()
            self._opened_at = None
            self._last_failure = None

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
            self._last_failure = None


breaker = CircuitBreaker(
    fail_threshold=5,
    window_size=20,
    fail_ratio=0.5,
    cooldown_s=2.0,
)
