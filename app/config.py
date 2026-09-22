"""配置项：集中定义可观测性相关的常量与默认值，可用环境变量覆盖。

环境变量（前缀 OBS_）：
- OBS_CORRELATION_ID_HEADER   关联标识请求头名，默认 X-Correlation-ID
- OBS_CORRELATION_ID_PATTERN  合法标识正则，默认 ^[A-Za-z0-9-]{8,64}$
- OBS_LOG_LEVEL               日志级别，默认 INFO
- OBS_SERVICE_NAME            服务名，默认 fastapi-observability-python
- OBS_DEPENDENCY_MODE         模拟依赖初始模式 ok/timeout/error，默认 ok
- OBS_DEPENDENCY_LATENCY_MS   模拟依赖正常耗时（毫秒），默认 5.0
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


def get_settings() -> Settings:
    """读取环境变量并返回 Settings。"""
    return Settings(
        correlation_id_header=os.getenv("OBS_CORRELATION_ID_HEADER", "X-Correlation-ID"),
        correlation_id_pattern=os.getenv("OBS_CORRELATION_ID_PATTERN", r"^[A-Za-z0-9-]{8,64}$"),
        log_level=os.getenv("OBS_LOG_LEVEL", "INFO"),
        service_name=os.getenv("OBS_SERVICE_NAME", "fastapi-observability-python"),
        dependency_mode=os.getenv("OBS_DEPENDENCY_MODE", "ok"),
        dependency_latency_ms=float(os.getenv("OBS_DEPENDENCY_LATENCY_MS", "5.0")),
    )
