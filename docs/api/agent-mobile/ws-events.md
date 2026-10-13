# 文本流 WebSocket 事件字典

> 通道 ②：App ↔ agent-runtime（RT）直连 WebSocket，用于流式文本对话、工具调用可见性、数字人事件。
> 语音通道见 [`voice-webrtc.md`](./voice-webrtc.md)；REST 控制面见 [`openapi.agent.yaml`](./openapi.agent.yaml)。

## 1. 连接握手

```
wss://agent.yesup.io/v1/agent/stream?session_id=sess_01J...
```

- 鉴权：连接时携带 `rt_token`。推荐 `Sec-WebSocket-Protocol: bearer, <rt_token>`；不支持自定义子协议的客户端可用 query `?token=<rt_token>`。
- 连接建立后 RT 首帧下发 `session.state`（当前状态 + scope）。
- `rt_token` 过期：RT 主动发 `error{code: SESSION_EXPIRED}` 并关闭；App 调 `POST /v1/agent/sessions/{id}/token` 续签后重连。

## 2. 消息信封

所有帧为 JSON 文本帧，统一信封：

```jsonc
{
  "type": "response.text.delta",   // 事件类型
  "seq": 128,                       // 单调递增序号（下行），断线重连去重用
  "session_id": "sess_01J...",
  "trace_id": "trc_01J...",
  "data": { }                       // 各事件的 payload
}
```

## 3. 上行事件（App → RT）

| type | data | 说明 |
|---|---|---|
| `user.message` | `{ "text": "查一下最新黄金消息", "client_msg_id": "c_01", "web_search": true, "read_aloud": false }` | 用户文本输入；`web_search=true` 仅对本条消息启用并强制执行联网搜索，缺省为 `false`；`read_aloud` 仅在明确为布尔值 `true` 时为本条回复启用 pipeline 语音合成，缺省关闭 |
| `user.interrupt` | `{ }` | 打断当前生成（含正在进行的工具调用展示） |
| `ping` | `{ }` | 心跳，RT 回 `pong` |

## 4. 下行事件（RT → App）

### 4.1 文本生成

| type | data | 说明 |
|---|---|---|
| `response.started` | `{ "response_id": "r_01" }` | 一轮回复开始 |
| `response.text.delta` | `{ "response_id": "r_01", "delta": "你当前" }` | 文本增量，来自模型的真实 token 流 |
| `response.text.done` | `{ "response_id": "r_01", "text": "你当前持有 1.2 ETH…" }` | 该轮文本完成，`text` 等于全部 delta 的拼接 |
| `response.stage` | 见 4.1.1 | 当前阶段（在干什么），用于填补工具卡之间的静默 |
| `response.thinking.done` | 见 4.1.2 | 「已思考 N 秒」定格，在第一个字之前到达 |
| `response.suggestions` | 见 4.1.3 | 「你可能还想问」4~5 条，渲染成 chips |

文字增量应直接追加到同一个正式回答气泡，不限制为三行预览。收到
`response.text.done` 即结束文字生成状态并允许继续发送，无须等待播报完成。

当 `read_aloud=true` 且服务端启用 pipeline 时，同一 WebSocket 还会下发
`audio.chunk`（`index`、`format`、Base64 `data`）、`audio.fallback` 和 `audio.done`。
音频可以晚于文字完成到达；客户端应独立接收/播放，收到 `audio.done` 后可关闭
本轮通道，已缓存的音频继续播放。关闭播报应立即停止本地音频并丢弃本轮后续分片；
开始下一轮或语音输入时应清理上一轮音频。开启播报从下一条请求生效。
旧客户端未携带 `read_aloud` 时也默认纯文字；旧后端忽略该字段时，新客户端仍应
在关闭状态丢弃音频，并在文字完成时结束等待。

#### 4.1.1 `response.stage` —— 现在在干什么

```json
{ "response_id": "r_01", "stage": "thinking", "title": "正在思考", "title_key": "stage.thinking" }
```

| stage | title | 什么时候 |
|---|---|---|
| `thinking` | 正在思考 | 一轮刚开始，模型还在判断要不要调工具 |
| `composing` | 正在整理结果 | 工具全部返回，模型在写最终答案 |

- **这是状态，不是事件流**：后到的阶段覆盖先到的，跟 `avatar.event` 的 state 一样。多轮工具的轮次会重复收到 `composing`，按「当前阶段」渲染即可，不要往下堆列表。
- `title` 是服务端渲染好的中文，可直接显示；`title_key` 固定为 `stage.<stage>`。**客户端有自己的翻译就用自己的，没有就回落 `title`** —— 后端新增阶段时客户端不发版也不会显示成空白。
- 未来可能新增 stage 值，**客户端必须容忍未知值**（照常显示它给的 `title`），不要写成穷举 switch 后崩掉。
- 工具本身的开始与结束仍走 `tool.invoked` / `tool.result`，两条通道不重叠：`response.stage` 管的是工具卡**之间**那几段静默。

#### 4.1.2 `response.thinking.done` —— 已思考 N 秒

```json
{ "response_id": "r_01", "elapsed_ms": 8200 }
```

- 语义是**首字延迟**：从收到 `user.message` 到第一个真正的字出现，**含工具执行那一段**。
- **每轮最多一条**，且保证在第一个 `response.text.delta` 之前到达。一个字都没吐出来的轮次（工具全失败、早退文案）会在 `response.text.done` 之前补一条。
- 出错（`error`）和被打断（`user.interrupt`）的轮次**不会**有这条 —— 那两种情况客户端本就该把进度行收掉。
- **不要客户端自己掐表。** 流式首块可能是空的（provider 的心跳与占位），一收到就停表会算出「已思考 0 秒」，比不显示更糟。服务端这个数字包含了工具耗时，才是用户真正等的那段。
- 显示真实数字就好，不需要往小了修。等待期一直有反馈的话，14 秒也不显得慢；数字做小了但界面干等，用户照样觉得卡，而且掐表一对就露了。

#### 4.1.3 `response.suggestions` —— 你可能还想问

```json
{ "response_id": "r_09", "questions": ["资金费率现在多少？", "要看我的持仓吗？", "平仓价位怎么定？"] }
```

- **每轮最多一条**，保证在 `response.text.done` **之前**到达 —— chips 要跟着那条消息一起渲染。
- **模型没给追问时不会有这条事件**，不是发一个空数组。前端不用写「questions 为空」的分支，收到就渲染。
- `questions` 最多 5 条，**可能少于 5 条**（模型偶尔只给 3~4 条，不值得为此整段丢弃）。按实际条数渲染，不要占位。
- 每条不超过 30 字，可以直接当 chip 文案；点击后作为新的 `user.message` 原样发出去即可，**不需要**自己拼上下文——服务端会带上本会话最近几轮的问答。

**追问不会重复出现在正文里。** 服务端在流式过程中就从正文尾部剥掉了，所以：

- `response.text.delta` 里没有追问，也没有那行控制标记；
- `response.text.done` 的 `text` 仍然**严格等于**全部 delta 的拼接（§4.1 那条契约不变，可以继续拿它做校验）。

> 剥离是边流边做的，正文该什么时候到还是什么时候到 —— 不会为了凑完整追问去缓冲整段。首字延迟与 PR #78 一致。

**降级行为**：模型没按格式输出时（把正文接着写在标记后面），服务端解析不出追问就**不发这条事件**，那段文字原样回到正文，这一轮照常收尾。前端不需要为「模型不听话」做任何特殊处理。

#### 4.1.4 正文的固定结构

深度分析场景（本轮调用了搜索或证据抓取工具）的正文开头是 **3 条 bullet 的【重点摘要】**，合计不超过 100 字，然后才展开四段正文。

这部分是**普通正文**，随 `response.text.delta` 正常流式到达，没有独立事件 —— 想把摘要单独做成卡片的话，按 markdown 的第一个 bullet 列表切即可。


**delta 的边界由模型决定**，不是固定字数，也不保证按句读切分——直接追加渲染即可，不要假设每条的长度。首个 delta 到达之前 `avatar.event` 保持 `Thinking`，第一个字到了才切 `Speaking`。

`done` 里的 `text` **等于已经发出去的全部 delta 拼接**。客户端可以用它做一次校验，但不要拿它整体替换已渲染的内容——两者按设计一致，替换只会造成闪烁。

**出错时也可能先收到 `done`**：如果这一轮已经吐出了部分内容才发生错误，服务端会先补一条 `response.text.done`（带已发出的部分）把这条消息收尾，再发 `error`。收到 `error` 时那条消息应当已经是完结状态，不要让它停在「正在输入」。一个字都没发出去就失败时不会有 `done`。

### 4.2 工具调用可见性

工具的开始与结束都是**实时**下发的，用于展示「正在查询行情…」这类中间过程。
每一条 `tool.invoked` 都保证有一条配对的 `tool.result`（含失败与用户中断）。

| type | data | 说明 |
|---|---|---|
| `tool.invoked` | 见下 | RT 开始调用受控工具，执行前发出 |
| `tool.result` | 见下 | 该次调用的终态 |

**`tool.invoked`**

```json
{
  "call_id": "t_01",
  "name": "web_search",
  "title": "联网搜索",
  "title_key": "tool.web_search.running",
  "params": { "query": "BTC 今天为什么跌" }
}
```

- `name` 是原始工具名，用于埋点，不要直接显示给用户。
- `title` 是服务端渲染好的中文文案，可直接显示。
- `title_key` 是 i18n key（固定为 `tool.<name>.running`）。**客户端有对应翻译就用自己的，没有就回落到 `title`** —— 这样后端新增工具时客户端不发版也不会显示成空白。
- `params` 只包含该工具声明为可展示的字段，值已做展示净化（清除不可见字符、超长截断）。可能为空对象。

**`tool.result`**

```json
{
  "call_id": "t_01",
  "name": "web_search",
  "status": "ok",
  "summary": "",
  "trade_draft_id": null
}
```

`status` 的取值与建议呈现：

| status | 含义 | 建议呈现 |
|---|---|---|
| `ok` | 拿到数据 | 收成完成态 |
| `no_data` | 没查到（超时、无结果、上游异常都归这一档，服务端已经重试过了） | **中性提示**，不是错误，不要用红色或告警图标 |
| `blocked` | 需要用户处理：写工具失败、额度或杠杆超限 | 提示用户，`summary` 是可直接显示的说明 |
| `cancelled` | 用户中断，该调用没有终态 | 收成中断态 |

`summary` 在 `no_data` / `blocked` / `cancelled` 时是一句可直接显示的中文，`ok` 时为空串（此时用 `tool.invoked` 的 `title` 渲染完成态即可）。

> **不要把 `status` 当错误码用。** 技术细节（具体是超时还是参数错误）不在这里下发，排查请用 `trace_id` 查服务端日志。产品口径是少报错：只读工具失败会降级成 `no_data` 并让 Agent 如实说「这次没查到」后继续作答，对话不中断。

> 下单类工具（如 `create_trade_draft`）只创建草案。App 收到 `trade_draft_id` 后，拉起 **yesup-core** 的确认页（`/v1/trade-drafts/{id}/confirm`），**不在本通道确认**。展开态可以显示 `params` 里的完整构造参数供用户复核 —— 最终签名与广播权在用户手上，这一步就是复核环节。

### 4.3 数字人事件

| type | data | 说明 |
|---|---|---|
| `avatar.event` | `{ "kind": "state", "value": "Speaking" }` | 状态：`Idle` / `Listening` / `Thinking` / `Speaking` |
| `avatar.event` | `{ "kind": "emotion", "value": "confident", "intensity": 0.6 }` | 情绪 |
| `avatar.event` | `{ "kind": "gesture", "value": "nod" }` | 动作 |

> 口型 viseme 第一版不下发（见 spec §8，语音直连下 RT 无音频流）。App 无数字人 SDK 时可忽略 `avatar.event` 降级为纯文本。

### 4.4 会话与错误

| type | data | 说明 |
|---|---|---|
| `session.state` | `{ "state": "text_active", "scope": { ... } }` | 首帧 & 状态变更时下发 |
| `pong` | `{ }` | 心跳响应 |
| `error` | `{ "code": "RATE_LIMITED", "message": "...", "trace_id": "..." }` | 错误信封，code 取值同 REST 错误表 |

Core AGENT Token 过期或被拒绝时，Runtime 发送 `error`：`code=INTERNAL`、
`message=智能体服务凭证暂不可用`，`details` 仅包含 `upstream_code`、`request_id`、
`retryable`、`credential_refresh_required`。这不是 Mobile USER Token 的 401；客户端不应
登出用户。该错误后连接保持打开，Runtime 紧接着发送 `avatar.event=Idle`，App 可继续
心跳并在服务恢复后重试。原始 Core body、JWT、Authorization 和 traceback 永不下发。

## 5. 断线重连

1. App 以指数退避重连同一 `ws_url`。
2. 若 `rt_token` 已过期 → 先 `POST /v1/agent/sessions/{id}/token` 续签。
3. 重连后 RT 下发最新 `session.state`；App 用 `GET /v1/agent/sessions/{id}` 或 `GET .../history` 补齐未完成轮次。
4. 用下行 `seq` 去重，避免重复渲染。

## 6. 示例：一次带工具的问答

```jsonc
// → 上行
{ "type": "user.message", "data": { "text": "我现在能开多少倍 ETH 多单？", "client_msg_id": "c_09", "web_search": false } }

// ← 下行（依次）
{ "type": "response.started", "seq": 40, "data": { "response_id": "r_09" } }
{ "type": "avatar.event",     "seq": 41, "data": { "kind": "state", "value": "Thinking" } }
{ "type": "response.stage",   "seq": 42, "data": { "response_id": "r_09", "stage": "thinking", "title": "正在思考", "title_key": "stage.thinking" } }
{ "type": "tool.invoked",     "seq": 43, "data": { "call_id": "t_09", "name": "get_risk_summary", "title": "查询风险状况", "title_key": "tool.get_risk_summary.running", "params": {} } }
{ "type": "tool.result",      "seq": 44, "data": { "call_id": "t_09", "status": "ok" } }
{ "type": "response.stage",   "seq": 45, "data": { "response_id": "r_09", "stage": "composing", "title": "正在整理结果", "title_key": "stage.composing" } }
{ "type": "response.thinking.done", "seq": 46, "data": { "response_id": "r_09", "elapsed_ms": 8200 } }
{ "type": "avatar.event",     "seq": 47, "data": { "kind": "state", "value": "Speaking" } }
{ "type": "response.text.delta", "seq": 48, "data": { "response_id": "r_09", "delta": "在当前风险额度下，" } }
{ "type": "response.suggestions",   "seq": 49, "data": { "response_id": "r_09", "questions": ["现在资金费率多少？", "帮我看下可用余额", "5 倍的强平价在哪？"] } }
{ "type": "response.text.done",  "seq": 50, "data": { "response_id": "r_09", "text": "在当前风险额度下，你最多可开 5 倍杠杆。" } }
{ "type": "avatar.event",     "seq": 51, "data": { "kind": "state", "value": "Idle" } }
```

## 7. 流式语音识别（Streaming ASR）

> 通道 ③：`WSS /v1/agent/audio/stream`。与通道②的文本流是**两条独立连接**——
> 这条只认用户身份，不绑定某个已有的 Agent Session，事件信封也更薄
> （只有 `type` + `data`，没有 `seq`/`session_id`），专门服务"按住说话、边说边出
> 字"这一个场景。识别出的文字仍然由 App 照常拼进输入框，再走通道②的
> `user.message` 正常发送——这条通道只管"听写"，不参与对话。

### 7.1 连接握手

```
wss://agent.yesup.io/v1/agent/audio/stream?token=<USER_TOKEN_OR_RT_TOKEN>
```

- `token` 接受两种：`POST /v1/agent/sessions` 签发的 `rt_token`，或 Core USER
  Bearer Token（不带 `Bearer ` 前缀，就是原始 token 值）。两种任一有效即放行。
- 鉴权失败：服务端直接以 close code **`4001`** 关闭连接，不下发 `error` 事件——
  App 应据此判断是鉴权问题而不是网络问题，引导用户重新登录或续签。
- 一条连接内可以反复 `audio.start` → `audio.end` 做很多段独立的录音，对应移动
  端反复按住说话，**不需要每次都重新连接**。

### 7.2 上行事件（App → Runtime）

| type | data | 说明 |
|---|---|---|
| `audio.start` | `{ "format": "pcm", "sample_rate": 16000, "channels": 1 }` | 开始一段新录音。`format` 取值 `pcm` / `wav` / `opus` / `aac` / `ogg`，缺省 `pcm`；`sample_rate` 缺省 16000（推荐值）；`channels` 缺省 1 |
| 二进制帧 | 原始音频字节 | **推荐方式**，每帧 100~200ms 音频，传输效率最高；`audio.start` 之后、`audio.end` 之前随时可发 |
| `audio.data` | `{ "type": "audio.data", "data": "<base64>" }` | 二进制帧的 JSON 兼容形式，`data` 直接是 base64 字符串（不嵌套） |
| `audio.end` | `{ }` | 这段录音说完了，Runtime 会尽快给出定稿；发送后可以立刻开始下一段 `audio.start`，不需要等定稿到达 |

### 7.3 下行事件（Runtime → App）

| type | data | 说明 |
|---|---|---|
| `transcript.delta` | `{ "text": "帮我查一下比", "is_final": false }` | 打字机中间态——**整体替换**当前显示文本，不是追加；后续增量会覆盖它 |
| `transcript.done` | `{ "text": "帮我查一下比特币现在多少钱", "is_final": true }` | 整句定稿。一段录音（一次 `audio.start`~`audio.end`）**最多一条** |
| `error` | `{ "code": "ASR_UPSTREAM_ERROR", "message": "语音识别服务暂时不可用" }` | 见 7.4 |

`transcript.delta` 不保证按字数递增或按标点切分——直接用最新一条整体替换输入框
内容即可，不要自己做字符串 diff。

### 7.4 错误与重试

| 场景 | 处理方式 |
|---|---|
| `token` 无效/缺失 | close code `4001`，不下发 `error` |
| 服务端未配置识别渠道 | 下发一条 `error{code: ASR_UPSTREAM_ERROR}`，随后 close code `1011` |
| `audio.start` 参数非法（不支持的 `format` 等） | 下发 `error{code: INVALID_REQUEST}`，**连接不断开**，可直接重发合法的 `audio.start` 重来 |
| 上游识别中途失败（网络抖动、火山 5xx） | 服务端受控重试；仍失败则下发 `error{code: ASR_UPSTREAM_ERROR}`，**连接不断开**，这一段录音作废，可直接开始下一段 |

除鉴权失败外，`error` 都不会主动断开连接——App 收到后清空「正在识别」状态、
提示用户重试即可，不需要重新握手。

### 7.5 示例：一段完整的按住说话

```jsonc
// → 上行
{ "type": "audio.start", "data": { "format": "pcm", "sample_rate": 16000, "channels": 1 } }
// → 二进制帧 × N（每帧 100~200ms 音频）
// → 上行
{ "type": "audio.end" }

// ← 下行（依次）
{ "type": "transcript.delta", "data": { "text": "帮我查一下比", "is_final": false } }
{ "type": "transcript.delta", "data": { "text": "帮我查一下比特币", "is_final": false } }
{ "type": "transcript.done",  "data": { "text": "帮我查一下比特币现在多少钱", "is_final": true } }
```

对接建议：iOS 用 `AVAudioEngine` 装一个 tap，PCM16 每 100~200ms 切一帧走
`URLSessionWebSocketTask.send(.data(...))`；Flutter 用 `record` 包的流式录音 +
`web_socket_channel`，同样按二进制帧发送，收到 `transcript.delta` 就
`setState` 覆盖输入框文本。
