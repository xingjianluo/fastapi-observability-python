"""按请求标识（correlation id）留存与回查观测档案。

留存规则（确定且可复现）：
- 容量固定为 ``max_records``，新记录写入时若已满，淘汰"最久未更新"的一条；
- 同一标识再次写入（调用方复用同一标识）会覆盖旧档案，并把该条移动到最新位置，
  因此按标识回查始终至多命中一条（命中=最新那次请求）；
- 淘汰只发生在本存储内部，不触碰指标计数、耗时统计与健康检查口径；
- 全部读写在同一把锁内完成：并发写入不丢失、不产生重复键，读取为一致性快照。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestRecord:
    """单个请求的观测档案。"""

    #: 请求关联标识（也是回查键）
    correlation_id: str
    #: 标识来源：provided=调用方提供且合法；generated=服务端补生成
    id_source: str
    #: HTTP 方法
    method: str
    #: 命中的路由模板（未匹配路由时为实际路径）
    route: str
    #: 最终响应状态码
    status_code: int
    #: 错误分类（成功为 None；business_rejected/dependency_timeout/...）
    error_type: str | None
    #: 请求总耗时（毫秒）
    latency_ms: float
    #: 是否走了熔断快速失败路径（未真正调用下游、未等待超时）
    fast_failed: bool
    #: 档案记录时间（ISO8601 UTC）
    recorded_at: str

    def to_dict(self) -> dict:
        return {
            "correlation_id": self.correlation_id,
            "id_source": self.id_source,
            "method": self.method,
            "status_code": self.status_code,
            "route": self.route,
            "error_type": self.error_type,
            "latency_ms": round(self.latency_ms, 3),
            "fast_failed": self.fast_failed,
            "recorded_at": self.recorded_at,
        }


class RequestArchive:
    """有容量上限、按最老记录淘汰的线程安全请求档案。"""

    def __init__(self, max_records: int = 1000) -> None:
        if max_records <= 0:
            raise ValueError("max_records must be positive")
        self._lock = threading.Lock()
        self._records: OrderedDict[str, RequestRecord] = OrderedDict()
        self.max_records = max_records

    def save(self, record: RequestRecord) -> None:
        """留存一条档案。

        同键覆盖并移到末尾（最新）；超过容量时从头部淘汰最老记录。
        """
        with self._lock:
            if record.correlation_id in self._records:
                # 覆盖旧档案，重新计为"最新"
                self._records.pop(record.correlation_id)
            self._records[record.correlation_id] = record
            while len(self._records) > self.max_records:
                self._records.popitem(last=False)

    def get(self, correlation_id: str) -> RequestRecord | None:
        """按标识取回唯一一条档案；不存在（含已被淘汰）返回 None。"""
        with self._lock:
            record = self._records.get(correlation_id)
            return record

    def size(self) -> int:
        """当前留存条数。"""
        with self._lock:
            return len(self._records)

    def reset(self, max_records: int | None = None) -> None:
        """清空档案（测试隔离用），可选同时调整容量。"""
        with self._lock:
            self._records.clear()
            if max_records is not None:
                if max_records <= 0:
                    raise ValueError("max_records must be positive")
                self.max_records = max_records


archive = RequestArchive()
