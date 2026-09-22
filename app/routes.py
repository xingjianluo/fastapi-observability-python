"""路由：业务端点、健康检查、指标查询、依赖状态控制（本地验证用）。"""
from __future__ import annotations

import re

from fastapi import APIRouter, BackgroundTasks

from .context import get_request_id
from .dependencies import dependency
from .errors import BusinessRejected
from .logging_config import get_logger
from .metrics import registry

logger = get_logger("app.routes")
router = APIRouter()

#: 业务约束：key 只允许小写字母/数字/连字符，否则业务拒绝
KEY_PATTERN = re.compile(r"^[a-z0-9-]+$")

#: 后台任务观察到的请求标识（本地验证与测试用）
background_observations: list[dict] = []


def _background_audit(key: str) -> None:
    """后台任务：验证请求标识在后台链路中保持不变。"""
    observed = get_request_id()
    background_observations.append({"key": key, "request_id": observed})
    logger.info("background task done", extra={"stage": "background", "key": key})


@router.get("/items/{key}")
async def get_item(key: str, background_tasks: BackgroundTasks) -> dict:
    """业务端点：校验 -> 调用模拟依赖 -> 派发后台任务。"""
    logger.info("handling item request", extra={"stage": "handler", "key": key})
    if not KEY_PATTERN.match(key):
        raise BusinessRejected(
            "key rejected by business rule",
            detail=f"key={key!r} must match {KEY_PATTERN.pattern}",
        )
    data = await dependency.fetch(key)
    background_tasks.add_task(_background_audit, key)
    return {"data": data, "request_id": get_request_id()}


@router.get("/health")
async def health() -> dict:
    """健康检查：聚合模拟依赖状态，区分 ok / degraded / down。"""
    dep = dependency.health()
    status = {"ok": "ok", "degraded": "degraded", "down": "down"}[dep]
    return {"status": status, "components": {"dependency": dep}}


@router.get("/metrics")
async def metrics() -> dict:
    """返回进程内指标快照。"""
    return registry.snapshot()


@router.post("/admin/dependency/{mode}")
async def set_dependency_mode(mode: str) -> dict:
    """切换模拟依赖状态，供本地验证。"""
    try:
        dependency.set_mode(mode)
    except ValueError as exc:
        raise BusinessRejected(
            "unknown dependency mode",
            detail=f"mode={mode!r}, expected one of ok/degraded/down",
        ) from exc
    logger.info("dependency mode switched", extra={"stage": "admin", "mode": mode})
    return {"dependency_mode": dependency.mode}


@router.get("/admin/boom")
async def boom() -> dict:
    """触发一个未预期异常，用于验证 500 归一化（本地验证/测试用）。"""
    raise RuntimeError("unexpected simulated crash with internals")
