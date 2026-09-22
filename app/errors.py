"""异常归一化：业务/依赖/未预期异常 -> 稳定错误类型与对外状态码。"""
from __future__ import annotations

from enum import StrEnum


class ErrorType(StrEnum):
    """对外可区分的错误类型。"""

    BUSINESS_REJECTED = "business_rejected"
    DEPENDENCY_TIMEOUT = "dependency_timeout"
    DEPENDENCY_FAILURE = "dependency_failure"
    INTERNAL_ERROR = "internal_error"


#: 错误类型 -> HTTP 状态码映射（集中定义，便于文档与测试对齐）
ERROR_STATUS_MAP: dict[ErrorType, int] = {
    ErrorType.BUSINESS_REJECTED: 422,
    ErrorType.DEPENDENCY_TIMEOUT: 504,
    ErrorType.DEPENDENCY_FAILURE: 502,
    ErrorType.INTERNAL_ERROR: 500,
}


class AppError(Exception):
    """可归一化的应用异常基类。message 对外安全，detail 仅供诊断。"""

    error_type: ErrorType = ErrorType.INTERNAL_ERROR

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class BusinessRejected(AppError):
    """业务规则拒绝（如参数不满足业务约束）。"""

    error_type = ErrorType.BUSINESS_REJECTED


class DependencyTimeout(AppError):
    """下游依赖超时。"""

    error_type = ErrorType.DEPENDENCY_TIMEOUT


class DependencyFailure(AppError):
    """下游依赖返回异常。"""

    error_type = ErrorType.DEPENDENCY_FAILURE


def normalize(exc: Exception) -> AppError:
    """把任意异常归一化为 AppError，保留原始异常供诊断。"""
    if isinstance(exc, AppError):
        return exc
    return AppError("internal error", detail=repr(exc))
