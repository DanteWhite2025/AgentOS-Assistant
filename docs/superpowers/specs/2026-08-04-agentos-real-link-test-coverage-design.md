---
title: AgentOS 真链路测试覆盖 100% 设计
date: 2026-08-04
status: approved
---

# AgentOS 真链路测试覆盖 100% 设计

## 一、目标

把「真链路」= AgentOS Staging Agent API 这条线的**所有需求**做到 pytest 化、
可度量、**行 + 分支 100% 覆盖**、**一次性全绿**，并进 CI。

覆盖口径（团队 2026-08-04 拍板，精准不外扩）：

- **数字门槛 100%**：`app/tools/agentos/`（client / models / trade_flow）。
- **场景全覆盖**：`/v1/agent/trade-draft` 接线（`app/api/agent.py` 该端点全部分支）
  + 白名单/executor 隔离（`app/tools/builtin.py` / `executor.py` 的 agentos 相关分支）。
- 其余占位模块**不设门槛**，避免误伤。

## 二、两条既定原则

1. **写操作不真打**（团队拍板）：`create_trade_draft` / `confirm` / `cancel`
   全部用 `httpx.MockTransport` 断言「请求构造 100% 符合 OpenAPI 契约」+ 错误信封解析；
   不产生真实副作用，保证「全部通过」不依赖未开通的写权限。
2. **只读真打**：`balances` / `positions` / `orders` 保留真链路，
   `@pytest.mark.skipif(无 AGENT_CORE_ACTOR_TOKEN)` 无 token 自动跳过（CI 默认跳过）。

## 三、测试架构（tests/ 新增 6 文件）

| 文件 | 覆盖面 | 关键场景 |
|---|---|---|
| `test_agentos_models.py` | `CreateTradeDraftRequest` 边界 | 枚举 side/order_type；Decimal 归一(int/float/str/Decimal)；非法 Decimal 多例/负数/`0`/空串；bool 挡 int；不支持类型；mandate 剔除/保留；reduce_only 非 bool；`to_body` |
| `test_agentos_client.py` | 6 操作构造 + 错误路径 | 6 操作 URL/method/header/body 全断言（**补 cancel**）；base_url 末尾斜杠归一；Idempotency-Key 未传自动生成(`idem_`前缀)/传了透传；confirm 只含 draft_version + X-Step-Up-Token；cancel 无 body；401/400/404/409/5xx `raise_for_status` 冒泡；错误信封 JSON 可解析 |
| `test_agentos_trade_flow.py` | 解析/建请求/卡片/端到端 | 围栏 JSON、缺必填、非 JSON、非对象、side 小写归一、可选字段省略；默认 leverage/slippage 注入；render_card 空 data/未知 side/缺字段兜底`—`；create_draft_from_text 透传 idempotency_key |
| `test_agentos_api.py` | `/trade-draft` 全分支 | 201 happy + 卡片 + trace_id + 审计 result_ref + Idempotency-Key 透传；404 SESSION_NOT_FOUND；403 TOOL_NOT_IN_SCOPE；503 未配置(INTERNAL)；400 IntentParseError(INVALID_REQUEST)；502 上游 HTTPStatusError(INTERNAL) |
| `test_agentos_registry.py` | 白名单/executor 隔离 | readonly/write 工具登记 external=True；is_allowed；executor 对 external 工具抛 UnknownToolError（防误走 core） |
| `test_agentos_live.py` | 真链路只读 | `skipif` 无 token；打真 staging balances/positions/orders |

## 四、技术选型

- HTTP mock 统一用 `httpx.MockTransport`（既有脚本一致、零新增运行时依赖）。
- 覆盖工具：`pytest-cov` 加入 dev 依赖。
- `[tool.pytest.ini_options]`：注册 `live` marker；`addopts` 默认排除 `live`，
  真链路覆盖门槛在 CI 用 `--cov=app.tools.agentos --cov-branch --cov-fail-under=100` 施加。

## 五、方法论

被测代码大多已存在，属「补测到既有代码」+ 缺口测试。写测试中若发现真 bug
（错误信封未解析、异常未映射等），按 TDD 就地补实现，确保一次性全绿。

## 六、清理

- 删 `scripts/smoke_agentos_flow.py`、`scripts/e2e_trade_draft.py`（断言已迁进 pytest）。
- 保留 `scripts/smoke_agentos_staging.py` 作手动真网测试入口。
