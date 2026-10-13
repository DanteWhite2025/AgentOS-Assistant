# WebRTC 实时语音网关 —— 交付报告

**日期**：2026-08-23 ｜ **状态**：已上线 staging ｜ **PR**：#34（主体）、#35（CI 门禁）、#36（契约补标）、#37（Token 透传 + Azure 兼容）、#38（Gemini Live）

一句话：**音频走直连图快，钱走服务端图稳。**

---

## 1. 全景架构

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        客户端（App / Web）                                │
│                                                                          │
│      💬 文本聊天区                    🎙 语音连麦区                        │
│      （打字 / 看历史）                （直连通话 / 握手超时降级）           │
│                                                                          │
│              └────────── 共享同一个 session_id ──────────┘                │
└───────┬──────────────────────────────────────────┬───────────────────────┘
        │                                          │
        │ ①控制面（HTTPS·JSON）                     │ ②数据面（WebRTC·音频）
        │   凭证 / 风控 / 结账                       │   不经过我们的服务器
        │                                          │
        │                                          ▼
        │                        ┌─────────────────────────────────────┐
        │                        │  云端语音大模型（自带耳朵和嘴）        │
        │                        │  · OpenAI Realtime  （海外·强推理）  │
        │                        │    └ Azure OpenAI（自动切鉴权头）    │
        │                        │  · Gemini Live（原生音频·WSS）        │
        │                        │  · 火山 RTC + 豆包   （国内·低延迟）  │
        │                        └──────────────┬──────────────────────┘
        │                                       │
        │                                       │ AI 说「帮我买 0.1 BTC」
        │                                       │ ↓ 触发 Tool Call
        │                        ┌──────────────┴──────────────────────┐
        │                        │  ⛔ 客户端只做转发，严禁直连交易所     │
        │                        └──────────────┬──────────────────────┘
        │                                       │
        │        ┌──────────────────────────────┘
        ▼        ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                  Agent Runtime 后端（本次交付的主体）                      │
│                                                                          │
│  🔑 凭证服务            🛡 Tool 风控网关          💰 结账中心              │
│  ─────────────         ─────────────────        ─────────────           │
│  · 统一凭证格式          · 工具白名单二次校验       · 按 session 幂等        │
│  · 按渠道分发            · mandate / 限额核验      · 时长 + token 计费      │
│  · 备用票（降级用）       · 每次调用都留痕          · 摘要写回记忆流         │
│                                                                          │
│                        🧠 记忆层（文字 ↔ 语音共享一条流）                  │
│                                                                          │
│                        📝 文本网关（New API 中转，PR #31~33）              │
└───────┬──────────────────────────────────┬───────────────────────────────┘
        │                                  │
        │ 只创建草案，绝不自动执行            │ 握手超时 / 弱网
        ▼                                  ▼
┌────────────────────────┐      ┌────────────────────────────────────────┐
│  AgentOS Core          │      │  火山 RTC 兜底网（STT + LLM + TTS）      │
│  · 行情 / 账户 / 持仓    │      │  传统级联，保证极端网络下也能出声         │
│  · TradeDraft（待确认）  │      │  本次完整保留，未做任何破坏性改动         │
└────────────────────────┘      └────────────────────────────────────────┘
```

---

## 2. 为什么这么分：两条流，两种诉求

| | 数据面（音频） | 控制面（指令/钱） |
|---|---|---|
| **路径** | 客户端 ⇄ 大模型，直连 | 客户端 → Agent Runtime → Core |
| **图什么** | 极致低延迟、能自然插话 | 可审计、可拦截、可结账 |
| **代价** | 我们看不到音频流 | 多一跳网络 |

音频要是绕经我们的服务器转发，延迟直接翻倍，「盯盘时自然插话」就没了。
但**钱绝不能走直连** —— 所以 AI 一旦要下单，客户端必须把指令 POST 回服务端。

---

## 3. 三条红线（本次落地的核心）

### 🔴 红线一：前端永远不碰交易所

AI 在直连通话里喊「买 0.1 BTC」，客户端**只做转发**：

```
POST /api/voice/tools/execute
  { session_id, tool_call_id, name, arguments }
        ↓
  ① 会话归属校验（不是你的会话 → 404）
  ② 工具白名单二次强校验（不信任客户端报的工具名 → 403）
  ③ mandate / 限额风控（超限 → 403）
  ④ 调 Core：行情等公共数据用 AGENT 凭证；
     持仓 / 额度 / 建草案这类私有账户操作，
     必须带上调用者本人的 USER 凭证（见下方「一次返工」）
  ⑤ 无论成败都写审计
        ↓
  返回标准 tool result，客户端喂回大模型
```

下单类工具**只产生 TradeDraft（待确认草案）**，绝不自动执行。

这条执行路径和原有的文本通道（`/sessions/{id}/tool-calls`）**共用同一份代码**，
两条通道的风控口径不会各写一套、慢慢跑偏。

### 🔴 红线二：结账不许顺手把会话关了

`POST /api/voice/session/end` **只记账，不停任务**。

原因很具体：`DELETE /sessions/{id}` 开头有个短路判断 ——
「已停止且已清理 → 直接返回」。结账端点要是顺手把会话标成已清理，
DELETE 就会跳过 `StopVoiceChat`，**在火山那边留下一个一直在计费的任务**。

所以停止链路仍然只归 DELETE 独有，并有专门的回归测试守着这条线。

结账本身按 `session_id` 幂等：重复上报返回 `duplicate=true`，不重复扣费、
摘要也不会重复写进记忆流。

### 🔴 红线三：降级换渠道，票和路由必须一起换

握手超过 3 秒，客户端带上备用渠道名重新领票：

```
POST /sessions/{id}/token   { "want": ["voice"], "provider": "volc_rtc" }
```

关键在于**光发票不够**。会话会记住 `voice_provider_name`，此后
`voice/start`、`presence`、`StopVoiceChat`、工具结果回灌**全部按它走** ——
否则客户端拿着 volc 的票，服务端却还在原渠道上空跑，bot 根本不会进房。

两条保护：
- 会话还有未清理的任务时**禁止切换**（`409`），否则旧任务在原渠道继续计费；
- 换渠道时把新凭证的 task/room/user 身份一并落库，否则 DELETE 拿旧 ID 判断会漏掉停止。

---

## 4. 文字 ↔ 语音：微信级连贯

两个方向都通了，靠的是**共享同一条记忆流**：

```
先打字后语音   聊天记录 ──→ 签发凭证 / 启动通话时注入 Realtime Session
                            （基础人设永远排最前，不被通话上下文覆盖）

先语音后打字   通话摘要 ──→ 挂断时写回同一条记忆流（source=voice）
                            回到聊天框继续打字，不割裂
```

---

## 5. 一次返工：私有账户工具漏传 USER 凭证

值得记一笔，因为踩点很典型。

主体上线后发现：语音通话里问「我的持仓」「可用额度」会失败。原因是
`/api/voice/tools/execute` 鉴权时拿到了 Core USER 身份，**却没把这个凭证
往下游传**。而 Core 对私有账户操作是按「本人」鉴权的：

```
公共数据（行情 / instrument 目录）  → AGENT 凭证        ✅ 一直是对的
私有账户（持仓 / 额度 / 建草案）    → 调用者本人 USER 凭证 ❌ 漏了
```

executor 层其实有门禁，缺凭证会直接拒（`TradingReadinessError`），对外表现成 409。
**保护是生效的，只是我没喂对东西** —— 属于「安全设计救了一次实现疏忽」。

### 为什么现有测试没抓到

fake 模式下没有装配 AgentOS 客户端，那段门禁整段跳过，工具照样返回假数据。
所以补的回归测试装配了**真 `ToolExecutor` + AgentOS 客户端**，让门禁真的生效，
并在 Core 侧断言收到的是调用者本人的 token：

```python
if request.url.path == "/api/v1/me/trading-readiness":
    assert request.headers["Authorization"] == f"Bearer {_CORE_USER_TOKEN}"
```

改之前这条测试稳定复现 409，改之后返回真实账户数据。

### 一个刻意没改的地方

`/sessions/{id}/tool-calls`（rt_token 旁挂通道）**没有一起改**。它用 rt_token 鉴权，
本就拿不到 USER 凭证 —— 硬塞一个进去只能是拿 AGENT Token 冒名，那是比 409 严重得多的
问题。这条路径的正确用法仍是走 `/messages`。

---

## 6. 可扩展性

新增一家语音供应商（Gemini Live / Grok Voice 等），只需两步：

1. 实现 `VoiceProvider` 协议；
2. 在 `app/voice/registry.py` 注册一个工厂函数。

容器和所有 API 端点**都不用改**。注册表是不可变的，`with_provider()` 返回新实例。

**Azure OpenAI 更省事**，连新 Provider 都不用写：主机名以 `.azure.com` 结尾时，
凭证请求自动从 `Authorization: Bearer` 切成 Azure 要求的 `api-key` 头。
运维只改一个环境变量：

```
AGENT_OPENAI_BASE_URL=https://<资源名>.openai.azure.com/openai
```

按端点二选一而不是两个头都发 —— Azure 不认 Bearer 形式的 API Key，混发只会让
上游报错更难定位。

> Azure 这条代码路径已就绪但**暂未启用**（充值问题），所以先上了 Gemini Live。

### 兑现一次：Gemini Live（PR #38；当前契约已升级为 v1beta / Gemini 3.1）

这是插件化设计的第一次真实检验 —— **`VoiceProvider` 抽象一行没动**，只加了
一个 Provider 实现和一个工厂函数。

三处实测出来的坑，都写进了代码注释、测试与移动端文档：

**① 短期票只认另一个端点**（排查花了一轮）

```
长期 API Key  →  v1beta  …GenerativeService.BidiGenerateContent
短期票        →  v1beta  …GenerativeService.BidiGenerateContentConstrained
```

喂错端点的报错是 `unregistered callers` —— **长得完全像凭证问题**。按官方文档试遍了
查询参数 / 请求头 / 两个 API 版本共四种组合全被拒，差点去要新 key，翻官方 demo
前端代码才看到真正的端点名。

**② 协议级防篡改 —— 比 OpenAI 那条更硬**

`Constrained` 端点执行的是**服务端签票时锁定的 setup**。实测客户端自带一份改过
`systemInstruction` 的 setup，被上游无视：

```
[客户端照抄锁定 setup]          模型说：锁定成功
[客户端篡改 systemInstruction]  模型说：锁定成功   ← 篡改无效
```

所以人设、记忆上下文、**工具白名单**全部锁死在票里。OpenAI Realtime 那边客户端
可以用 `session.update` 改配置，这边物理上改不了。

记忆注入也在真链路上验证过：签票时注入「用户刚问过 BTC 仓位」，客户端只发一句
「我刚才问你什么了？」，模型转写回答 **「你刚才问了 BTC 仓位」** —— 客户端从未
发送这段上下文。

**③ 工具 schema 是 OpenAPI 子集**

`functionDeclarations.parameters` 不认 `additionalProperties` / `$schema`，带上直接
400。我们 18 个内置工具里 9 个带这个字段，转换时递归剔除，工具定义本身不用改。

**传输是 WebSocket，不是 WebRTC。** Gemini 官方没有原生 WebRTC（四个官方示例全是
WSS，WebRTC 只存在于 LiveKit / Pipecat 这类第三方桥接）。「音频不经过 Runtime」这条
架构约束不变，只是客户端换一种连法：接 WSS + 收发 PCM（`audio/pcm;rate=24000`）。

真链路实测：握手 374ms，首个音频包 2842ms。

---

## 7. 交付验证

**测试**
```
967 passed, 6 skipped
受管模块 100% 行 + 分支覆盖
ruff check .   全绿
mypy app       全绿（103 个文件）
新增测试 61 条；新模块自身覆盖率 95% ~ 100%
```

**Staging 线上实测**
```
/healthz   ok
/readyz    200 ready · provider_mode=real · core_credential=valid
           rtc / s2s / ark / 两个回调 全部 configured

契约        /api/voice/tools/execute      ✓
           /api/voice/session/end        ✓
           /token 的 provider 入参 + 409  ✓
           凭证的 endpoint_url / expires_at / allowed_tools  ✓

鉴权        无凭证        → 401
           伪造 token    → 401（本地测试用的假 token 在线上被拒，
                              证实走的是真 Core 鉴权而非 Fake）

Core 只读   行情 BTC/ETH stale=false · Catalog 5 个 instrument all_fresh
           Account Summary 1 持仓 11 订单 · write_performed=false
```

**回归**：火山兜底链路的既有测试（`test_volc_rtc` / `test_voice_lifecycle` /
`test_realtime_security` / `test_volc_callback`）全绿，未受影响。

**顺带修的**：CI 原先只在 `push: main` 触发，PR 上没有任何 check ——
等于把 CI 当事后告警而非合并准入。已加 `pull_request` 触发（PR #35），
PR #36 起已验证生效：test job 自动运行，deploy job 正确 skipped。

**线上没法自证的一点**：USER 凭证透传（第 5 节）需要一个真实的 Core USER Token
和属于该用户的会话才能触发，我手里没有。线上能验的只是「服务活着、端点在、
无效凭证被拒」。这条的保障来自 CI 里那条集成测试（真 executor + 真门禁），
**改前稳定复现 409、改后返回真实账户数据**。真机侧请补验：语音通话里问
「我的持仓」「可用额度」不再报错。

**交付时间线**

| PR | 内容 |
|---|---|
| #34 | 主体：凭证标准化、风控执行、通话结账、记忆衔接 |
| #35 | CI 门禁：补 `pull_request` 触发 |
| #36 | 契约补标：`/token` 的 409 写进 OpenAPI |
| #37 | USER 凭证透传 + Azure 兼容 + 修掉两条空断言 |
| #38 | Gemini Live 原生音频渠道（插件化设计的第一次兑现） |

---

## 8. 还没做的（不阻塞 staging 验证，但要排期）

| 事项 | 说明 | 紧急度 |
|---|---|---|
| **计费账本落库** | 现在是 Runtime 进程内实现，**多 worker 或重启会丢账**。Core 暂无计费接口，需落 PG 或迁到 Core | 🔴 正式上线前必须做 |
| **真机端到端** | 说话 → ASR → 工具 → 可听音频 → 挂断摘要落库；**含问持仓/额度**（USER 凭证透传的线上验证） | 🟡 验收必需 |
| **预冻结 / 实时扣费** | 目前是挂断后一次性结算，没有通话前预冻结额度 | 🟢 看业务是否需要 |
| **Node 20 弃用** | CI 的 checkout/setup-python action 被强制跑在 Node 24，runner 收紧会挂 | 🟢 找时间升版本 |

> 说明：架构图里画的是**当前真实跑着的东西**。渠道现状：火山 RTC 为默认主力、
> Gemini Live 已接入（PR #38）、OpenAI 可用、Azure 代码就绪但因充值暂未启用、
> Grok Voice 仍是预留扩展位。交易所侧一律经 AgentOS Core，Runtime 不直连任何交易所。

---

## 附：本次新增/改动的关键文件

```
app/voice/registry.py        Provider 注册表（插件化）
app/voice/gemini_live.py     Gemini Live 原生音频 Provider
app/voice/usage.py           用量计费与幂等结算
app/voice/provider.py        凭证标准化 + 上下文注入
app/api/voice.py             /api/voice 两个端点
app/api/tool_execution.py    风控执行的唯一落点（两条通道共用 + USER 凭证透传）
app/runtime/context_builder.py   文字→语音的记忆注入
tests/test_voice_webrtc_gateway.py   45 条测试
tests/core_fixtures.py       Core Trading Readiness 共享夹具
tests/test_gemini_live_provider.py   16 条测试
scripts/check_gemini_live_connectivity.py   落地前的四步连通性自检
docs/api/agent-mobile/voice-webrtc.md   移动端契约文档
```
