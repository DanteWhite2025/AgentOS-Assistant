# yesup-agent-runtime · 技术栈选型调研

- 日期：2026-07-31
- 负责人：wesley
- 范围：Repository 3 `yesup-agent-runtime` 的技术栈逐项验证与选型（对应 README「Day 0 代码资产盘点 / Day 1 架构冻结」）
- 版本说明：文中版本为 **2026-07 调研快照**，安装时以当下最新稳定版为准并跑冒烟测试，切勿盲抄旧教程；生产环境一律 **pin 已测版本**。

---

## 0. 摘要与核心决策

README 给的技术栈方向（Python 3.12 / FastAPI / Pydantic v2 / Redis / PostgreSQL / OTel / pytest）**整体成立、予以保留**。真正需要拍板的是三个"或"字号选型，结论如下：

| 决策点 | README 原文 | 调研结论 | 一句话理由 |
|---|---|---|---|
| 编排层 | 「LangGraph **或**自研显式状态机」 | ✅ **自研显式 async 状态机 + Pydantic AI 承接单 agent** | 工具面固定、LLM 循环在 OpenAI 云端、后端不在音频关键路径，LangGraph 的持久化/checkpoint 卖点对 MVP 零增量、反收学习曲线税 |
| LLM Provider | 「可插拔 LLM Provider」 | ✅ **分两层**：文本推理用 Pydantic AI（可换厂商）；实时语音用官方 `openai` SDK 单独实现 | 语音是"传输/会话协议"问题，文本是"模型可替换性"问题，强行统一是负资产 |
| 语音接入 | 「OpenAI Realtime API」 | ✅ 官方 `openai` SDK 走 **Realtime GA**（`client_secrets` 临时凭证），Beta 接口已于 2026-05-12 移除 | 与接口设计方案 B 一致 |

其余（Redis / PostgreSQL / OTel / pytest）为具体库落地建议，见第 4–7 节。

## 1. 推荐分层架构

```text
┌───────────────────────────────────────────────┐
│ 骨架层（自研 async 状态机）                     │
│ session_manager / intent_router / turn_manager │
│ + 4 类 agent 的显式调度（immutable turn state） │
└───────────────┬─────────────────┬───────────────┘
                │                 │
   ┌────────────▼──────┐   ┌──────▼─────────────────┐
   │ Agent 执行层        │   │ 语音层（单独处理）      │
   │ Pydantic AI Agent  │   │ openai SDK · Realtime GA│
   │ @tool → core REST  │   │ 签发 client_secret      │
   │ 可插拔文本模型      │   │ 锁 session config       │
   └────────────────────┘   │ 工具旁挂执行            │
                            └─────────────────────────┘
```

分界原则：**语音会话在 OpenAI 云端 + 移动端；后端只做文本/工具控制平面。** Pydantic AI 只服务文本层，不接管 Realtime 音频（其 Realtime 支持仍是 open issue #1447）。

## 2. 编排层：自研状态机 + Pydantic AI（不上 LangGraph）

| 维度 | LangGraph 1.2.9 | 自研 async 状态机 | Pydantic AI 2.20+ / OpenAI Agents SDK |
|---|---|---|---|
| 成熟度 | 高（2026 事实标准） | 取决于自己，可控性最高 | Pydantic AI Production/Stable；Agents SDK 官方出品、原生 gpt-realtime-2.1 |
| 延迟适配 | ⚠️ agent 作为 node 会缓冲丢流式，伤 TTFT（语音场景已知坑） | ✅ 每步 await 自控，工具旁挂精确控制在单 turn | ✅ 同 async 栈，流式友好 |
| 与 FastAPI 集成 | 中（需 graph-stream→SSE 胶水） | ✅ 就是普通 FastAPI 代码 | ✅ 同源无摩擦 |
| 学习曲线 | ⚠️ 陡（state/node/edge + 样板） | ✅ 最低 | ✅ 低（已用 Pydantic v2） |
| 锁定风险 | ⚠️ 高（最佳体验绑 LangSmith/LangChain） | ✅ 无 | 低–中 |
| 两周落地 | ⚠️ 差（过度工程） | ✅ 最强 | ✅ 强 |

**推荐**：骨架用自研 async 状态机；每个 agent（avatar/monitor/strategy/fund_manager）= 一个 Pydantic AI Agent，工具即 `@agent.tool` 包装的 core REST 调用，输入输出全 Pydantic 校验（天然满足系统边界校验规范）。

**LangGraph 留作后续升级项**：当 planner 演化成真正的多智能体有状态图、且需要跨会话持久化与强 trace 审计时再迁移——只要工具层保持干净的 registry + 统一响应 envelope，迁移成本可控。

**风险**：自研的风险是复杂度失控时返工，靠"清晰的状态机边界 + tool registry"缓解；Pydantic AI 不覆盖 Realtime 音频，职责边界要划清。

## 3. LLM Provider：文本可插拔 / 语音单独

**分层是本节最重要结论。**

- **实时语音主通道**：官方 `openai` SDK 的 **Realtime GA**。鉴权走 `POST /v1/realtime/client_secrets` 签发临时凭证，移动端 WebRTC 直连（见接口文档 `voice-webrtc.md`）。为语音单独定义窄接口 `VoiceSession` / `RealtimeTransport`，封装 session 生命周期、GA 事件（`response.output_audio.delta` 等）、function-call 回调；实现体第一阶段只做 OpenAI 一个。**不要**把它塞进通用 `chat.completions` 抽象或 LiteLLM Proxy 语音透传（多一跳、成熟度弱、伤延迟）。
- **异步文本/策略推理层**：这里才是"避免被单一模型锁死"的真正落点。定义 `LLMProvider` Protocol（`complete/stream/tool-calling`），实现用 **Pydantic AI 的 Model 抽象**（与 Pydantic v2 同源，`OpenAIChatModel` → 其他厂商一行切换，覆盖 OpenAI/Anthropic/Gemini/DeepSeek/Bedrock/OpenRouter/LiteLLM）。
  - 备选：若团队更想要"OpenAI 兼容单端点 + 统一成本/日志"，用 **LiteLLM Python SDK**（进程内，零基础设施）；两者可共存（Pydantic AI 可把 LiteLLM 当 provider）。
  - **暂不引入**独立部署的 LiteLLM Proxy / Portkey / 托管网关——需要 PG+Redis+YAML，MVP 阶段过度工程；待有多云成本治理/RBAC/审计需求再作为独立层加入。

| 方案 | 覆盖 Realtime 语音 | MVP 适配 |
|---|---|---|
| 官方 SDK + 自建薄抽象 | ✅（唯一可靠的语音路径） | 语音层首选 |
| Pydantic AI Model 抽象 | ❌ 不覆盖音频流 | 文本层首选 |
| LiteLLM SDK | 部分（不建议做语音主路径） | 文本层备选 |
| 托管网关（OpenRouter/Portkey…） | 基本不面向语音主通道 | MVP 过重，后续治理层 |

**风险**：Realtime Beta 已移除，旧示例会失效；LiteLLM 版本节奏极快需 pin；抽象泄漏（Realtime 的 function-call/打断无法被文本抽象覆盖）。

## 4. Web 与核心库版本（2026-07 快照）

| 包 | 快照版本 | 说明 |
|---|---|---|
| `python` | 3.12+ | 与项目约束一致 |
| `fastapi` | 当前稳定线（0.11x–0.13x，安装取最新） | `pip install "fastapi[standard]"`；默认 Pydantic v2 |
| `pydantic` | 2.11.x | Rust 校验核心 |
| `uvicorn` | 0.35.x | 配 uvloop 提吞吐 |
| `httpx` | 随 `fastapi[standard]` | 异步 HTTP 客户端；OpenAI SDK 底层也用它 |
| `openai` | 2.5x（含 Realtime GA + function calling） | 语音主通道 |
| `pydantic-ai` | 2.20+ | 文本推理层可插拔首选 |
| `litellm` | 1.x（**务必 pin**） | 文本层备选 / 未来治理层 |

## 5. Redis：会话状态与分布式锁

**推荐**：用 `redis-py` 的 `redis.asyncio`；会话状态放 Redis（配 TTL），**不放进程内**；分布式锁先用 `redis-py` 自带 `Lock`，不要一上来上 Redlock。

- `aioredis` 已于 2021 年归并入 `redis-py`（`redis.asyncio`）且早已归档——2026 年不要再引入这个历史包名。
- 会话状态：多副本 + 跨 worker 共享 + 需分布式锁的场景，进程内字典在扩容/重启后即丢，**用 Redis**，进程内只做请求级缓存。
- 锁：`redis.asyncio.lock.Lock`（Lua 脚本、单实例语义）覆盖 95% 场景，MVP 首选；`aioredlock`（多 master Redlock）只有部署多独立 Redis master 抗单点故障才需要，MVP **不需要**。
- 版本：`redis>=6.2,<7`（含连接池、`redis.asyncio`、内置 `Lock`）；连接用 `ConnectionPool` + FastAPI `lifespan` 管理。
- 风险：内置 `Lock` 主从切换瞬间理论上可能双持锁 → 审计去重靠幂等设计兜底；会话 key **务必设 TTL** 防内存泄漏。

## 6. PostgreSQL：会话索引 + 审计写入

**推荐**：`SQLAlchemy 2.x async`（driver 用 `asyncpg`）+ `Alembic`（async 模板）；不裸用 asyncpg，也不必为这个轻量后端引入 SQLModel。

| 方案 | 取舍 |
|---|---|
| 裸 asyncpg | 最快最省依赖，但要手写 SQL/连接池/映射，会很快重造半个 ORM。不作主线 |
| **SQLAlchemy 2.x async** ✅ | 官方一等 async，底层就是 asyncpg；轻量场景只用 Core（`insert()/select()`）即可，审计写入就是一条 `insert()` |
| SQLModel | SQLAlchemy+Pydantic 薄封装；async 成熟度/文档滞后，且这里只有两三张表，共享模型收益小，非必需 |

- 版本：`sqlalchemy[asyncio]>=2.0`；`asyncpg` 取最新稳定（"必须 pin <0.29" 是 2024 旧问题，2026 已兼容，冒烟确认即可）；`alembic` 1.18.x，异步项目用 `alembic init -t async`。
- 红线：本服务对业务库**只读**——用独立、权限受限的 DB 账号（业务库只读、审计表读写自己的），连接层隔离，避免迁移误碰业务表。
- 审计 `args_hash` 规范化（**冻结规范，全项目统一**）：
  1. 序列化 `json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`——`sort_keys` 递归排序保证嵌套 key 顺序一致；固定 `separators` 消除空格差异；`ensure_ascii=False` 统一中文编码。
  2. **金额禁用 float**：参数里金额一律字符串 Decimal（与全局约定一致），杜绝浮点序列化在不同平台/精度下产生不同字符串。
  3. 可选字段规范化：入 hash 前剔除值为 `None` 的键（或统一补默认），避免"带/不带可选字段"的同义调用产生不同 hash。
  4. 对规范化后的字符串取 SHA-256。`result_ref` 存指针不存大 payload，防审计表膨胀。
- async ORM 注意 session 不跨协程、显式 `selectinload`。

## 7. 可观测性：OpenTelemetry + GenAI semconv

**推荐**：官方 OTel SDK + contrib 自动埋点覆盖 FastAPI/httpx/asyncpg；LLM 埋点用 GenAI semantic conventions，但清楚它 2026-07 **仍是 Development（未 Stable）**，需 pin 版本并封装隔离。

- 自动埋点（无需改业务代码）：`opentelemetry-instrumentation-fastapi` / `-httpx`（覆盖出站 core/OpenAI 调用）/ `-asyncpg`。
- LLM 埋点：`opentelemetry-instrumentation-openai-v2`（GenAI `gen_ai.*` 语义，OpenAI SDK 埋点最成熟）。2026-06 起 GenAI semconv 拆到独立仓走快节奏发布，**尚无字段 Stable**。
- 审计契合：`trace_id` 取自 span context，`session_id/tool_name/args_hash/result_ref` 作为 span attribute + 双写 PG；自定义 `tool.*` span 包住每次工具调用。
- 版本：`opentelemetry-sdk`/`-api`/`-exporter-otlp`（API/SDK 已 Stable，取同序列最新）；instrumentation 与 SDK 对齐。启动可用 `opentelemetry-instrument` 零代码注入。
- 风险与红线：GenAI semconv 未 Stable → pin 版本 + 封装在自有 wrapper 后；**高并发下别开 prompt/completion 内容捕获**（会把语音转写原文写进 trace，触隐私合规且撑爆后端）——审计只落 `args_hash` 正是对的方向。

## 8. 测试：pytest 异步栈

**推荐**：`pytest` + `pytest-asyncio`（1.x，`asyncio_mode=auto`）+ `httpx.AsyncClient(ASGITransport)` 做 in-process 集成测试；上游（OpenAI / yesup-core）用 `respx` 在 HTTP 层拦截。

- `pytest-asyncio>=1.0` 有破坏性变更：移除 `event_loop` fixture，改用 `loop_scope`；别抄旧教程。
- 测 FastAPI：`httpx.AsyncClient(transport=ASGITransport(app=app))` 进程内打 ASGI，不开真实端口。
- Mock 上游：`respx`（httpx 传输层拦截，对 OpenAI SDK 与 core httpx client 都透明）；**不要** monkeypatch SDK 方法。务必覆盖失败分支（超时/429/5xx），验证审计里 `result_ref` 错误态落库。
- Redis/PG：Redis 用 `fakeredis[async]`；PG 审计核心路径用 `testcontainers[postgresql]` 起真库跑集成，单测用事务回滚隔离。
- 版本：`pytest-asyncio` 1.4.x、`respx`、`fakeredis[async]`、`testcontainers[postgresql]`。

## 9. 推荐依赖清单（汇总）

| 领域 | 包 | 快照版本 |
|---|---|---|
| Web | `fastapi[standard]` / `uvicorn` | 当前稳定线 / 0.35.x |
| 校验 | `pydantic` | 2.11.x |
| 编排-agent 执行 | `pydantic-ai`（或 `pydantic-ai-slim`） | 2.20+ |
| 语音 | `openai`（Realtime GA） | 2.5x |
| 文本 provider 备选 | `litellm`（pin） | 1.x |
| Redis+锁 | `redis`（含 `redis.asyncio`+`Lock`） | >=6.2,<7 |
| PG ORM/Core | `sqlalchemy[asyncio]` | >=2.0 |
| PG driver | `asyncpg` | 最新稳定（冒烟确认） |
| 迁移 | `alembic`（async 模板） | 1.18.x |
| 可观测性 | `opentelemetry-sdk`/`-api`/`-exporter-otlp` + `-instrumentation-{fastapi,httpx,asyncpg}` | 同序列最新 |
| LLM 埋点 | `opentelemetry-instrumentation-openai-v2`（pin，未 Stable） | pin |
| 测试 | `pytest` + `pytest-asyncio>=1.0` + `httpx` + `respx` + `fakeredis[async]` + `testcontainers[postgresql]` | pytest-asyncio 1.4.x |

## 10. 贯穿性红线

- 语音走 Realtime **GA**（`client_secrets`），不用已移除的 Beta。
- 文本可插拔 / 语音单独处理，**不为"统一"牺牲语音链路能力**。
- 业务库只读、受限账号、审计表权限隔离。
- 审计只落 `args_hash`（哈希）与 `result_ref`（指针），**不落原文**（隐私 + 成本）。
- 生产 pin 版本；未 Stable 的 GenAI semconv 封装在自有 wrapper 后。
- Redis 会话 key 设 TTL。
- `args_hash` 按第 6 节冻结规范计算（`sort_keys` + 固定 `separators` + 金额字符串 + 剔除 `None` + SHA-256），全项目统一，禁止各处自行拼串。

## 11. 与接口设计的一致性

本调研与 [`../api/agent-mobile/`](../api/agent-mobile/) 接口契约完全对齐：
- 语音方案 B（移动端直连 OpenAI + 服务端锁 session + 工具旁挂）↔ 本文第 3 节语音单独处理。
- 工具旁挂审计字段（`trace_id/session_id/tool_name/args_hash/result_ref`）↔ 第 6、7 节。
- Agent 只调 core 受控工具、只创建 TradeDraft ↔ 第 2 节 Pydantic AI `@tool` 包装 core REST。

## 12. 已冻结决策（2026-07-31）

原开放项已拍板：

1. **文本推理层：Pydantic AI**（与 Pydantic v2 同源、类型安全更干净）。
2. **多 Agent Handoff：暂不引入 OpenAI Agents SDK**，MVP 保持单 Agent 入口，路由交由自研状态机处理；待真正需要语音内 handoff 再评估。
3. **事件队列：第一阶段不自建 NATS/Redis Streams**，保持架构最简。

> 衔接提示：不自建队列后，Monitor Agent 的事件源第一阶段需明确——建议降级为"按需/定时查询 core"或由 core 主动 push（webhook），避免监控悬空。

### 12.1 目录结构精简（同行评审采纳 2026-07-31）

评审在选型基础上进一步"拒绝过度工程"，采纳如下精简（相对 README `app/` 原目录）：

1. **删除后端 `avatar/` 目录**（viseme/emotion/animation 三件套）。语音走方案 B、音频不过后端，口型（viseme）由**客户端**解析 OpenAI 音频/事件原生完成，后端不中转音频算口型（多写多错、增延迟）。
   - ⚠️ **一处保留，避免与接口契约脱节**：**文本通道**（通道②，走 RT）的 `avatar.event`（`emotion`/`state`）由 RT 生成文本回复时**顺带标注**，仍按 `ws-events.md` 下发。这只是 response 组装时的一个轻量标签，**不需要独立 avatar 子系统**。语音通道口型完全在客户端。
2. **扁平化 `agents/` 目录**：不写 avatar/monitor/strategy/fund_manager 四个子目录/类，改为**单一通用 `AgentRunner` + 配置/prompt 模板**区分身份（契合已冻结的"MVP 单 Agent 入口、路由交自研状态机"）。待某 Agent 出现真正独有逻辑再拆。
3. **PostgreSQL 用 SQLAlchemy 2.x async Core 直插审计**，不定义 ORM 级联关系（本就是第 6 节推荐，评审再次确认）。

**精简后 `app/` 建议（相对 README 的 diff）**：
- ✂️ 删 `avatar/`（口型/表情移交客户端）
- ✏️ `agents/{avatar,monitor,strategy,fund_manager}/` → `agents/runner.py` + `agents/profiles/`（各 agent 的 model/tools/prompt 配置）
- ✅ 其余保留：`api/ runtime/ orchestration/ tools/ voice/ policies/ memory/ telemetry/`

## 13. 参考链接（精选）

**编排 / Agent 框架**
- LangGraph/LangChain 1.0 GA：https://www.langchain.com/blog/langchain-langgraph-1dot0
- LangGraph 用于语音 AI 的限制（node 缓冲丢流式）：https://buildingagenticai.com/blog/langgraph-for-voice-ai-agents/
- 三框架对比（LangGraph vs OpenAI Agents SDK vs PydanticAI）：https://open-techstack.com/blog/langgraph-vs-openai-agents-sdk-vs-pydanticai-2026/
- Pydantic AI 文档：https://ai.pydantic.dev/ ｜ Realtime open issue #1447：https://github.com/pydantic/pydantic-ai/issues/1447
- OpenAI Agents SDK（Realtime）：https://openai.github.io/openai-agents-python/realtime/quickstart/

**LLM Provider / 语音**
- OpenAI Realtime GA（WebRTC / client_secrets）：https://developers.openai.com/api/docs/guides/realtime-webrtc
- OpenAI 变更日志（Beta 移除）：https://platform.openai.com/docs/changelog
- LiteLLM：https://github.com/BerriAI/litellm ｜ Realtime：https://docs.litellm.ai/docs/realtime
- LLM 网关横评 2026：https://www.requesty.ai/blog/litellm-vs-portkey-vs-openrouter-best-llm-gateway-2026

**存储 / 可观测性 / 测试**
- SQLAlchemy 2 async（miguelgrinberg）：https://blog.miguelgrinberg.com/post/sqlalchemy-2-in-practice---chapter-7-asynchronous-sqlalchemy
- redis.asyncio 用法：https://oneuptime.com/blog/post/2026-03-31-redis-async-python-redis-asyncio/view
- Redis 分布式锁官方：https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/
- OTel GenAI semconv 现状（2026-07）：https://john-hodge.com/blog/opentelemetry-genai-semantic-conventions/
- OTel FastAPI instrumentation：https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/fastapi/fastapi.html
- FastAPI 异步测试（ASGITransport）：https://fastapi.tiangolo.com/advanced/async-tests/
- respx mock OpenAI：https://tonyaldon.com/2026-02-12-mocking-the-openai-api-with-respx-in-python/
> **历史评估，实时语音结论已于 2026-08-07 更新。** 当前默认是火山 RTC +
> StartVoiceChat；OpenAI Realtime 不再是部署必需项。现行契约见
> `docs/api/agent-mobile/`。
