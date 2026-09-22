# FastAPI 可观测性骨架

基于 FastAPI 的可本地验证可观测性能力：请求关联标识传播、结构化日志、
异常归一化、请求指标与健康检查。下游依赖、指标后端与日志后端均由进程内
状态模拟，无需任何外部服务即可验证。

## 快速开始

```bash
uv sync                 # 安装依赖（含 dev 依赖）
uv run uvicorn main:app --reload
uv run pytest -q        # 运行全部测试
```

## 请求标识（X-Request-ID）

- 请求头：`X-Request-ID`（可用 `Settings.request_id_header` 修改）。
- 合法格式：8–64 位十六进制字符或连字符（`^[0-9a-fA-F-]{8,64}$`，见
  `app/config.py` 的 `REQUEST_ID_PATTERN`）。
- 处理规则（确定且可解释）：
  - 提供合法标识 → 全链路（异步调用链与后台任务）原样沿用；
  - 未提供 → 生成新的 UUID hex；
  - 提供但格式非法 → 生成新标识替换，并输出一条 `stage=request_id` 的告警日志；
  - 请求结束按 token 复位上下文，并发请求之间不串号、不残留。
- 响应头与业务响应体中均带回 `request_id`，错误响应中同样包含，便于关联诊断。

## 结构化日志字段约定

每条日志为单行 JSON，稳定字段：

| 字段 | 说明 |
| --- | --- |
| `timestamp` | UTC ISO8601 时间 |
| `level` | 日志级别 |
| `logger` | logger 名称 |
| `message` | 日志消息 |
| `request_id` | 请求标识（无请求上下文时为 `-`） |

关键阶段通过 `stage` 字段区分：`handler`（业务处理）、`access`（请求完成，
含 `method/route/status_code/duration_seconds`）、`error`（错误，含
`error_type/error_detail`）、`background`（后台任务）、`admin`（管理操作）。

错误日志的 `error_detail` 保留原始错误（含异常类型与信息）供诊断；
该字段**不会**出现在对外响应中。

## 错误分类与状态码映射

| 错误类型 | 触发场景 | HTTP 状态码 |
| --- | --- | --- |
| `business_rejected` | 业务规则拒绝（如 key 不满足 `^[a-z0-9-]+$`） | 422 |
| `dependency_timeout` | 模拟依赖超时 | 504 |
| `dependency_failure` | 模拟依赖返回异常 | 502 |
| `internal_error` | 未预期异常 | 500 |

映射集中在 `app/errors.py` 的 `ERROR_STATUS_MAP`。对外响应只含
`error.type / error.message / error.request_id`，不泄露内部细节；
原始错误通过 `normalize()` 保留在日志中。

## 指标

进程内 `MetricsRegistry` 按 `(路由模板, 状态码)` 聚合：

- `requests_total`：请求计数（如 `/items/{key}|200`）；
- `duration_seconds_sum`：耗时总和。

全部写入走同一把锁，并发下不丢失、不重复；`GET /metrics` 返回快照。

## 健康检查

`GET /health` 聚合模拟依赖状态：

| 依赖状态 | 健康结论 | 含义 |
| --- | --- | --- |
| `ok` | `ok` | 正常 |
| `degraded` | `degraded` | 可用但延迟升高 |
| `down` | `down` | 不可用（调用返回 502/504） |

## 配置项（`app/config.py` 的 `Settings`）

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `request_id_header` | `X-Request-ID` | 标识请求头名 |
| `request_id_pattern` | `^[0-9a-fA-F-]{8,64}$` | 合法标识正则 |
| `log_level` | `INFO` | 日志级别 |
| `dependency_mode` | `ok` | 模拟依赖初始状态 |
| `dependency_latency` | `0.01` | 模拟依赖基础延迟（秒） |
| `dependency_timeout` | `0.5` | 依赖超时阈值（秒） |

## 本地验证方法

```bash
# 1. 正常请求，观察标识沿用与结构化日志
curl -i -H 'X-Request-ID: abcdef123456' http://localhost:8000/items/abc

# 2. 非法标识被替换（响应头为新生成标识）
curl -i -H 'X-Request-ID: !!!' http://localhost:8000/items/abc

# 3. 业务拒绝 -> 422 business_rejected
curl -i http://localhost:8000/items/BAD_KEY

# 4. 依赖降级/不可用，观察健康检查与错误归一化
curl -X POST http://localhost:8000/admin/dependency/degraded
curl http://localhost:8000/health          # -> degraded
curl -X POST http://localhost:8000/admin/dependency/down
curl http://localhost:8000/health          # -> down
curl -i http://localhost:8000/items/abc    # -> 502 dependency_failure
curl -i http://localhost:8000/items/x-timeout  # -> 504 dependency_timeout

# 5. 未预期异常 -> 500 internal_error（响应不泄露内部细节）
curl -i http://localhost:8000/admin/boom

# 6. 指标快照
curl http://localhost:8000/metrics

# 7. 自动化验证（并发标识隔离、非法标识、异常归一化、指标计数）
uv run pytest -q
```

## 目录结构

```
app/
  config.py         配置项与标识规则
  context.py        请求标识上下文（contextvars）
  logging_config.py 结构化 JSON 日志
  errors.py         错误分类与状态码映射
  metrics.py        进程内指标后端
  dependencies.py   进程内模拟下游依赖
  middleware.py     标识传播 / 异常归一化 / 指标 / 访问日志
  routes.py         业务、健康、指标、管理端点
main.py             应用装配
tests/              并发隔离、非法标识、异常归一化、指标与健康测试
```
