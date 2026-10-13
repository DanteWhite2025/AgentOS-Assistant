# 📱 Agent Runtime 全量 API 调试与对接规范文档

**文档版本**：v1.2.0  
**部署环境**：Staging (`http://174.138.21.66:8081` / `https://agent-staging.agentos-app.app`)  

> ⚠️ 域名极易与 Core 的 staging 域名 `https://api-staging.agentos-app.app` 混淆（一字之差：
> `agent-` vs `api-`）。两者是完全不同的服务：打错域名会命中 Core，`/v1/agent/*` 全部 404
> （Go 风格纯文本 `404 page not found`），`/healthz`/`/readyz` 却能通但返回的是 Core 自己的
> 极简健康检查（`{"status":"ok"}`），很容易被误判成"agent-runtime 挂了"或"接口没实现"。
**适用对象**：前端 App 开发团队、测试团队、Golang Core 团队

> 认证以 Core USER Bearer Token 为准；历史示例中的 `X-User-Id` 不是身份凭据，不能用于
> 生产鉴权。策略 Draft 必须提交 `strategy_id`，账户、Venue 与 Mandate 由 Runtime 通过
> Core StrategyBinding 解析。

---

## 目录
1. [身份鉴权与 Header 规范](#一-身份鉴权与-header-规范)
2. [会话管理与凭证签发 (Sessions API)](#二-会话管理与凭证签发-sessions-api)
3. [单轮文本与卡片交互 (Single Turn API)](#三-单轮文本与卡片交互-single-turn-api)
4. [语音工具旁挂执行 (Tool Calls API)](#四-语音工具旁挂执行-tool-calls-api)
5. [会话历史与字幕归档 (History API)](#五-会话历史与字幕归档-history-api)
6. [系统健康与就绪探针 (Health API)](#六-系统健康与就绪探针-health-api)

---

## 一、 身份鉴权与 Header 规范

所有调往 `agent-runtime` 的 HTTP 请求均需在 Header 中携带身份识别与鉴权数据：

```http
Authorization: Bearer <USER_TOKEN_OR_RT_TOKEN>
X-User-Id: u_dev_001
Content-Type: application/json
```

* **`USER_TOKEN`**：由 Golang Core / Identity 服务签发的 JWT Token，作用域为 `trade:confirm` / 用户主体。
* **`RT_TOKEN`**：由 Agent Runtime `/sessions` 接口签发的 Session Token，作用域为 `tool:relay` 及 WS 实时流。

---

## 二、 会话管理与凭证签发 (Sessions API)

### 1. 创建 Agent 会话 (`POST /v1/agent/sessions`)

创建新的交互会话，获得 `rt_token`、WebSocket 连接地址以及实时语音凭证包（火山 RTC 或 OpenAI Realtime）。

* **Request**
```bash
curl -X POST "http://174.138.21.66:8081/v1/agent/sessions" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: u_dev_001" \
  -d '{
    "modality": "both"
  }'
```

* **Response (HTTP 201 Created)**
```json
{
  "session_id": "sess_a8b9c7d6e5f41234567890abcdef1234",
  "rt_token": "eyJzaWQiOiJzZXNzX2E4YjljN2Q2ZTVmNDEyMzQ1Njc4OTA...",
  "rt_token_expires_at": "2026-08-07T16:00:00.000Z",
  "ws_url": "wss://agent-staging.agentos-app.app/v1/agent/stream?session_id=sess_a8b9c7d6e5f41234567890abcdef1234",
  "voice": {
    "provider": "volc_rtc",
    "room_id": "room_sess_a8b9c7d6e5f4",
    "user_id": "u_dev_001",
    "token": "001app_id_token_example_xxxx",
    "app_id": "673f8a12b3c4d5e",
    "expires_at": "2026-08-07T16:00:00.000Z"
  },
  "scope": {
    "agents": ["avatar", "strategy"],
    "tools": [
      "agentos_get_balances",
      "agentos_get_positions",
      "agentos_get_orders",
      "agentos_create_trade_draft"
    ]
  },
  "trace_id": "trc_998877665544332211"
}
```

---

### 2. 查询会话状态 (`GET /v1/agent/sessions/{session_id}`)

* **Request**
```bash
curl -X GET "http://174.138.21.66:8081/v1/agent/sessions/sess_a8b9c7d6e5f41234567890abcdef1234" \
  -H "X-User-Id: u_dev_001"
```

* **Response (HTTP 200 OK)**
```json
{
  "session_id": "sess_a8b9c7d6e5f41234567890abcdef1234",
  "state": "created",
  "modality": "both",
  "created_at": "2026-08-07T15:00:00.000Z",
  "last_activity_at": "2026-08-07T15:05:00.000Z",
  "scope": {
    "agents": ["avatar", "strategy"],
    "tools": ["agentos_get_balances", "agentos_get_positions", "agentos_create_trade_draft"]
  }
}
```

---

### 3. 刷新/续签凭证 (`POST /v1/agent/sessions/{session_id}/token`)

* **Request**
```bash
curl -X POST "http://174.138.21.66:8081/v1/agent/sessions/sess_a8b9c7d6e5f41234567890abcdef1234/token" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: u_dev_001" \
  -d '{
    "want": ["rt_token", "voice"]
  }'
```

* **Response (HTTP 200 OK)**
```json
{
  "rt_token": "eyJzaWQiOiJzZXNzX2E4YjljN2Q2ZTVmNDEyMzQ1Njc4OTA...",
  "rt_token_expires_at": "2026-08-07T17:00:00.000Z",
  "voice": {
    "provider": "volc_rtc",
    "token": "001app_id_new_refreshed_token_xxxx"
  },
  "trace_id": "trc_112233445566778899"
}
```

---

### 4. 结束会话 (`DELETE /v1/agent/sessions/{session_id}`)

* **Request**
```bash
curl -X DELETE "http://174.138.21.66:8081/v1/agent/sessions/sess_a8b9c7d6e5f41234567890abcdef1234" \
  -H "X-User-Id: u_dev_001"
```

* **Response (HTTP 204 No Content)**

---

## 三、 单轮文本与卡片交互 (Single Turn API)

适用于不需要维持持续 WS 连接的简易文本对话与卡片触发展示。

### 策略交易草稿 (`POST /v1/agent/trade-draft`)

```json
{
  "session_id": "sess_...",
  "strategy_id": "strategy_...",
  "text": "按 60000 限价买入 0.001 BTC"
}
```

客户端不能提交 `unified_account_id`、`venue_account_id`、`mandate_id` 或授权 ID。
Runtime 会读取 Core StrategyBinding 并精确匹配账户；返回值只是 Draft 和确认卡片，不代表
已下单或成交。

### 1. 发送单轮指令 (`POST /v1/agent/single-turn`)

* **Request**
```bash
curl -X POST "http://174.138.21.66:8081/v1/agent/single-turn" \
  -H "Content-Type: application/json" \
  -H "X-User-Id: u_dev_001" \
  -d '{
    "user_input": "帮我按 50000 价格买入 0.1 个 BTC",
    "session_id": "sess_a8b9c7d6e5f41234567890abcdef1234"
  }'
```

* **Response (HTTP 200 OK - 返回文本 + 内嵌卡片)**
```json
{
  "session_id": "sess_a8b9c7d6e5f41234567890abcdef1234",
  "text": "好的，已为您生成买入 0.1 BTC 的交易草案，请在下方卡片中确认：",
  "card": {
    "card_type": "TRADE_DRAFT",
    "card_id": "crd_5544332211",
    "draft_id": "draft_9988776655",
    "symbol": "BTC/USDT",
    "side": "BUY",
    "order_type": "LIMIT",
    "quantity": "0.1",
    "limit_price": "50000.00",
    "leverage": 1,
    "est_liquidation_price": "N/A",
    "est_fee": "0.005 USDT",
    "status": "PENDING_CONFIRMATION"
  },
  "trace_id": "trc_4433221100"
}
```

---

## 四、 语音工具旁挂执行 (Tool Calls API)

当移动端使用 RTC / WebRTC 语音通道收到 AI 发起的工具调用指令时，异步旁挂请求 Runtime 执调。

### 1. 执行旁挂工具 (`POST /v1/agent/sessions/{session_id}/tool-calls`)

* **Request**
```bash
curl -X POST "http://174.138.21.66:8081/v1/agent/sessions/sess_a8b9c7d6e5f41234567890abcdef1234/tool-calls" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <RT_TOKEN>" \
  -d '{
    "tool_name": "agentos_get_balances",
    "arguments": {
      "unified_account_id": "acc_001"
    },
    "call_id": "call_12345678"
  }'
```

* **Response (HTTP 200 OK)**
```json
{
  "call_id": "call_12345678",
  "output": "{\"USDT\": {\"free\": \"10000.00\", \"locked\": \"0.00\"}, \"BTC\": {\"free\": \"0.5\", \"locked\": \"0.0\"}}",
  "status": "success",
  "trace_id": "trc_7788990011"
}
```

### 2. 短语音上传转写 (`POST /v1/agent/audio/transcribe`)

端侧本地识别（如 iOS Speech API）对中文与币圈专有名词（BTC、滑点、做多做空……）
识别率差。该接口接收前端上传的短语音文件，调用火山豆包 Seed ASR Flash 做一次
同步识别，返回准确文字。**只适合几秒到几十秒的短语音**——上传的是整段音频，
不是流式；更长的录音应等录完再上传，或改走流式识别通道。

* **鉴权**：与其他接口一致，`Authorization: Bearer <USER_TOKEN>`。
* **入参**：`multipart/form-data`，字段 `file`（必填），支持 `.m4a` / `.wav` /
  `.mp3` / `.aac` / `.ogg` / `.pcm`，大小上限 10MB。

* **Request**
```bash
curl -X POST "http://174.138.21.66:8081/v1/agent/audio/transcribe" \
  -H "Authorization: Bearer <USER_TOKEN>" \
  -F "file=@test.m4a"
```

* **Response (HTTP 200 OK)**
```json
{
  "transcript": "帮我查看一下当前比特币价格和我的持仓",
  "duration_ms": 1250,
  "trace_id": "trc_5566778899"
}
```

* **异常响应**
  | 场景 | HTTP | `code` |
  | --- | --- | --- |
  | 文件为空 / 扩展名不受支持 / 超过 10MB | 400 | `INVALID_REQUEST` |
  | 未携带有效 `Authorization` | 401 | `UNAUTHENTICATED` |
  | 上游超时或 5xx（受控重试 1 次后仍失败） | 502 | `INTERNAL` |

  错误信封统一为 `{code, message, details, trace_id}`；不会在 `message` /
  `details` 中回显火山 API Key 或原始响应体。

---

## 五、 会话历史与字幕归档 (History API)

### 1. 查询历史消息 (`GET /v1/agent/sessions/{session_id}/history`)

* **Request**
```bash
curl -X GET "http://174.138.21.66:8081/v1/agent/sessions/sess_a8b9c7d6e5f41234567890abcdef1234/history?limit=20" \
  -H "X-User-Id: u_dev_001"
```

* **Response (HTTP 200 OK)**
```json
{
  "messages": [
    {
      "msg_id": "msg_001",
      "role": "user",
      "content": "查一下我的持仓",
      "timestamp": "2026-08-07T15:00:00Z"
    },
    {
      "msg_id": "msg_002",
      "role": "assistant",
      "content": "您当前持有 0.5 个 BTC，未实现盈亏为 +$120.50 USDT。",
      "timestamp": "2026-08-07T15:00:02Z"
    }
  ],
  "next_cursor": null
}
```

---

## 六、 系统健康与就绪探针 (Health API)

### 1. 就绪探针 (`GET /readyz`)

读取 Runtime 的依赖就绪缓存。启动时及后台周期任务会用 Runtime AGENT Token 向 Core
执行真实只读 BTC ticker preflight；本探针自身不会在每次请求时访问 Core。

* **Request**
```bash
curl -i http://174.138.21.66:8081/readyz
```

* **Response (HTTP 200 OK)**
```json
{
  "status": "ready",
  "service": "agent-runtime",
  "env": "staging",
  "provider_mode": "real",
  "realtime_voice_provider": "volc_rtc",
  "core_credential": {
    "status": "valid",
    "valid": true,
    "core_verified": true,
    "actor_type_valid": true,
    "agent_id_valid": true,
    "scopes_valid": true,
    "forbidden_scopes_absent": true,
    "expiring_soon": false,
    "expires_in_seconds": 604800,
    "core_request_id": "req_...",
    "last_verified_at": "2026-08-15T00:00:00+00:00"
  },
  "voice_e2e_verified": false
}
```

Token 缺失/过期、Core 401/403、actor 或 agent_id 错误、Scope 不足、包含
`trade:confirm`/`trade:execute` 等禁止 Scope，或 Core 暂不可用时返回 HTTP 503 和
`status=not_ready`。`/healthz` 的 200 仅表示进程存活，不表示行情或语音可用。

---
*文档由 AgentOS Runtime 自动生成于 2026-08-07。*
