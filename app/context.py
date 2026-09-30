"""请求级关联标识（correlation id）的上下文传播。

使用 contextvars 保证：
- 同一条异步调用链（含 background tasks）内标识保持不变；
- 并发请求之间互不串号；
- 每个请求处理前重置，避免残留上一请求的上下文。
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    """返回当前上下文的关联标识，未设置时为 None。"""
    return _correlation_id.get()


def set_correlation_id(value: str) -> Token:
    """设置当前上下文的关联标识，返回 Token 供 reset 使用。"""
    return _correlation_id.set(value)


def reset_correlation_id(token: Token) -> None:
    """按 Token 恢复上下文，确保请求结束后不残留。"""
    _correlation_id.reset(token)


def new_correlation_id() -> str:
    """生成一个新的关联标识（UUID4 十六进制，符合合法格式）。"""
    return uuid.uuid4().hex
