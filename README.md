# fastapi-observability-python

基于 FastAPI 的可观测性示例服务：用进程内状态模拟下游依赖、指标后端与日志后端，
覆盖关联标识传播、结构化日志、异常归一化、请求指标与健康检查，以及按请求标识
回查的请求观测档案与依赖熔断（快速失败 + 自动恢复），全部可本地验证。

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
`correlation_id.rejected` / `dependency.mode_changed` / `request.archive_miss` /
`circuit.opened` / `circuit.half_open` / `circuit.closed` / `circuit.reopened`。

## 请求观测档案（按请求标识回查）

每个真正返回了响应的请求（成功、业务被拒、依赖超时、依赖出错、含走到最后一层
兜底的 500）在出口处由 `ArchiveMiddleware` 留一份档案。响应中的 `X-Correlation-ID`
就是回查键：

```bash
curl -i -H 'X-Correlation-ID: my-trace-001' http://127.0.0.1:8000/items/1
curl http://127.0.0.1:8000/observability/requests/my-trace-001
```

档案字段（`GET /observability/requests/{correlation_id}`，命中 200；不存在或已淘汰 404）：

| 字段 | 说明 |
| --- | --- |
| `correlation_id` | 请求标识，与响应头/响应体/日志中是同一个值 |
| `correlation_id_source` | `client`=调用方提供且合法；`generated`=未提供/非法由服务端补 |
| `method` | 请求方法 |
| `route` | 命中的路由模板（未命中路由时为原始路径） |
| `status_code` | 最终响应状态码 |
| `outcome` | 结果归类：`success` / `business_rejected` / `dependency_timeout` / `dependency_error` / `unexpected_error` / `client_error`（404/405 等） |
| `error_type` | 错误分类（成功与普通 4xx 为 `null`），与错误响应体 `error.type` 同口径 |
| `latency_ms` | 服务端处理耗时（毫秒） |
| `started_at` | 请求开始时间（ISO8601 UTC） |

留存与淘汰规则（确定、可复现）：

- 有界容量，默认最多留存 1000 条（`OBS_ARCHIVE_MAX_RECORDS`）；
- 超过上限时按**最早完成入档先淘汰**（FIFO）逐条淘汰，直到不超过上限；
- 同一标识重复入档时以最新一条为准（旧记录先移除、新记录排到队尾），
  因此用响应里的标识永远恰好查回一条，不会查不到（未淘汰时）、也不会多条；
- 淘汰只影响老记录能否回查，不影响请求计数、耗时统计与健康检查口径；
- 所有读写在同一把锁内完成，并发写入不丢失、不重复；归档是一次 O(1) 的
  内存操作，不会明显拖慢请求。

## 依赖熔断与快速失败

下游依赖调用由进程内熔断器（`app.resilience.CircuitBreaker`）保护，三种状态：

- `closed`（正常）：正常调用下游，每次结果计入滑动窗口；
- `open`（降级）：不再等待下游，请求**立即快速失败**；
- `half_open`（试探）：冷却后只放行一个试探请求，其余继续快速失败。

降级判定（满足任一即 `closed -> open`）：

1. 连续失败次数达到 `OBS_BREAKER_FAIL_THRESHOLD`（默认 5）；
2. 最近 `OBS_BREAKER_WINDOW_SIZE` 次（默认 20）**真实**调用中，失败占比
   达到 `OBS_BREAKER_FAIL_RATIO`（默认 0.5）。窗口攒满才判定，避免小样本抖动。

恢复判定（防抖动）：

- `open` 经过 `OBS_BREAKER_COOLDOWN_S`（默认 2.0s）后，下一个请求成为唯一试探；
- 试探成功 -> `closed`（清空连续计数与窗口），正常流量恢复；
- 试探失败 -> 立即重新 `open` 并重新计时，不会在正常/降级之间来回抖；
- 快速失败不是真实下游调用，不计入失败窗口，不会自我强化降级。

快速失败的对外表现（与现有口径完全一致、不泄露内部细节）：

- HTTP 502，错误响应体 `error.type = dependency_error`，
  不出现 `CircuitOpen`、下游内部原因等字样；
- 不再逐个干等到超时：依赖持续不可用时快速失败请求毫秒级返回；
- 关联标识从头到尾一致：响应头、错误响应体、结构化日志、回查档案是同一个值；
- 快速失败请求仍按 502 计入既有指标分组（每请求恰好计一次），档案照常留存。

健康检查联动（`GET /health`）：响应体新增 `circuit` 判定快照（状态、连续失败数、
窗口大小/失败数/失败率、冷却时长）；熔断 `open` 时依赖判为 `down`（503
unavailable），`half_open` 时判为 `degraded`，`closed` 时沿用模拟模式的口径。

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

`GET /health` 反映模拟依赖状态与熔断器状态：

| 依赖/熔断状态 | dependency | status | HTTP |
| --- | --- | --- | --- |
| 模式 `ok` 且熔断 `closed` | `healthy` | `ok` | 200 |
| 模式 `timeout` 且熔断 `closed`，或熔断 `half_open` | `degraded` | `degraded` | 200 |
| 模式 `error` 且熔断 `closed` | `down` | `unavailable` | 503 |
| 熔断 `open`（持续失败降级中） | `down` | `unavailable` | 503 |

响应体额外携带 `circuit` 字段：`state` / `consecutive_failures` /
`window_size` / `window_failures` / `window_fail_ratio` / `cooldown_s`，
可看出“最近是不是在持续出问题”及当前判定依据。

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OBS_CORRELATION_ID_HEADER` | `X-Correlation-ID` | 关联标识请求头名 |
| `OBS_CORRELATION_ID_PATTERN` | `^[A-Za-z0-9-]{8,64}$` | 合法标识正则 |
| `OBS_LOG_LEVEL` | `INFO` | 日志级别 |
| `OBS_SERVICE_NAME` | `fastapi-observability-python` | 服务名 |
| `OBS_DEPENDENCY_MODE` | `ok` | 模拟依赖初始模式 `ok`/`timeout`/`error` |
| `OBS_DEPENDENCY_LATENCY_MS` | `5.0` | 模拟依赖正常耗时（毫秒） |
| `OBS_ARCHIVE_MAX_RECORDS` | `1000` | 请求观测档案留存上限（条，超容量 FIFO 淘汰） |
| `OBS_BREAKER_FAIL_THRESHOLD` | `5` | 连续失败多少次触发降级 |
| `OBS_BREAKER_WINDOW_SIZE` | `20` | 失败率统计窗口（最近多少次真实调用） |
| `OBS_BREAKER_FAIL_RATIO` | `0.5` | 窗口攒满后失败占比达到多少触发降级 |
| `OBS_BREAKER_COOLDOWN_S` | `2.0` | 降级后多久放行一次试探调用（秒） |

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
6. 请求档案回查：
   ```bash
   # 成功请求：用响应头里的标识回查
   curl -i -H 'X-Correlation-ID: my-trace-001' http://127.0.0.1:8000/items/1
   curl http://127.0.0.1:8000/observability/requests/my-trace-001
   # 服务端补标识：从响应头 X-Correlation-ID 取值后同样可查
   curl -i http://127.0.0.1:8000/items/1
   # 查不到（含已被淘汰）：404 not_found
   curl -i http://127.0.0.1:8000/observability/requests/no-such-id
   ```
7. 熔断降级、快速失败与恢复（为便于观察，可调小阈值与冷却）：
   ```bash
   export OBS_BREAKER_FAIL_THRESHOLD=3 OBS_BREAKER_COOLDOWN_S=3
   uv run uvicorn app.main:app --reload
   # 依赖持续出错：连续打 3 次后熔断打开
   for i in 1 2 3; do
     curl -X POST -H 'Content-Type: application/json' \
       -d '{"mode":"error"}' http://127.0.0.1:8000/admin/dependency-mode
     curl -i -H "X-Correlation-ID: down-$i" http://127.0.0.1:8000/items/1
   done
   curl -i http://127.0.0.1:8000/health   # -> 503，circuit.state=open
   # 第 4 次起毫秒级快速失败，仍是 502 dependency_error，不等待超时
   curl -i -H 'X-Correlation-ID: fast-fail-1' http://127.0.0.1:8000/items/1
   curl http://127.0.0.1:8000/observability/requests/fast-fail-1
   # 依赖恢复；冷却结束后第一个请求试探成功，健康检查回到 ok
   curl -X POST -H 'Content-Type: application/json' \
     -d '{"mode":"ok"}' http://127.0.0.1:8000/admin/dependency-mode
   sleep 3
   curl -i http://127.0.0.1:8000/items/1
   curl -i http://127.0.0.1:8000/health   # -> 200，circuit.state=closed
   ```
8. 自动化验证：`uv run pytest -v -s`（用例打印输入与判定依据；
   `tests/test_archive.py` 覆盖可查回/查不回、FIFO 淘汰、并发不丢不重、
   各结果（含 500 兜底与快速失败）标识一致；`tests/test_resilience.py`
   覆盖连续失败降级、窗口失败率、半开试探恢复/失败保持、并发稳定与快速失败口径）。
