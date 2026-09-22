"""结构化日志：字段稳定的 JSON 输出。

每条记录固定包含字段：
  timestamp / level / logger / message / correlation_id
额外上下文通过 extra={"extra_fields": {...}} 传入，平铺到顶层。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.context import get_correlation_id

RESERVED = {"timestamp", "level", "logger", "message", "correlation_id"}


class JsonFormatter(logging.Formatter):
    """把 LogRecord 序列化为单行 JSON，字段稳定。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": get_correlation_id(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                if key not in RESERVED:
                    payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """配置 root logger 输出 JSON 结构化日志（幂等）。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
