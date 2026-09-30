"""按请求标识回查的请求观测档案（有上限、FIFO 淘汰、并发安全）。

- 每个真正返回了响应的请求在出口处留一份 RequestRecord，按 correlation_id 精确检索；
- 容量有界：超过上限按「最早完成入档」的顺序淘汰最老一条（FIFO，可复现）；
- 同一标识重复入档时以最新一条为准（先删旧键再追加到队尾，保持“较新记录后淘汰”）；
- 淘汰只影响老记录能否回查，不触碰指标计数 / 耗时统计 / 健康检查口径；
- 所有读写均在同一把锁内完成，并发写入不丢失、不重复（同一标识不产生两条）。
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RequestRecord:
    """单个请求的观测档案。

    correlation_id        请求标识（响应头 X-Correlation-ID 中同一个值）
    correlation_id_source client=调用方提供且合法；generated=服务端补生成
    method                请求方法
    route                 命中的路由模板（未命中路由时为原始路径）
    status_code           最终响应状态码
    outcome               最终结果归类：success/business_rejected/
                          dependency_timeout/dependency_error/unexpected_error
    error_type            错误分类（成功为 None）
    latency_ms            服务端处理耗时（毫秒）
    started_at            请求开始时间（ISO8601 UTC）
    """

    correlation_id: str
    correlation_id_source: str
    method: str
    route: str
    status_code: int
    outcome: str
    error_type: str | None
    latency_ms: float
    started_at: str

    def to_dict(self) -> dict:
        return asdict(self)


class RequestArchive:
    """有界请求档案：按 correlation_id 检索，超容量按 FIFO 淘汰最早一条。"""

    def __init__(self, max_records: int) -> None:
        if max_records < 1:
            raise ValueError("max_records must be >= 1")
        self._lock = threading.Lock()
        self._max = max_records
        self._records: OrderedDict[str, RequestRecord] = OrderedDict()

    def save(self, record: RequestRecord) -> None:
        """入档一条请求记录；超容量时淘汰最老记录，直到不超过上限。"""
        with self._lock:
            # 同标识旧记录先弹出：保证恰好一条，并让新记录排到队尾（更新完成顺序）
            self._records.pop(record.correlation_id, None)
            self._records[record.correlation_id] = record
            while len(self._records) > self._max:
                self._records.popitem(last=False)

    def get(self, correlation_id: str) -> RequestRecord | None:
        """按标识精确取一条；不存在（含已被淘汰）返回 None。"""
        with self._lock:
            return self._records.get(correlation_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def resize(self, max_records: int) -> None:
        """调整容量上限（配置/测试用）；缩容时立即按 FIFO 淘汰到新上限。"""
        if max_records < 1:
            raise ValueError("max_records must be >= 1")
        with self._lock:
            self._max = max_records
            while len(self._records) > self._max:
                self._records.popitem(last=False)

    def reset(self) -> None:
        """清空档案（测试隔离用）。"""
        with self._lock:
            self._records.clear()


archive = RequestArchive(1000)
