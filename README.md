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

## 观测档案（按请求标识回查）

服务端为最近处理过的请求各留一份观测档案，端点：

```
GET /observability/requests/{correlation_id}
```

- 命中返回 `200` 与该标识**唯一一条**档案；查不到（从未出现或已被淘汰）返回
  `404`，错误体 `error.type=request_record_not_found`（并回带被查询的标识）。
- 任何一个真正返回了响应的请求——成功、业务被拒（422）、依赖超时（504）、
  依赖出错（502）、最后兜底（500）、快速失败、甚至 404——都能用**响应里
  `X-Correlation-ID` 头出现的那个标识**恰好查回一条；调用方没给或给得不合法、
  由服务端补出来的标识同样可查（`id_source=generated`）。

档案字段：

| 字段 | 说明 |
| --- | --- |
| `correlation_id` | 请求关联标识（回查键，与响应头/响应体/日志中的值一致） |
| `id_source` | 标识来源：`provided`（调用方提供且合法）/ `generated`（服务端补） |
| `method` | HTTP 方法 |
| `route` | 命中的路由模板（如 `/items/{item_id}`；未匹配路由时为实际路径） |
| `status_code` | 最终响应状态码 |
| `error_type` | 错误分类（成功为 `null`）：`business_rejected` / `dependency_timeout` / `dependency_error` / `unexpected_error` |
| `latency_ms` | 请求总耗时（毫秒，从进入最外层中间件到发出响应） |
| `fast_failed` | 是否走了熔断快速失败路径（未真正等待下游超时） |
| `recorded_at` | 留档时间（ISO8601 UTC） |

### 留存与淘汰规则（确定、可复现）

- 容量固定为 `OBS_ARCHIVE_MAX_RECORDS`（默认 1000），**不会随运行时间无限增长**。
- 新记录写入时若已满，淘汰**最久未更新**的一条（FIFO，结构等价于有界
  OrderedDict，从头部淘汰）。
- 同一标识再次写入（调用方复用同一标识）会**覆盖**旧档案并把该条移到最新位置，
  因此按标识回查始终至多一条，且为最新那次请求的结果。
- 所有读写在同一把锁内完成：并发写入不丢失、不产生重复键；读取为一致性快照。
- 淘汰只影响老记录能否回查，**不触碰**请求计数、耗时统计与健康检查口径
  （三者由独立的指标注册表与熔断器维护）。

## 健康检查与自动降级（熔断）

`GET /health` 同时反映**当下依赖模式**与**熔断器的近期表现**，取两者更差者：

- `dependency`：当下模式映射（`healthy`/`degraded`/`unavailable`）；
- `circuit`：熔断器状态（`healthy`/`degraded`/`unavailable`）；
- `circuit_detail`：熔断器快照（状态、连续失败数、窗口样本/失败数/失败占比、
  触发错误分类、冷却剩余秒数）。

| 综合状态 | status | HTTP |
| --- | --- | --- |
| 两者都 healthy | `ok` | 200 |
| 任一 degraded | `degraded` | 200 |
| 任一 unavailable（熔断 open） | `unavailable` | 503 |

熔断器（`app/resilience.py`，全部状态变更在同一把锁内，并发下不会来回抖动）：

- **关闭 closed**：正常放行。每次真实依赖调用结果写入最近 `OBS_CB_WINDOW_S`
  秒的时间窗口，满足任一条件即熔断为 open：
  1. 连续失败达到 `OBS_CB_FAILURE_THRESHOLD`（默认 5）次；
  2. 窗口内样本数达到 `OBS_CB_MIN_SAMPLES`（默认 5），且失败占比
     `>= OBS_CB_FAILURE_RATIO`（默认 0.5）。
  业务拒绝（不调用依赖）不计入，未预期的本地异常也不计入——只统计真实依赖结果。
- **开放 open**：新请求在进入依赖调用前**立即快速失败**，不再一个个干等到超时。
  快速失败沿用触发熔断时的那类错误分类与状态码（依赖超时触发 →
  `504 dependency_timeout`；依赖错误触发 → `502 dependency_error`），对外响应体
  仍是统一错误信封，message 为通用文案，不泄露任何下游内部细节；档案中
  `fast_failed=true`。
- **半开 half-open**：open 持续 `OBS_CB_OPEN_COOLDOWN_S`（默认 5 秒）后，下一个
  请求被放行作为**唯一探测**（探测未返回期间其余请求继续快速失败）。探测成功 →
  回 closed 并重新累计窗口（正常流量恢复后自动回到正常）；探测失败 → 立即回
  open 并重新计时。

熔断状态变化有结构化日志：`circuit.opened` / `circuit.half_open` /
`circuit.closed`。

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OBS_CORRELATION_ID_HEADER` | `X-Correlation-ID` | 关联标识请求头名 |
| `OBS_CORRELATION_ID_PATTERN` | `^[A-Za-z0-9-]{8,64}$` | 合法标识正则 |
| `OBS_LOG_LEVEL` | `INFO` | 日志级别 |
| `OBS_SERVICE_NAME` | `fastapi-observability-python` | 服务名 |
| `OBS_DEPENDENCY_MODE` | `ok` | 模拟依赖初始模式 `ok`/`timeout`/`error` |
| `OBS_DEPENDENCY_LATENCY_MS` | `5.0` | 模拟依赖正常耗时（毫秒） |
| `OBS_ARCHIVE_MAX_RECORDS` | `1000` | 观测档案最大留存条数，超过后淘汰最老一条 |
| `OBS_CB_FAILURE_THRESHOLD` | `5` | 连续失败多少次触发熔断 |
| `OBS_CB_WINDOW_S` | `30.0` | 失败占比统计的时间窗口（秒） |
| `OBS_CB_MIN_SAMPLES` | `5` | 窗口内参与占比计算的最少样本数 |
| `OBS_CB_FAILURE_RATIO` | `0.5` | 窗口失败占比阈值（达到即触发熔断） |
| `OBS_CB_OPEN_COOLDOWN_S` | `5.0` | 熔断开放后多久放行一个探测请求（秒） |

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
6. 按标识回查观测档案（用任一响应头里的 `X-Correlation-ID`）：
   ```bash
   curl -s -H 'X-Correlation-ID: demo-trace-0001' http://127.0.0.1:8000/items/1 >/dev/null
   curl -s http://127.0.0.1:8000/observability/requests/demo-trace-0001 | python -m json.tool
   curl -i http://127.0.0.1:8000/observability/requests/not-there-0001   # -> 404
   ```
   返回体包含 method/route/status_code/error_type/latency_ms/fast_failed/
   id_source/recorded_at（字段含义见上文“观测档案”）。
7. 熔断降级与快速失败（用小阈值+短冷却启动，便于观察）：
   ```bash
   OBS_CB_FAILURE_THRESHOLD=3 OBS_CB_OPEN_COOLDOWN_S=5 \
     uv run uvicorn app.main:app --reload
   curl -X POST -H 'Content-Type: application/json' -d '{"mode":"error"}' \
     http://127.0.0.1:8000/admin/dependency-mode
   for i in 1 2 3; do curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/items/1; done
   # 前 3 次为真实 502；之后立即再请求 -> 仍 502 dependency_error，但瞬间返回（快速失败）
   curl -i -H 'X-Correlation-ID: fastfail-demo-1' http://127.0.0.1:8000/items/1
   curl -s http://127.0.0.1:8000/health | python -m json.tool   # status=unavailable, circuit=open
   # 用该标识回查，fast_failed=true，且响应头/响应体/档案/日志标识一致
   curl -s http://127.0.0.1:8000/observability/requests/fastfail-demo-1 | python -m json.tool
   # 恢复依赖：等待冷却后第一个请求作为探测，成功即回正常
   curl -X POST -H 'Content-Type: application/json' -d '{"mode":"ok"}' \
     http://127.0.0.1:8000/admin/dependency-mode
   sleep 5
   curl -i http://127.0.0.1:8000/items/1    # 探测成功 -> 200
   curl -s http://127.0.0.1:8000/health | python -m json.tool   # status=ok, circuit=closed
   ```
8. 自动化验证：`uv run pytest -v -s`（用例日志打印输入与判定依据），
   覆盖：档案能查回/查不回、超上限淘汰、并发不丢不重、淘汰不影响指标与健康、
   连续失败降级与恢复、窗口占比降级、半开探测、快速失败口径与四处标识一致、
   业务拒绝不触发熔断。
