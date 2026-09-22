"""请求级关联标识的上下文传播（基于 contextvars）。"""
from __future__ import annotations

import uuid
from contextvars import ContextVar

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def new_request_id() -> str:
    """生成一个新的请求标识。"""
    return uuid.uuid4().hex


def set_request_id(value: str) -> object:
    """设置当前上下文的请求标识，返回可复位的 token。"""
    return _request_id.set(value)


def reset_request_id(token: object) -> None:
    """按 token 复位，避免上下文残留到下一个请求。"""
    _request_id.reset(token)  # type: ignore[arg-type]


def get_request_id() -> str:
    """读取当前请求标识；不存在时返回占位符，保证日志字段稳定。"""
    return _request_id.get() or "-"
