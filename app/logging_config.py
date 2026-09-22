"""结构化日志：字段稳定的 JSON 记录，自动附带请求标识。"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from .context import get_request_id

#: 结构化记录中保证存在的稳定字段
STABLE_FIELDS = ("timestamp", "level", "logger", "message", "request_id")


class RequestIdFilter(logging.Filter):
    """为每条记录注入 request_id 字段（来自上下文，无请求时为 "-"）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


class JsonFormatter(logging.Formatter):
    """输出单行 JSON，字段稳定；异常信息归入 error 字段。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        # 额外业务字段（通过 logger.extra 传入）合并进记录
        for key, value in record.__dict__.items():
            if key not in payload and not key.startswith("_") and key not in (
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "module",
                "msecs", "msg", "name", "pathname", "process", "processName",
                "relativeCreated", "stack_info", "thread", "threadName",
                "taskName",
            ):
                payload[key] = value
        if record.exc_info and record.exc_info[1] is not None:
            payload["error"] = {
                "type": type(record.exc_info[1]).__name__,
                "message": str(record.exc_info[1]),
            }
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO") -> None:
    """配置根 logger 输出结构化 JSON；重复调用不叠加 handler。"""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
