# 统计口径（DATA.md）

WorkBuddy Dashboard 所有指标均从本机只读数据计算，本文说明每个指标的来源与算法。

## 数据源

| 文件 | 读取方式 | 说明 |
|---|---|---|
| `~/.workbuddy/workbuddy.db` | sqlite3 只读 URI `mode=ro` + `query_only=ON` | 会话、用量、工作区、技能 |
| `~/.workbuddy/traces/**/*.jsonl` | 只读流式逐行解析 | Token、模型、工具、时段 |

可用 `WORKBUDDY_DB` / `WORKBUDDY_TRACES` 覆盖默认位置。

## Token 指标

Token 数据来自 traces 中 `generation` 类型的 `span_end` 事件。每次生成调用的 `toolOutput`
是一个 JSON 数组，元素含 `usage` 对象：

- `total` = 所有有效用量的 `usage.total_tokens` 之和。单条记录缺少有效总量时，使用该条记录的输入量与输出量之和。
- `input` = 所有 `usage.prompt_tokens` 之和
- `output` = 所有 `usage.completion_tokens` 之和
- `cached` = `usage.prompt_tokens_details.cached_tokens` 之和
- `reasoning` = `usage.completion_tokens_details.reasoning_tokens` 之和
- `requests` = 含有效 `usage` 的生成调用次数
- `peak` = 单次生成的最大 `total_tokens`
- `avg` = `total / requests`（四舍五入）
- `cache_hit_rate` = `cached / input × 100%`（保留 2 位小数）

生成记录使用 `spanId` 去重。一条生成记录中的用量数组可能包含多个模型响应，每个有效用量计为一次请求。

> 说明：`cached_tokens` 是 `prompt_tokens` 的子集（缓存命中的输入部分），
> 因此缓存命中率以 input 为分母。`total_tokens` 通常已包含输入与输出，
> 各分项用于结构性展示，不做相加校验。

## 趋势

- **每日**：按生成调用的 `startedAt` 转成当地时间后取日期（`YYYY-MM-DD`），
  在最早与最晚活跃日之间逐日补齐，无数据日补 0。
- **每月**：按日期前 7 位（`YYYY-MM`）聚合。
- **活跃天数**：每日 Token > 0 的天数。
- **活跃跨度**：最后活跃日 − 最早活跃日 + 1（天）。

## 模型

按 `usage` 元素中的 `model` 字段分组，统计各模型的 `total / requests / input / output / cached`，
按 Token 降序排列。

## 工具

统计 `type=function` 的 `span_end` 事件的 `name`，得到各工具调用次数。
`mcp_tools` 等 `type=custom` 的内部 span 不计入，避免淹没真实工具调用。

## 会话

来自 `sessions` 表（排除已删除），按会话标识关联 `session_usage` 的最新记录：

- 标题：`custom_title` 优先，否则 `title`
- 上下文占用采用 `used / size`。已用量非负且容量大于零时，展示计算所得百分比。
- 累计积分为 `credit_json` 中各模型积分之和。缺失或无效的积分记录在 JSON 中为 `null`，在 CSV 中为空单元格。有效的零积分记录显示为零。
- 时间：毫秒时间戳转当地时间

## 项目总览

按 `sessions.cwd` 的完整绝对路径分组，以末级目录名显示项目，并附完整路径。工作区登记状态依据 `workspaces.path` 的完整路径精确匹配。同名目录按各自路径统计，未登记目录参与统计，无有效目录的会话单列。

日期筛选按会话创建日期计算新增会话。从所选会话汇总状态、后台自动化会话数、会话登记模型、最近活动及累计积分。项目上下文占用为有效记录的已用量之和除以这些记录的容量之和。

## 项目用量与导出

追踪记录中的 `trace.sessionId` 与未删除会话的 `sessions.id` 精确关联。通过 `traceId` 归集生成和工具事件，会话标识可在事件之后出现。缺少关联、关联会话已删除或会话无有效目录的事件统一计入「未归属项目」。

项目用量按事件的 `startedAt` 统计，沿用全局 Token、实际调用模型、工具和请求热力图的口径。请求、累计 Token 和工具调用分别计算归属覆盖率，分母是同一版本和日期范围的全局对应总量。分母为零时，覆盖率为空值。

项目页与 CSV、JSON 导出使用同一统计接口。接口接受可省略的 `from` 和 `to` 参数，格式为 `YYYY-MM-DD`，含起止日期，按服务所在时区计算。「今日实时」和 Trace 文件总数、状态仍按其原有范围统计。

## 技能

来自 `local_skills` 表：统计总数、启用数、可用户调用数，
按 `source` 分组得到来源分布；类型分布依据技能名称与描述关键词粗略归类。

## 活跃热力图

以每次生成请求的当地时间为准，按 `weekday（0=周一）× hour（0-23）` 累加，
形成 7×24 矩阵。颜色深浅按单元格相对最大值映射。

## 缓存机制

- 热数据（DB 查询 + 聚合）：进程内缓存 5 秒
- traces 全表扫描：缓存 30 秒，过期后台单飞刷新（单线程，不阻塞请求）

## 演示数据

`demo/` 下的数据由 `tools/make_demo.py` 生成，会话标题、路径、用量全部为虚构值，
不含任何真实信息。设 `WORKBUDDY_DEMO=1` 可强制使用。
