# fastapi-observability-python

基于 FastAPI 的可观测性示例服务：用进程内状态模拟下游依赖、指标后端与日志后端，
覆盖关联标识传播、结构化日志、异常归一化、请求指标与健康检查，全部可本地验证。

## 快速开始

```bash
uv sync                 # 安装依赖
uv run uvicorn app.main:app --reload   # 启动服务
uv run pytest -v -s     # 运行测试（-s 可看到每个用例打印的输入与判定依据）
```

## 关联标识（Correlation ID）约定

- 请求头：`X-Correlation-ID`（可通过 `OBS_CORRELATION_ID_HEADER` 修改）。
- 合法格式：正则 `^[A-Za-z0-9-]{8,64}$`（可通过 `OBS_CORRELATION_ID_PATTERN` 修改）。
- 处理规则（确定且可解释）：
  - 提供且合法 → 整条异步调用链（含后台任务）内原样沿用；
  - 未提供或非法 → 服务端生成 UUID4 hex 新标识，响应头附带
    `X-Correlation-ID-Generated: true`；非法输入会输出一条 WARNING 日志；
  - 每个请求处理结束后 reset 上下文，并发请求之间不串号、不残留。
- 响应头始终回写 `X-Correlation-ID`，错误响应体的 `error.correlation_id` 同样携带。

## 结构化日志字段约定

JSON 单行输出，每条记录固定字段：

| 字段 | 说明 |
| --- | --- |
| `timestamp` | ISO8601 UTC 时间 |
| `level` | 日志级别 |
| `logger` | logger 名称 |
| `message` | 日志消息 |
| `correlation_id` | 当前上下文关联标识（自动注入） |

业务上下文通过 `extra_fields` 平铺到顶层（如 `event`、`item_id`、`error_type`），
异常记录附带 `exception` 堆栈字段。关键事件：`item.received` / `item.completed` /
`item.audit`（后台任务）/ `request.failed` / `request.unexpected_error` /
`correlation_id.rejected` / `dependency.mode_changed`。

## 错误分类与状态码映射

| 错误类型 (`error.type`) | 含义 | HTTP 状态码 |
| --- | --- | --- |
| `business_rejected` | 业务规则拒绝（如 item_id 非正数） | 422 |
| `dependency_timeout` | 下游依赖超时 | 504 |
| `dependency_error` | 下游依赖返回异常 | 502 |
| `unexpected_error` | 未预期异常（归一化后） | 500 |

错误响应体统一为：

```json
{"error": {"type": "...", "message": "...", "correlation_id": "..."}}
```

- 对外不泄露内部细节：`unexpected_error` 的 message 固定为 `internal error`，
  依赖异常的内部原因不出现在响应中；
- 原始异常通过 `exc.cause` 与服务端日志（`exc_info` / `cause` 字段）保留，供诊断。

## 指标

进程内 `MetricsRegistry` 按 `(method, route 模板, status_code)` 统计：

- `count`：请求数（锁保证并发不丢失、每请求恰好计一次，异常计入 500）；
- `latency_ms`：`total` / `avg` / `max`。

通过 `GET /metrics` 获取一致性快照。

## 健康检查

`GET /health` 反映模拟依赖状态：

| 依赖模式 | dependency | status | HTTP |
| --- | --- | --- | --- |
| `ok` | `healthy` | `ok` | 200 |
| `timeout` | `degraded` | `degraded` | 200 |
| `error` | `down` | `unavailable` | 503 |

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OBS_CORRELATION_ID_HEADER` | `X-Correlation-ID` | 关联标识请求头名 |
| `OBS_CORRELATION_ID_PATTERN` | `^[A-Za-z0-9-]{8,64}$` | 合法标识正则 |
| `OBS_LOG_LEVEL` | `INFO` | 日志级别 |
| `OBS_SERVICE_NAME` | `fastapi-observability-python` | 服务名 |
| `OBS_DEPENDENCY_MODE` | `ok` | 模拟依赖初始模式 `ok`/`timeout`/`error` |
| `OBS_DEPENDENCY_LATENCY_MS` | `5.0` | 模拟依赖正常耗时（毫秒） |

## 本地验证方法

1. 正常请求与标识透传：
   ```bash
   curl -i -H 'X-Correlation-ID: my-trace-001' http://127.0.0.1:8000/items/1
   ```
2. 非法标识被替换（响应头出现 `X-Correlation-ID-Generated: true`）：
   ```bash
   curl -i -H 'X-Correlation-ID: !!bad!!' http://127.0.0.1:8000/items/1
   ```
3. 业务拒绝：`curl -i http://127.0.0.1:8000/items/-1` → 422。
4. 依赖异常归一化与健康检查联动：
   ```bash
   curl -X POST -H 'Content-Type: application/json' \
     -d '{"mode":"timeout"}' http://127.0.0.1:8000/admin/dependency-mode
   curl -i http://127.0.0.1:8000/items/1   # -> 504 dependency_timeout
   curl -i http://127.0.0.1:8000/health    # -> degraded
   curl -X POST -H 'Content-Type: application/json' \
     -d '{"mode":"error"}' http://127.0.0.1:8000/admin/dependency-mode
   curl -i http://127.0.0.1:8000/items/1   # -> 502 dependency_error
   curl -i http://127.0.0.1:8000/health    # -> 503 unavailable
   ```
5. 指标快照：`curl http://127.0.0.1:8000/metrics`。
6. 自动化验证：`uv run pytest -v -s`（用例日志打印输入与判定依据）。
