"""配置项：集中定义可观测性相关的常量与默认值，可用环境变量覆盖。

环境变量（前缀 OBS_）：
- OBS_CORRELATION_ID_HEADER   关联标识请求头名，默认 X-Correlation-ID
- OBS_CORRELATION_ID_PATTERN  合法标识正则，默认 ^[A-Za-z0-9-]{8,64}$
- OBS_LOG_LEVEL               日志级别，默认 INFO
- OBS_SERVICE_NAME            服务名，默认 fastapi-observability-python
- OBS_DEPENDENCY_MODE         模拟依赖初始模式 ok/timeout/error，默认 ok
- OBS_DEPENDENCY_LATENCY_MS   模拟依赖正常耗时（毫秒），默认 5.0
- OBS_ARCHIVE_MAX_RECORDS     请求观测档案留存上限（条），默认 1000
- OBS_BREAKER_FAIL_THRESHOLD  连续失败多少次触发降级，默认 5
- OBS_BREAKER_WINDOW_SIZE     失败率统计窗口（最近多少次调用），默认 20
- OBS_BREAKER_FAIL_RATIO      窗口内失败占比达到多少触发降级，默认 0.5
- OBS_BREAKER_COOLDOWN_S      降级后多久放行一次试探调用（秒），默认 2.0
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """服务配置。"""

    correlation_id_header: str = "X-Correlation-ID"
    # 合法标识：8~64 位，仅字母数字与 '-'
    correlation_id_pattern: str = r"^[A-Za-z0-9-]{8,64}$"
    log_level: str = "INFO"
    service_name: str = "fastapi-observability-python"
    # 模拟下游依赖的初始模式：ok / timeout / error
    dependency_mode: str = "ok"
    dependency_latency_ms: float = 5.0
    # 请求观测档案留存上限（超过后按 FIFO 淘汰最早一条）
    archive_max_records: int = 1000
    # 熔断器：连续失败阈值 / 滑动窗口大小 / 窗口失败占比阈值 / 打开后冷却秒数
    breaker_fail_threshold: int = 5
    breaker_window_size: int = 20
    breaker_fail_ratio: float = 0.5
    breaker_cooldown_s: float = 2.0


def get_settings() -> Settings:
    """读取环境变量并返回 Settings。"""
    return Settings(
        correlation_id_header=os.getenv("OBS_CORRELATION_ID_HEADER", "X-Correlation-ID"),
        correlation_id_pattern=os.getenv("OBS_CORRELATION_ID_PATTERN", r"^[A-Za-z0-9-]{8,64}$"),
        log_level=os.getenv("OBS_LOG_LEVEL", "INFO"),
        service_name=os.getenv("OBS_SERVICE_NAME", "fastapi-observability-python"),
        dependency_mode=os.getenv("OBS_DEPENDENCY_MODE", "ok"),
        dependency_latency_ms=float(os.getenv("OBS_DEPENDENCY_LATENCY_MS", "5.0")),
        archive_max_records=int(os.getenv("OBS_ARCHIVE_MAX_RECORDS", "1000")),
        breaker_fail_threshold=int(os.getenv("OBS_BREAKER_FAIL_THRESHOLD", "5")),
        breaker_window_size=int(os.getenv("OBS_BREAKER_WINDOW_SIZE", "20")),
        breaker_fail_ratio=float(os.getenv("OBS_BREAKER_FAIL_RATIO", "0.5")),
        breaker_cooldown_s=float(os.getenv("OBS_BREAKER_COOLDOWN_S", "2.0")),
    )
