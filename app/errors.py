"""错误分类与对外状态码映射。

分类规则：
- BusinessRejected  业务规则拒绝        -> 422
- DependencyTimeout 下游依赖超时        -> 504
- DependencyError   下游依赖返回异常    -> 502
- UnexpectedError   未预期异常归一化    -> 500

对外响应只暴露 error_type / message / correlation_id，
内部细节（堆栈、原始异常）仅通过 cause 保留在服务端日志中。
"""
from __future__ import annotations


class AppError(Exception):
    """业务可预期错误基类，携带可对外暴露的 message 与仅供诊断的 cause。"""

    error_type: str = "internal_error"
    status_code: int = 500
    #: 对外是否展示 message（未预期异常不展示，避免泄露内部细节）
    expose_message: bool = True

    def __init__(self, message: str = "", *, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause


class BusinessRejected(AppError):
    """业务规则拒绝（如参数不满足业务约束）。"""

    error_type = "business_rejected"
    status_code = 422


class DependencyTimeout(AppError):
    """下游依赖超时。"""

    error_type = "dependency_timeout"
    status_code = 504


class DependencyError(AppError):
    """下游依赖返回异常。"""

    error_type = "dependency_error"
    status_code = 502


class UnexpectedError(AppError):
    """未预期异常归一化后的类型，对外不暴露内部 message。"""

    error_type = "unexpected_error"
    status_code = 500
    expose_message = False


def normalize_exception(exc: BaseException) -> AppError:
    """把任意异常归一化为 AppError，原始异常挂到 cause 上供诊断。"""
    if isinstance(exc, AppError):
        return exc
    return UnexpectedError("internal error", cause=exc)


def to_error_payload(exc: AppError, correlation_id: str | None = None) -> dict:
    """把异常映射为对外响应体，不泄露内部细节。"""
    return {
        "error": {
            "type": exc.error_type,
            "message": exc.message if exc.expose_message else "internal error",
            "correlation_id": correlation_id,
        }
    }


def classify_outcome(status_code: int) -> str:
    """按最终状态码把请求结果归入统一口径（档案 outcome 字段用）。

    成功 / 业务被拒 / 依赖超时 / 依赖出错 / 兜底未预期错误；
    其余 4xx（如 404、405）归为 client_error。与错误分类状态码映射保持一致。
    """
    if status_code < 400:
        return "success"
    if status_code == 504:
        return "dependency_timeout"
    if status_code == 502:
        return "dependency_error"
    if status_code == 422:
        return "business_rejected"
    if status_code < 500:
        return "client_error"
    return "unexpected_error"


def error_type_for_status(status_code: int) -> str | None:
    """按状态码反查错误分类（与 AppError 子类一一对应），成功/4xx 其余返回 None。"""
    return {
        422: "business_rejected",
        504: "dependency_timeout",
        502: "dependency_error",
        500: "unexpected_error",
    }.get(status_code)
