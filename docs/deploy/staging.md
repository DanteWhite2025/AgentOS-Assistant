# Staging 部署：Realtime Voice

Staging 固定使用 `AGENT_PROVIDER_MODE=real`。配置缺失、Fake provider、非公网
`AGENT_RT_WS_BASE` 都会在启动时失败，不会静默降级。

当前主链路为 Gemini Live v1beta：

| 变量 | GitHub 类型 | 用途 |
|---|---|---|
| `AGENT_REALTIME_VOICE_PROVIDER=gemini_live` | 固定 | 选择 Gemini Live 原生音频 WSS |
| `AGENT_GEMINI_API_KEY` | Secret | 仅 Runtime 用于签发单次短期票，绝不下发 Mobile |
| `AGENT_GEMINI_BASE_URL=https://generativelanguage.googleapis.com` | 固定 | 官方 v1beta Auth Token 服务 |
| `AGENT_GEMINI_LIVE_MODEL=gemini-3.1-flash-live-preview` | 固定 | 当前 Mobile 与 Runtime 共同锁定的 Live 模型 |

Gemini Live 的预设音色由 Runtime 签票与 Mobile setup 共同固定为 `Leda`；两端必须保持一致，
避免受约束 WebSocket 因会话配置漂移而拒绝连接。

Runtime 启动会真实调用 `/v1beta/auth_tokens`，再用该单次临时票连接 v1beta
`BidiGenerateContentConstrained` 并等待 `setupComplete`。任一步失败时 `/readyz` 为 503，
`gemini_connectivity_verified=false`；日志只记录安全状态和模型名，不记录 Key、Token 或
带 `access_token` 的 WebSocket URL。这个 preflight 只证明签票和握手，不是用户语音 E2E。

以下火山配置仅在显式切换 `volc_rtc` 备用链路时使用；Gemini 模式的 `/readyz` 不再用
RTC/S2S/Ark 的 false 字段表达 Gemini 是否可用。

## 必填配置

| 变量 | GitHub 类型 | 用途 |
|---|---|---|
| `AGENT_REALTIME_VOICE_PROVIDER=volc_rtc` | 固定 | 选择火山 RTC 主链路 |
| `AGENT_RTC_APP_ID` | Variable | RTC 公共 AppId，必须为 24 字符 |
| `AGENT_RTC_APP_KEY` | Secret | Runtime 生成 RTC Token，绝不下发 App |
| `AGENT_VOLC_ACCESS_KEY_ID` | Secret | 最小权限 IAM 子用户 AK，用于 RTC OpenAPI |
| `AGENT_VOLC_SECRET_ACCESS_KEY` | Secret | 对应子用户 SK |
| `AGENT_HUOCHAN_S2S_APP_ID` | Variable | 豆包端到端实时语音服务 App ID |
| `AGENT_HUOCHAN_S2S_TOKEN` | Secret | 与 S2S App ID 配对的端到端实时语音 Token；不是 TTS/ASR API Key |
| `AGENT_HUOCHAN_API_KEY` | Secret | 普通豆包 TTS/ASR API Key；S2S 主链路不使用 |
| `AGENT_VOLC_CALLBACK_URL` | Variable | 公网 HTTPS 状态回调地址，固定到 Runtime `/v1/agent/volc-callback` |
| `AGENT_VOLC_CALLBACK_SECRET` | Secret | 独立回调鉴权值，至少 32 字符，不得复用 rt_token secret |
| `AGENT_VOLC_FUNCTION_CALLBACK_URL` | Variable | 公网 Function Calling 回调，固定到 `/v1/agent/volc-function-callback` |
| `AGENT_VOLC_FUNCTION_CALLBACK_SECRET` | Secret | Function Callback 独立签名，至少 32 字符 |
| `AGENT_VOLC_ARK_MODEL` | Variable | 支持 Function Calling 的火山方舟模型名称 |
| `AGENT_VOLC_ARK_ENDPOINT_ID` | Variable | 已启用的自定义方舟推理接入点 ID；必须为 `ep-*`，禁止预置 `ep-m-*` |
| `AGENT_VOLC_ASR_SILENCE_TIMEOUT_SECONDS` | Variable | 欢迎语/远端音频后等待首个 ASR/用户字幕的诊断窗口，默认 12 秒 |
| `AGENT_VOLC_FUNCTION_PROGRESS_DELAY_SECONDS` | Variable | 工具调用超过该秒数后播放“正在查询”安抚语，默认 2 秒；不会表示 Draft/订单成功 |
| `AGENT_LLM_BASE_URL` | Secret / Variable | 自建 New API 中转站地址（含 `/v1`）；real 模式必填 |
| `AGENT_LLM_API_KEY` | Secret | 中转站 Key，文本/策略推理唯一出口；real 模式必填 |
| `AGENT_LLM_MODEL` | Variable | 站内模型 ID（默认 `claude-opus-4-8-low`），与上游原名不同，填错直接 404；real 模式必填 |
| `AGENT_CORE_ACTOR_TOKEN` | Secret | Runtime 调 Core 内部接口的 AGENT Actor Token；行情需 `market:read` |
| `AGENT_CORE_TIMEOUT_SECONDS` | Variable | Core 请求超时，默认 10 秒 |
| `AGENT_CORE_CREDENTIAL_AGENT_ID=agent-test-001` | 固定 | readiness 要求的 AGENT 主体 |
| `AGENT_CORE_CREDENTIAL_REQUIRED_SCOPES` | 固定 | `market:read,account:read,trade:draft` |
| `AGENT_CORE_CREDENTIAL_FORBIDDEN_SCOPES` | 固定 | 拒绝确认、执行、转账、提现和 admin 高权限 |
| `AGENT_CORE_CREDENTIAL_VALIDATION_INTERVAL_SECONDS` | Variable | Core 凭证缓存重验间隔，默认 300 秒 |
| `AGENT_CORE_CREDENTIAL_EXPIRY_WARNING_SECONDS` | Variable | 到期预警阈值，默认 86400 秒 |
| `AGENT_AGENTOS_DEFAULT_STRATEGY_ID=strategy_btc_limit_v1` | 固定 | 文字 Agent Draft 的正式 Core Strategy Binding |
| `AGENT_CORE_BASE_URL` | Variable | Core API 基础地址 |
| `AGENT_RT_WS_BASE` | Variable | 公网 `wss://` Runtime 地址 |
| `AGENT_RT_TOKEN_SECRET` | Secret | Runtime 短期 rt_token 签名密钥 |

`AGENT_CORE_ACTOR_TOKEN` 是唯一接受的 Token 名称。`AGENT_AGENTOS_STAGING_TOKEN`、
`AGENTOS_STAGING_TOKEN` 和 `AGENT_STAGING_TOKEN` 不会被 Runtime 读取，部署时也会从旧
`.env` 删除。缺少正式 GitHub Secret 时部署立即失败；不会输出 Token、长度、前后缀或
解码内容。

Token 必须由 Core/Identity 按其 issuer、audience、AGENT actor 和签名策略签发；Runtime
不得自行签发、延长或保存 Core HMAC Secret。当前没有 Runtime 可调用的自动轮换接口，
标准轮换流程是：Core/Identity 签发最小权限短期 Token → DevOps 更新 GitHub Secret
`AGENT_CORE_ACTOR_TOKEN` → 重新部署。工作流在切换 release symlink 前执行
`python -m app.ops.core_credential_preflight`，该命令先做本地到期/主体/Scope 防呆，再用
同一 Token 向 Core 发起真实只读 BTC ticker 和 agent-trading-accounts 请求；任何一步失败
都会使部署失败，且不会先替换当前运行中的容器。

禁止使用主账号全权限 Access Key。子用户需授予 RTC `StartVoiceChat`、
`StopVoiceChat`、`UpdateVoiceChat`、`GetRoomOnlineUsers` 所需权限；CI、日志和健康检查都不得打印上述 Secret。
还需在控制台确认端到端 S2S AppId/Token 已开通且有余额/配额，模型 `O` 已在
当前账号正式开通。OpenAPI HTTP 200 只表示请求被接受，不表示 Bot 已进房。
Runtime 默认把 S2S 内置识别和合成参数写入
`S2SConfig.ProviderParams.asr/tts`（800ms 判停、24kHz 单声道 PCM、默认 Jupiter 音色）。
判停窗口由 `AGENT_VOLC_ASR_END_SMOOTH_WINDOW_MS` 控制（允许 500–50000ms）；即使部署提供
完整的 `AGENT_VOLC_VOICE_CHAT_CONFIG_JSON`，该显式参数仍会覆盖 JSON 中的旧判停值。
不要用只有 `ModelName`/`VoiceType` 的不完整顶层 `ASRConfig`/`TTSConfig` 替代这组参数；
如果通过 `AGENT_VOLC_VOICE_CHAT_CONFIG_JSON` 显式提供完整的独立 ASR/TTS 配置，仍须同时
具备对应服务的 AppId、Token、ResourceId/Cluster 与配额。

real + `volc_rtc` 启动时会对最终生效的 JSON 做 fail-closed 结构检查：Provider 必须是
`volcano`、`OutputMode=1`、S2S app 凭证存在、`dialog.extra.model=O`、ASR/TTS 完整、RTS
字幕开启、InterruptMode 为 0 或 1；Ark Endpoint 必须是自定义 `ep-*`，两个回调必须是公网
HTTPS。检查错误只报告缺失字段，不回显配置值。

火山 CloudTrail 的 StartVoiceChat RequestParameters 可能展示回调签名。Runtime 因此只把由
各自 master secret 按 Session/Task 派生的签名发送给火山，状态和 Function Callback 两类签名
相互隔离；master 只保存在 GitHub Secret/运行环境中。火山协议提供的是服务端原样回传的不透明
签名字段，没有独立的短期凭证签发接口，因此生命周期由 Task 约束，而不是自行发明过期字段。
轮换时应先阻止新 Voice Session、等待或 Stop 存量任务，再分别更新两个 GitHub Secret、部署并
恢复流量；直接轮换会使存量任务的回调验签失败。本轮不自动轮换 Staging Secret。

## 验证顺序

1. `GET /healthz` 只验证进程存活，不能证明 Core 凭证、行情或语音可用。
2. `GET /readyz` 必须为 HTTP 200、`status=ready` 且
   `core_credential.status=valid/core_verified=true`。过期、Core 拒绝、错误主体、缺 Scope、
   含禁止 Scope 或 Core 暂不可用均返回 HTTP 503 `status=not_ready`。探针只读缓存，不会
   每次访问 Core；后台默认每 300 秒重验。
3. `/readyz` 应显示 `realtime_voice_provider=volc_rtc`；
   `voice_configuration` 只能证明各类配置已注入。`provider_account_verified=false`、
   `bot_presence_verified=false` 或 `voice_e2e_verified=false` 仍表示尚未完成火山账号能力、
   Bot 进房或真机闭环验证，不能用总体 HTTP 200 覆盖这些负面状态。
4. 用 Core USER Token 创建 voice Session，确认响应只包含公共 AppId、房间/用户 ID
   和短期 RTC Token，不含 AppKey/AK/SK。
5. Flutter 真机加入房间成功后调用 `/voice/start`；响应状态为 `task_accepted`，随后
   进入 `bot_joining`。回调的 ASR/LLM/TTS 运行态不能单独证明 Bot 已进房。
6. Runtime 使用 `GetRoomOnlineUsers` 轮询同一 AppId/RoomId，只有精确发现
   `bot_user_id` 才标记 `voice_ready`；超时为 `bot_join_timeout`，火山错误为
   `provider_failed`。UI 只使用稳定的 `voice_error_code/voice_error_message`；脱敏的原始阶段、
   Provider 错误码/消息、Start request ID 和 callback trace 位于 `voice_diagnostics`。Stop/close
   不会清除这些首次失败证据。
   若已看到 Bot 和欢迎语，但 ASR/用户字幕窗口内保持空白，检查
   `voice_error_code=VOICE_UPLINK_OR_ASR_SILENT`、`voice_diagnostics.user_audio_status` 与
   `asr_started_at/first_user_transcript_at`：`UNKNOWN` 表示 Provider 回调没有可证明的入站音频，
   不能写成 PASS，下一步应对照 Mobile 的麦克风授权、`startAudioCapture` 和音频发布回调。
   首轮 presence 查询后采用 0.5s、0.75s、1s、1.5s 的受限退避，Bot 进房即停止；这不会
   预建第二个 Session/Task，也不会延长 join timeout。状态回调应为 HTTP 200，且
   `callback_schema_rejected_total` 不再因官方二进制信封持续增长。
7. 确认 bot_user_id 进房、欢迎词、中文字幕、AI 语音、打断、弱网重连和 Token 刷新。
8. 直接验证 Runtime `market_tickers({"symbols":["BTC","ETH"]})` 命中 Core
   `/internal/v1/market-data/tickers`，AGENT Token 含 `market:read`，响应为 Hyperliquid
   Mainnet PERP 且 Decimal 保持 String；`stale=true` 时不得称为实时行情。
9. 询问实时 BTC 行情，确认 Function Callback、Core request_id、ToolCallID 和
   `UpdateVoiceChat(Command=function)` 完整串联；价格必须与同一 Core 响应一致。
10. DELETE Session 后确认 `StopVoiceChat` 成功，避免任务继续计费。
11. 文字验收先以同一 USER Token 调用 `/messages`：查询模式只读取 Core Trading
    Readiness、正式 `/internal/v1/account-summary?unified_account_id=...` 与真实 BTC
    Ticker，不创建 Draft。检查额度分别返回 Venue Margin、Capital、单笔、总敞口与
    日剩余；Core Ledger balance 不得显示为 Hyperliquid Venue Equity。“生成计划但不要执行”只允许返回一个
    `core_draft_id`，必须为 `agent-test-001 / strategy_btc_limit_v1 / ALWAYS_CONFIRM`，并核对
    Core 订单数与资金流水未变化。

只有上述运行态回调和真机收发保存了脱敏证据，才能把
`voice_e2e_verified` 改成 `true`。
