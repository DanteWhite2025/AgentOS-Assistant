---
title: 策略容器 / 沙箱设计
date: 2026-08-08
status: approved
branch: feat/strategy-container
---

# 策略容器 / 沙箱设计

## 一、背景与目标

同事（Bruce @OrderbookTrade）与团队敲定的模型：**用户 → 多个 Agent → 每个 Agent 多个策略**；
Core 官方提供**策略模板**。本里程碑在 `yesup-agent-runtime` 侧「把策略容器搭建起来」：
每个策略是一个隔离的沙箱，有独立的额度/容量/风控/有效期/运行状态，一人一 Agent 可并行多策略。

范围（团队确认）：**容器框架 + 接入现有交易链路**；策略玩法（算法）留可插拔桩，稳定性
（重连/超时/降级）留后续测试阶段。

## 二、架构决策（最终敲定）

- **隔离机制：1 个 Agent = 1 个 Core 账户（+ 该 Agent 的 Core mandate）。**
- **策略 = Runtime 侧的逻辑配额容器**，在 Agent 的单一 Core 账户内做资金分配与风控，
  **不再为每个策略向 Core 申请独立账户/mandate**。
- 隔离为**逻辑隔离**（runtime 记账 + 预检）；仓位/保证金的物理隔离由 Core 账户维度提供，
  Core 始终是资金与风控的权威。runtime 预检为镜像/护栏。
- Agent 身份从 Bearer token（`user_id + agent_id`）派生，账户经 `agent-trading-accounts`
  发现——**不信任客户端传入账户 id**（延续现有红线）。

## 三、领域模型 `StrategyContainer`

不可变 frozen dataclass；金额一律 `Decimal`（禁 float）。

| 字段 | 类型 | 含义 |
|---|---|---|
| `strategy_id` | str | `strat_<hex>`，runtime 生成 |
| `user_id` | str | 拥有者（从 token 派生）|
| `agent_id` | str | 所属 Agent（从 token 派生；1 Agent=1 Core 账户）|
| `name` | str | 策略名称 |
| `template_id` | str | 绑定 Core 官方策略模板 id（opaque，非空；Core 权威）|
| `runtime_env` | Literal["live","paper","backtest"] | 策略运行环境 |
| `allocation` | Decimal | 当前投入本金 |
| `capital_capacity` | Decimal | 本金容量（个人本金上限）|
| `strategy_capacity` | Decimal | 策略容量（含跟单的总池子上限）|
| `max_leverage` | Decimal | 杠杆上限 |
| `allowed_assets` | tuple[str,...] \| None | 资产范围（None=不限）|
| `expires_at` | datetime \| None | 有效期（None=不过期）|
| `state` | StrategyState | 运行状态 |
| `created_at` / `updated_at` | datetime | 时间戳 |
| `version` | int | 乐观版本（每次变更 +1）|

**不变量**（构造即校验，违反抛 `ValueError`）：
- `0 ≤ allocation ≤ capital_capacity ≤ strategy_capacity`
- 金额非负；`max_leverage > 0`
- `runtime_env` 属合法枚举
- `template_id` / `name` 非空

**状态机 `StrategyState`**：`ACTIVE → PAUSED → ACTIVE`（可往返）；`ACTIVE|PAUSED → TERMINATED`（终态）；
`ACTIVE|PAUSED → EXPIRED`（到期自动，终态）。非法迁移抛 `IllegalStateTransition`。
不可变：迁移方法返回**新实例**（`version+1`、更新 `updated_at`）。

## 四、模块拆分（`app/strategy/`，多小文件）

| 文件 | 职责 |
|---|---|
| `app/strategy/container.py` | `StrategyContainer` + `StrategyState` + 不变量 + 不可变迁移方法（`pause/resume/terminate/expire/with_allocation`）|
| `app/strategy/store.py` | `StrategyStore` Protocol（`create/get/update/delete/list_by_agent/list_by_user`）+ `InMemoryStrategyStore`（real 走 PG，配置即切换）|
| `app/strategy/manager.py` | `StrategyManager`：创建（发现 Agent 账户、不变量+聚合额度校验、template/runtime_env 校验）、列表、迁移、`resolve_execution_context`（策略→Agent 账户+派生 mandate+状态/额度预检）|
| `app/api/strategy.py` | REST 端点（挂 `/v1/agent/strategies`）|

派生 mandate：`manager` 从容器配置生成 `AgentMandate`（`max_leverage`/`allowed_assets`/`expires_at`/
`max_notional=allocation`），复用现有 `PolicyEngine` 做每单预检。
玩法算法：定义 `StrategyLogic` Protocol（`decide(context) -> list[intent]`）先留桩，真算法下个里程碑接。

## 五、API 契约

前缀 `/v1/agent/strategies`；`user_id`/`agent_id` 从 token/会话派生，**不入 body**。统一错误信封 `{code,message,details,trace_id}`。

| # | Method / Path | 请求体 | 成功 | 主要错误 |
|---|---|---|---|---|
| 1 | `POST /v1/agent/strategies` | `{name, template_id, runtime_env, allocation, capital_capacity, strategy_capacity, max_leverage, allowed_assets?, expires_at?}` | 201 `StrategyContainer` | 400 不变量/枚举非法；409 聚合额度超 Agent 可用本金；403 无权限 |
| 2 | `GET /v1/agent/strategies` | — | 200 `[StrategyContainer]`（按 agent 过滤）| — |
| 3 | `GET /v1/agent/strategies/{id}` | — | 200 `StrategyContainer` | 404 不存在/非本 agent |
| 4 | `POST /v1/agent/strategies/{id}/pause` | — | 200 `StrategyContainer` | 404；409 非 ACTIVE |
| 5 | `POST /v1/agent/strategies/{id}/resume` | — | 200 `StrategyContainer` | 404；409 非 PAUSED/已过期 |
| 6 | `POST /v1/agent/strategies/{id}/terminate` | — | 200 `StrategyContainer` | 404；409 已终态 |
| 7 | `POST /v1/agent/strategies/{id}/allocate` | `{allocation}` | 200 `StrategyContainer` | 404；400 超 capital_capacity；409 非 ACTIVE |
| 8 | `POST /v1/agent/strategies/{id}/trade-draft` | `{text}` | 201 `TradeDraftCard` | 404；409 非 ACTIVE/过期；403 违反策略额度/杠杆；400 意图解析失败；502 上游 |

端点 8 = 在策略上下文内交易：`resolve_execution_context(id)` → Agent 账户(`unified/venue`) + 派生 mandate 预检 →
复用 `trade_flow.create_draft_from_text`（以 Agent 账户建单）。上游错误经现有 `_map_upstream_error` 映射。

## 六、执行接线与预检数据流

```
建策略 → 发现 Agent 账户(agent-trading-accounts) → 不变量+聚合额度校验 → 存储 → ACTIVE
策略交易 → 解析容器 → 校验 ACTIVE 且未过期 → 派生 mandate 过 PolicyEngine 预检(杠杆/资产/名义≤allocation)
        → create_draft_from_text(用 Agent 账户) → Core 权威风控 → 返回确认卡片
```

聚合额度：`manager` 校验「该 Agent 下所有 ACTIVE 策略 `allocation` 之和 ≤ Agent 账户可用本金」
（可用本金取自 `get_balances`；取不到则降级为仅按容器不变量预检，并 `log`）。

## 七、错误处理

- 找不到策略 / 非本 agent → 404
- 状态非法（暂停已暂停、终态再操作、交易时非 ACTIVE/已过期）→ 409
- 不变量违反（allocation>capital_capacity 等、聚合超额）→ 400/409
- 策略额度/杠杆/资产预检不过 → 403 `POLICY_VIOLATION`
- 上游 Core 错误 → 复用 `_map_upstream_error`（精细映射 + `upstream_code`/`retryable`）

## 八、测试与覆盖方案

**单元**
- `container`：全不变量违反用例（负值、allocation>capital_capacity、capital_capacity>strategy_capacity、非法 runtime_env、空 template/name、leverage≤0）；不可变迁移（合法往返 + 每种非法迁移抛错）；`version` 递增。
- `store`：CRUD；`list_by_agent`/`list_by_user` 隔离（A agent 看不到 B agent 的策略）；更新不改原实例。
- `manager`：创建（不变量 + 聚合额度 happy/超额 + template/runtime_env 校验 + 账户发现失败）；迁移编排；`resolve_execution_context`（ACTIVE 通过 / 暂停/过期拒 / 派生 mandate 预检拒）。
- 派生 mandate：字段映射正确、PolicyEngine 拒/放行。

**API**（真跑 FastAPI + mock store/agentos client）
- 8 个端点 happy + 全错误分支（400/403/404/409/502）；账户来自发现（非 body）；审计留痕。

**覆盖门槛**
- 把 `--cov=app.strategy` 纳入 CI，**行+分支 100%**（比照 `app.tools.agentos`）。

**live（可选脚手架，`--run-live`）**
- 建策略（绑真 Agent 账户）→ 策略上下文内真建草稿；默认 skip。

## 九、假设与边界

- Core 依赖（同事已确认模型）：1 Agent=1 Core 账户；策略为 runtime 逻辑容器；`template_id` 引用 Core 官方模板（runtime 视为 opaque，非空校验，未来可接模板目录端点校验）。
- 账户/mandate 由 Agent 维度提供（经发现）；策略不单独申请 Core 账户/mandate。
- 稳定性（重连/超时/降级）：留后续测试阶段专项，本里程碑不含。
- 玩法算法（网格/趋势/跟单执行）：`StrategyLogic` 留桩，下个里程碑接。
- 持久化：MVP `InMemoryStrategyStore` + Protocol；real 模式 PG 落库（后续）。

## 十、非目标

- 不实现具体策略算法、回测引擎、跟单撮合。
- 不在 runtime 侧创建/撤销 Core 账户或 mandate。
- 不做多 venue/多市场的策略（当前 testnet 单 venue）。
