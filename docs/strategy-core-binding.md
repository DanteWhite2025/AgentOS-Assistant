# Runtime ↔ Core StrategyBinding 契约

更新日期：2026-08-11

## 权威边界

| 对象 | 权威方 | Runtime 用途 |
|---|---|---|
| StrategyContainer、模板参数、信号 | Runtime | 编排、展示、本地确定性预检 |
| StrategyBinding | Core | 策略与 Agent 账户、Venue、Mandate、授权的唯一绑定关系 |
| ExecutionMandate / AuthorizationGrant | Core | 额度、标的、杠杆、有效期与授权最终判定 |
| TradeDraft / Order / Fill / Position / Settlement | Core | 金融记录、状态机、执行和归因 |

Runtime 本地检查可以比 Core 更严格，但不能放宽 Core 的限制，也不能把本地
`StrategyContainer` 当成资金授权。

## Draft 创建流程

1. Runtime 根据当前本地所有者读取 `StrategyContainer`，要求本地状态可交易。
2. Runtime 携带 `AGENT_CORE_ACTOR_TOKEN` 调用：

   `GET /internal/v1/strategy-bindings/{strategy_id}`

3. Core 从 Token 派生 `user_id + agent_id`，只返回该 Agent 可见的绑定。
4. Runtime 要求 Binding 状态为 `ACTIVE`，并读取：
   `agent_trading_account_id`、`venue_account_id`、`mandate_id`、
   `authorization_grant_id`、`capital_limit_minor`。
5. Runtime 调用 `GET /internal/v1/agent-trading-accounts`，按
   `agent_trading_account_id` 精确匹配账户，并交叉校验 `venue_account_id`。
6. Runtime 解析交易意图并执行本地策略预检。
7. Runtime 调用 `POST /internal/v1/trade-drafts`，请求必须包含：

```json
{
  "strategy_id": "strategy_example",
  "unified_account_id": "uac_example",
  "venue_account_id": "vac_example",
  "mandate_id": "mnd_example",
  "symbol": "BTC",
  "side": "BUY",
  "order_type": "LIMIT",
  "quantity": "0.001",
  "limit_price": "60000",
  "leverage": "2",
  "reduce_only": false,
  "max_slippage": "0.005"
}
```

8. Core 重新校验 Token、StrategyBinding、账户、Mandate、授权、额度和风险；V1
   为 `ALWAYS_CONFIRM`，Runtime 不能替用户确认。

## 必须拒绝

- Core Binding 不是 `ACTIVE`；
- Core 返回的 `strategy_id` 与请求不一致；
- Binding 对应的 Agent Trading Account 不可见或不是 `ACTIVE`；
- Binding 与账户的 `venue_account_id` 不一致；
- Binding 缺少 Mandate、AuthorizationGrant 或正数 `capital_limit_minor`；
- 客户端或 LLM 试图提交 `user_id`、账户 ID、Mandate ID 或授权关系。

Runtime 不会回退到“取第一个 ACTIVE 账户”。这能防止同一 Agent 的多个策略误用
另一个策略的账户或授权。

## Symbol 与环境

- 接口提交 canonical symbol：`BTC`；页面可展示：`BTC/USDC`。
- 当前交易执行：Hyperliquid Testnet。
- 行情读取：Hyperliquid Mainnet。
- Runtime 不直连 Hyperliquid 写接口。

## 当前未解决边界

Runtime 的旧策略管理 REST 接口仍使用 MVP `X-User-Id / X-Agent-Id` 头识别本地
StrategyContainer。Core 会在 Draft 创建时阻止跨账户与跨 Agent 资金操作，但这些旧头本身
不是生产级身份凭据。下一安全切片应把策略管理接口切换到已验证的 Core USER 身份，并从
服务端配置解析 Agent，而不是信任客户端头。
