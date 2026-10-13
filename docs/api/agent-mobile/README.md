# AgentOS Mobile ↔ Runtime 接口

当前实时语音主链路为：Flutter → 火山 RTC，Runtime 负责短期 Token 和
Start/StopVoiceChat。OpenAI Realtime 仅保留为显式兼容 Provider。

## 端点

| 方法 | 端点 | 鉴权 | 用途 |
|---|---|---|---|
| POST | `/v1/agent/sessions` | Core USER Token | 创建会话并获取短期 RTC Token |
| POST | `/v1/agent/sessions/{id}/voice/start` | Core USER Token | App 进房成功后启动 AI 任务，幂等 |
| POST | `/v1/agent/sessions/{id}/token` | Core USER Token | 刷新 RTC Token / rt_token |
| DELETE | `/v1/agent/sessions/{id}` | Core USER Token | 先 StopVoiceChat，再删除 Session |
| GET | `/v1/agent/sessions/{id}` | Core USER Token | 查询状态 |
| POST | `/v1/agent/messages` | Core USER Token | 文字查询；Token 仅请求内用于正式 Trading Readiness 解析 |
| POST | `/v1/agent/trade-draft` | Core USER Token | 提交 `session_id + strategy_id + text`，Runtime 按 Core StrategyBinding 创建 Draft |
| WS | `/v1/agent/stream?session_id={id}` | rt_token | 文本实时通道 |

`/messages` 与 `/trade-draft` 都不接受账户、Venue、Mandate 或授权 ID。Runtime 先用当前
Core USER Token 调 `/api/v1/me/trading-readiness`，取得规范化的 Unified Account、Venue
Margin、Agent Trading Account、Mandate 与 Strategy Binding；该 Token 只在当前请求内
使用，不写 Session。Runtime 随后只用该规范化 `unified_account_id` 和最小权限 AGENT
Token 调 `/internal/v1/account-summary`；不调用旧的 `account_summary`，也不通过 `user_id`
猜账户。创建 Draft 时再以最小权限 AGENT Token 读取
`strategy_btc_limit_v1` 的正式绑定并交叉校验。Core V1 `ALWAYS_CONFIRM`，返回
`core_draft_id` 后仍须由用户确认；Runtime 不确认、不执行。

额度响应分别保留 `venue_margin_minor`、`capital_limit_minor`、
`per_order_limit_minor`、`total_exposure_limit_minor` 与日额度的 limit/used/remaining。
Core 尚未提供总敞口剩余额度时，`total_exposure_remaining.status=NOT_AVAILABLE`；因此
`available_trade_notional_minor` 只表示保守上界，并带
`available_trade_notional_is_upper_bound=true`。`account-summary.balance` 是 Core Ledger，
不是 Hyperliquid Venue Equity；在缺少权威字段时 `venue_equity` 明确返回
`EQUITY_UNAVAILABLE / NOT_AVAILABLE`，不得从 balance、positions 或 orders 推算。

Runtime 的 Core Client 仅暴露只读接口和 `create_trade_draft`。它没有 confirm、
request-execution、订单撤单或 Step-Up Token 透传能力；确认和订单生命周期必须由 Mobile
直接调用 Core USER 接口。

## 凭证包

```json
{
  "session_id": "sess_...",
  "rt_token": "...",
  "rt_token_expires_at": "2026-08-07T01:05:00Z",
  "ws_url": "wss://agent-staging.agentos-app.app/v1/agent/stream?session_id=sess_...",
  "voice": {
    "provider": "volc_rtc",
    "app_id": "public-rtc-app-id",
    "room_id": "room_...",
    "user_id": "user_...",
    "token": "001...",
    "token_expires_at": "2026-08-07T01:10:00Z",
    "task_id": "task_...",
    "bot_user_id": "agent_..."
  },
  "scope": {"agents": ["avatar", "strategy"], "tools": []},
  "trace_id": "trc_..."
}
```

RTC AppKey、火山 AK/SK、端到端语音 API Key 永远不进入 Flutter。`room_id`、
`user_id`、`task_id` 均由 Runtime 从 Session 确定性生成，刷新 Token 时保持不变。

## 生命周期

1. Mobile 创建 voice Session。
2. Mobile 使用 `voice.app_id/token/room_id/user_id` 初始化 SDK 并加入房间。
3. 收到进房成功回调后，Mobile 调 `/voice/start`。
4. `/voice/start` 的 200 仅表示火山接受异步任务，Session 依次为
   `voice_starting -> task_accepted -> bot_joining`。Runtime 校验回调身份，并轮询
   `GetRoomOnlineUsers`；只有同一 AppId/RoomId 中出现精确 `bot_user_id` 才变成
   `voice_ready`。超时为 `bot_join_timeout`，异步错误为 `provider_failed`。
5. 收到 Token 将过期回调时，Mobile 调 `/token` 并原地 `updateToken`；失败才安全重进房。
6. 用户结束时，Mobile 先释放本地 RTC，再 DELETE Session；Runtime 幂等调用
   `StopVoiceChat`，成功后保留短期停止墓碑状态，供迟到回调去重和清理证据查询。

当前真实链路使用 `S2SConfig.OutputMode=1` 混合编排：普通闲聊由 S2S 低延迟处理，
实时价格/24 小时涨跌/趋势问题由支持 Function Calling 的 Ark LLM 调用
`market_tickers`（`get_market_price` 仅保留为单 symbol 兼容入口）。Runtime 只请求 Core
内部行情接口，不直连 Hyperliquid；返回的是
Hyperliquid Mainnet BTC/ETH PERP，不是现货价格，也不是 Testnet 交易成交证据。

模型输出的 symbol 不可信。`BTCUSDC`、`BTC-USDC`、`BTC/USDC`、`BTCUSDT` 等值只作为
Core Market Catalog 的搜索提示；Runtime 必须取得唯一 `PERP` Instrument，随后仅使用
Catalog 的 `venue_symbol` 查询行情。Catalog 多匹配、stale、inactive 或
`trade_supported=false` 时不会查询行情，也不会创建 Draft。

Mobile 调用 `/v1/agent/messages` 应为一次用户消息保持同一个 `Idempotency-Key`。如果等待
超时，以完全相同的消息和 Key 重试；Runtime 会返回已保存的同一响应（包括原
`core_draft_id`），不会重复调用模型或创建第二份 Draft。Key 原值、USER Token 和模型 Key
均不写入 Session。

完整字段见 [`openapi.agent.yaml`](./openapi.agent.yaml)，RTC 时序见
[`voice-webrtc.md`](./voice-webrtc.md)。
