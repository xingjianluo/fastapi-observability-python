"""配置项：请求标识规则、日志与依赖模拟开关。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 合法的请求标识：8-64 位十六进制或标准 UUID 形式，便于人工辨识与日志检索
REQUEST_ID_PATTERN = re.compile(r"^[0-9a-fA-F-]{8,64}$")


@dataclass
class Settings:
    """服务配置，全部可用默认值本地运行。"""

    request_id_header: str = "X-Request-ID"
    request_id_pattern: re.Pattern[str] = field(default=REQUEST_ID_PATTERN)
    log_level: str = "INFO"
    #: 模拟依赖的初始状态：ok / degraded / down
    dependency_mode: str = "ok"
    #: 模拟依赖的基础延迟（秒）
    dependency_latency: float = 0.01
    #: 模拟依赖超时阈值（秒），超过视为依赖超时
    dependency_timeout: float = 0.5


settings = Settings()
