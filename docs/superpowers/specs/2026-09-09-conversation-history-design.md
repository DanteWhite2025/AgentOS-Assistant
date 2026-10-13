# 历史对话记录（Conversation History）设计

- 日期：2026-09-09
- 分支：`feat/session-history-list`
- 状态：待评审

## 1. 背景

移动端设计稿要求在 Agent 聊天页提供「历史」入口：弹出历史对话列表，每条显示标题（如「BTC行情分析」）与时间，点击可回看并继续提问。对标形态是币安 AI 助手的会话侧栏。

排查后确认，后端目前**不具备**该能力，且缺口比表面更深：

| 表象 | 实际情况 | 位置 |
| --- | --- | --- |
| 「已经有 history 接口了」 | `GET /sessions/{id}/history` 返回的是**工具调用审计记录**（`tool_name` / `args_hash` / `result_ref`），不是对话消息 | `app/api/agent.py:2439` |
| 「history 支持分页」 | `cursor` 入参从未被使用，`next_cursor` 恒为 `None` | `app/api/agent.py:2439-2465` |
| 「对话内容存着呢」 | 对话正文只存在 `Session.turns`，裁剪到最近 3 轮 / 6000 字 | `app/runtime/session.py:45-46` |
| 「会话记录能查回来」 | Session 存 Redis，TTL 3600s，不活动一小时即消失 | `app/container.py:264` |
| 「从审计表反查用户会话」 | `AuditRecord` 不含 `user_id`（刻意的隐私红线） | `app/audit/store.py:14-22` |

结论：**一小时前的对话内容在系统中已不存在**。本功能不是「加个标题和列表接口」，而是要先补上对话正文的持久化层。

## 2. 目标与非目标

### 目标

1. 持久化文本会话的对话正文，支持完整回放。
2. 提供按用户维度的会话列表（标题 + 时间），支持分页。
3. 支持从历史会话继续提问，且新内容追加进同一条历史。
4. 自动生成会话标题。
5. 30 天自动清理，存储可控。

### 非目标（本期明确不做）

- 语音会话入库（二期；转写回写路径需另接 gemini_live / 火山两条链路）。
- 删除 / 重命名历史会话（二期）。
- 放宽模型上下文窗口（见 §3.1，这是刻意保留的设计）。
- 改造既有 `GET /sessions/{id}/history`（保持向后兼容，仅补文档说明）。

## 3. 已确认决策

### 3.1 UI 完整回放 + 模型滑动窗口

回放读 PG（完整），模型上下文只取最近 3 轮（`_MAX_HISTORY_TURNS = 3`、`_MAX_HISTORY_CHARS = 6000`）。

这不是妥协，是刻意设计。仓库中已有同一判断的记录（`app/runtime/session.py:53-54`）：

> 只存纯文本，不存工具调用与结果：过期的行情数字留在上下文里会被模型当成当前数据用，而那种错比串人更难发现——数字看着合理，只是过期了。

金融场景下把整段历史灌回上下文会引入过期行情导致的幻觉，且这类错误极难发现。因此窗口维持现状，不放宽。

**产品侧须同步**：用户看到的是完整历史，但 AI 只「记得」最近 3 轮。

### 3.2 其余决策

| 项 | 结论 |
| --- | --- |
| 命名空间 | 新开 `/v1/agent/conversations`，与运行时 `/sessions` 解耦 |
| 标识符 | 对外一律 `conversation_id`，取值即 `session_id`，见 §3.4 |
| 标题 | LLM 异步生成，失败降级为首条用户消息截断 |
| 覆盖范围 | 一期仅文本会话 |
| 保留期 | 30 天自动清理 |

### 3.3 为什么另起命名空间

`session` 是运行时对象：Redis 1 小时 TTL，携带约 50 个 `voice_*` 遥测字段。`conversation` 是持久化记录，生命周期完全不同。

若做成 `GET /sessions` 返回列表，客户端会自然地认为返回的 `session_id` 可直接用于 `POST /sessions/{id}/messages`，而 Redis 中该会话早已过期，结果是 404。两个概念必须在 URL 上就分开。

### 3.4 标识符命名：对外 `conversation_id`，对内 `session_id`

同一个取值（`sess_xxx`）在两层用不同名字，映射只发生在 API schema 层：

| 层 | 名称 | 位置 |
| --- | --- | --- |
| API 路径与响应 | `conversation_id` | `/conversations/{conversation_id}`、`ConversationSummary.conversation_id` |
| DB 列与领域模型 | `session_id` | `agent_conversations.session_id`、`agent_conversation_messages.session_id` |

对内保留 `session_id`，因为它**确实就是** session 的 id：resume 要拿它重建 `Session`，两张表也靠它关联，改名只会让 store 层读起来别扭。

对外统一 `conversation_id`，因为客户端面对的是持久化记录，不该以为自己拿到了一个可用的运行时会话。

**客户端心智**：拿 `conversation_id` 去 resume，拿回来的 `SessionCredentials.session_id` 才是用来连 WebSocket 的。这一次转换正是「历史记录 → 活跃会话」的真实生命周期跃迁，不是命名冗余。

## 4. 数据模型

新增包 `app/history/`，遵循仓库既有的 PG 模块模式（模块级 `MetaData` + `sa.Table` + `create_schema()`，无 Alembic，参照 `app/news/pg_store.py`、`app/memory/pg_store.py`）。

```
app/history/
  models.py      # ConversationSummary / ConversationMessage 领域模型
  store.py       # ConversationStore Protocol + 游标编解码 + 内存实现
  pg_store.py    # SQLAlchemy 表定义 + PG 实现
  titler.py      # 标题生成
  purger.py      # 保留期清理循环
```

### 4.1 `agent_conversations`

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `session_id` | `String(64)` PK | 与运行时 `Session.session_id` 同值 |
| `user_id` | `String(64)` | Core 权威 user_id |
| `title` | `String(200)` NOT NULL | 建行时即初始化，永不为 `NULL`，见 §4.4 |
| `modality` | `String(16)` NOT NULL default `"text"` | 本期恒为 `"text"`，见 §4.5 |
| `created_at` | `DateTime(timezone=True)` | |
| `last_message_at` | `DateTime(timezone=True)` | 列表排序键 |
| `message_count` | `Integer` | 列表展示用 |

索引：`Index("ix_conversations_user_time", "user_id", "last_message_at")`（列表主查询）、`Index("ix_conversations_created", "created_at")`（清理扫描）。

### 4.2 `agent_conversation_messages`

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | `BigInteger` PK autoincrement | 游标次级排序键，保证同毫秒稳定 |
| `session_id` | `String(64)` | |
| `role` | `String(16)` | `user` \| `assistant` |
| `content` | `Text` | 已剥离追问 chips 的正文 |
| `created_at` | `DateTime(timezone=True)` | |

索引：`Index("ix_messages_session_time", "session_id", "created_at", "id")`。

### 4.3 时区

回读一律过 `ensure_utc()`。SQLite 测试会返回 naive datetime，此坑在 `app/strategy/equity_pg_store.py:33-40` 与 `app/news/pg_store.py:75` 已各踩过一次。

### 4.4 `title` 非空：写时初始化，不在读路径计算

`title` 建行时即写入 `user_text[:30]`，LLM 生成完成后 `UPDATE ... SET title = :llm_title` 覆盖。

不采用「存 `NULL`、读列表时降级为首句截断」的方案。`agent_conversations` 不存首条消息正文，读路径要算降级值就必须 JOIN 或子查询 `agent_conversation_messages`——列表接口是高频路径，为一个展示字段引入联表不划算，写时初始化则让列表查询恒为单表、零 JOIN。

副作用是列表接口逻辑归零：查出来什么就返回什么，没有分支。

### 4.5 `modality` 提前落列

本期只写 `"text"`，但列现在就建。

项目靠 `metadata.create_all` 建表且无 Alembic，而 `create_all` **对已存在的表不补列**。上线后再加列必须手写 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`（参照 `app/news/pg_store.py:115-122`），并承担存量数据回填的风险。二期语音接入必然需要这个字段，现在加的成本是一行，将来加的成本是一次线上 DDL。

## 5. 写路径

挂载点：`SessionRunner._save_turn`，`app/agents/runner.py:562-589`。

该方法已具备理想语义：追问 chips 已剥离（`body`）、空回合已跳过、异常已吞掉且注释写明「任何失败都只丢历史，绝不阻断回合」。持久化沿用同一契约。

改动后的顺序：

1. 计算 `body`（现有逻辑，不动）。
2. `body` 为空则返回（现有逻辑，不动）。
3. **新增**：写 PG —— `conversation_store.record_turn(session_id, user_id, user_text, body, now)`。
4. 现有逻辑：读取 Redis session，若已过期则返回，否则 `with_conversation_turn` 回写。

**PG 写入放在 Redis 早退判断之前**。这一轮确实发生过，耐久记录不应因运行时会话恰好过期而丢失。

`record_turn` 的行为：

- 会话行不存在 → 插入 `agent_conversations`，`title = user_text[:30]`、`modality = "text"`，并触发标题生成（§7）。
- 会话行已存在 → 更新 `last_message_at`、`message_count += 2`，**不动 `title`**（避免覆盖 LLM 已生成的标题）。
- 插入两条 `agent_conversation_messages`（user + assistant）。

**会话行懒创建**：只在首轮落库时创建，而非 `POST /sessions` 时。否则建了会话却一句没聊的空记录会污染历史列表。

失败处理：整体包在现有 `try/except` 中，`logger.warning` 后继续，绝不阻断回合。

## 6. 读路径

三个接口，鉴权全部复用 `get_authenticated_user`（`app/api/deps.py:39-46`）。

所有权语义严格对齐 `owned_session`（`app/api/deps.py:49-61`）：**不存在与跨用户一律折叠成 404**，避免泄漏标识符。

### 6.1 `GET /v1/agent/conversations`

| 参数 | 约束 |
| --- | --- |
| `limit` | `Query(default=20, ge=1, le=50)` |
| `cursor` | `Query(default="")` |

响应 `ConversationPage { items, next_cursor }`，`items` 为 `ConversationSummary { conversation_id, title, modality, created_at, last_message_at, message_count }`（对外命名见 §3.4）。

按 `user_id` 过滤，`last_message_at DESC, session_id DESC` 排序。**单表查询，无 JOIN、无子查询**（`title` 已在写时初始化，见 §4.4）。

游标为 keyset 分页，编码照抄 `app/news/store.py:19-35`：`base64url(f"{last_message_at.isoformat()}|{session_id}")`，去除 padding。非法游标抛 `ValueError`，路由层转 400（照抄 `app/api/news.py:123-125`）。

`title` 恒非空，客户端无需处理 `null`。

### 6.2 `GET /v1/agent/conversations/{conversation_id}/messages`

同款 `limit` / `cursor` 分页，按 `created_at ASC, id ASC` 返回（回放是正序）。

游标编码 `base64url(f"{created_at.isoformat()}|{id}")`。

先校验会话归属，不属于当前用户或不存在 → 404。

### 6.3 `POST /v1/agent/conversations/{conversation_id}/resume`

请求体复用 `SessionCreateRequest`（`app/api/schemas.py:49-52`：`modality` / `locale` / `client_info`），响应与 `POST /sessions` 一致，客户端可复用既有处理逻辑。

行为（rehydrate）：

1. 校验 `agent_conversations` 中该行归属当前用户，否则 404。
2. Redis 中该 `session_id` 仍存活 → 复用该 Session，跳到第 5 步。
3. 否则从 PG 按 `created_at DESC, id DESC` 取最近 6 条消息（= 3 轮 × user/assistant），反转为正序后按 `(user, assistant)` 配对还原为 `ConversationTurn`，经 `with_conversation_turn` 逐轮灌入（自动套用 3 轮 / 6000 字裁剪，不绕过），以**原 session_id** 重建 Session 写回 Redis。落单的消息（配不成对）丢弃，不构造半轮。
4. `scope` 走与 `create_session` 完全相同的派生逻辑（`Scope(agents=("avatar", "strategy"), tools=container.registry.names())`，`app/api/agent.py:729`），**不从 PG 恢复**。
5. **无论第 2 步命中与否，一律换发新凭证**并返回完整的 `SessionCredentials`。

#### scope 必须重新派生（安全红线）

`Scope.tools` 是 RT 二次校验的唯一权威来源（`app/runtime/session.py:26`）。若从历史快照恢复，工具策略收紧后旧会话仍持有旧白名单——这是提权口子。resume 只恢复对话内容，权限一律按当前策略重算。

#### 凭证必须换发（不是「可能过期」，是「基本必然过期」）

`rt_token_ttl_seconds` 默认 **300 秒**（`app/config.py:313`），而 Redis session TTL 是 **3600 秒**（`app/container.py:264`）。两者差 12 倍，意味着 Redis 命中的会话中，rt_token 绝大多数情况下**已经过期**。

因此第 2 步不能「直接返回」——必须走 `container.token_service.issue_rt_token(session_id, user.user_id, scope.tools, now)`（签名见 `app/api/agent.py:782-784`）换发，否则客户端拿着死 token 去连 WebSocket 必然失败。

实现上把 `create_session` 中「派发凭证并组装 `SessionCredentials`」的那段（`app/api/agent.py:735-796`，含 voice 分支与 `_voice_schema`）抽成共享 helper，供 `create_session` 与 `resume` 复用。好处有二：契约天然一致，不会漂移；二期语音接入时 resume 零改动。

幂等性说明：resume 幂等的是**会话状态**（不会重建已存在的 Session、不会重复灌历史），而非返回值——每次调用都返回新的 `rt_token`，这是预期行为。

### 6.4 既有接口的文档澄清

`GET /sessions/{id}/history` 保持不变，但在 `docs/api/agent-mobile/` 中明确标注其为**工具调用审计**语义，并指向新接口。该接口命名极具误导性，客户端接错的概率很高。

## 7. 标题生成

`app/history/titler.py`，完整照搬 `app/news/title_translator.py` 的工程约定：

- 只依赖窄 Protocol（`_Completer`），便于测试注入。
- 复用容器中同一个 `container.llm` 实例，不额外配模型或客户端。
- system prompt 要求只输出 JSON；用正则抠最外层 `{}`，容忍代码围栏与客套话。
- `asyncio.CancelledError` 必须重抛，其余异常吞掉降级。
- 结果不可用时回退默认值，绝不写坏数据。

触发时机：`record_turn` 创建会话行时 `asyncio.create_task` 异步触发，**不阻塞回合响应**。统一网关单次耗时 1.5~3 分钟（`app/news/title_translator.py` 模块 docstring 已记录），挂读路径会拖死页面。

写回方式：生成成功则 `UPDATE agent_conversations SET title = :llm_title WHERE session_id = :sid`。生成失败或返回不可用则**什么都不做**——行里已经有写时初始化的 `user_text[:30]`（§4.4），保持原样即是降级，无需额外的回退分支。

因此标题在任何时刻都可读：建行即有截断标题，LLM 完成后自动升级为语义标题，失败则永久停留在截断标题。列表接口对此完全无感。

装配：`provider_mode != "real"` 时不装（照抄 `app/container.py:720-726`）。

## 8. 保留期清理

`app/history/purger.py`，循环照抄 `app/strategy/sampler.py:171-189`：`while True` → `try/except` 只记日志 → `asyncio.sleep(interval)`。

- 多副本去重用 `RedisRoundLock`（`app/services/round_lock.py`），key `history:purge:round`。
- **间隔取 3600s 而非 86400s**。`lock_ttl_for()` 会算出 `interval - 30s`，日间隔得到 86370s 的锁，滚动重启后当天不再执行。小时级间隔按 cutoff 幂等删除，无数据可删即 0 行。
- 删除照抄 `app/news/pg_store.py:311-332`：先用**子查询谓词**删子表再删主表，不用 `IN (ids)`——绕开 PG 65535 绑定参数上限。
- 装配在 `Container.startup()`（`app/container.py:396` 旁），`aclose()` 中对应 cancel + `suppress(CancelledError)`。
- `create_schema()` 挂到 `app/container.py:362-378` 的鸭子类型调用段。

## 9. 配置

`app/config.py` 新增，**全部带默认值**：

| 配置 | 默认值 |
| --- | --- |
| `history_enabled` | `True` |
| `session_retention_days` | `30` |
| `session_purge_interval_seconds` | `3600` |

无默认值的新必填项会导致服务启动失败。本项目 push 到 `main` 即部署 real 环境，新必填项漏配 `set_env` 会让整个服务起不来——这是已发生过的事故模式。

`database_url` 为空时回退内存实现（照抄 `app/container.py:592-608`），保证 fake 模式与本地测试可用。

## 10. 测试策略

TDD，目标覆盖率 80%+。CI 用确切命令验证：`ruff check .`。

| 层 | 用例 |
| --- | --- |
| `store` | 游标编解码往返、非法游标抛 `ValueError`、keyset 分页边界（同时间戳靠 id 稳定排序）、`record_turn` 懒创建与计数累加、**续写不覆盖已生成的 title** |
| `pg_store` | SQLite 下建表、查询、`purge_before` 返回 rowcount；`ensure_utc` naive datetime 回读 |
| `titler` | FakeLLM 正常返回后 title 被 UPDATE、JSON 外裹代码围栏、返回非 JSON 时**保持截断标题不变**、抛异常时同样不变、`CancelledError` 重抛 |
| API | 分页、跨用户 404、不存在 404、非法游标 400、列表查询不含 JOIN（断言 SQL 或行为等价） |
| resume | Redis 存活时复用 Session **但仍换发新 rt_token**、过期后 rehydrate 且轮次被裁剪到 3 轮、scope 重新派生而非恢复、连续两次 resume 不重复灌历史 |
| purger | cutoff 计算、锁未获取时跳过、删除失败只记日志不中断循环 |

断言不依赖第三方对象的 `__eq__`——CI 与本地依赖版本不同，此坑已踩过。

## 11. 风险与已知欠账

| 项 | 说明 |
| --- | --- |
| 语音会话不入库 | 用户语音聊完会发现「没存下来」。需向产品明确一期范围 |
| 无删除能力 | 历史可能含仓位、资产等敏感对话。二期补，届时加软删除字段需手写 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`（本项目无 Alembic，参照 `app/news/pg_store.py:115-122`） |
| `create_all` 不补列 | 后续加列必须显式 `ALTER TABLE`。`modality` 已按 §4.5 提前落列，二期语音无需 DDL；软删除字段仍待偿 |
| 标题生成延迟 | 首轮后 1.5~3 分钟内列表显示的是 `user_text[:30]` 截断标题，之后自动升级。可接受 |
| 存储增长 | 30 天窗口内无上限。若单用户消息量超预期，需再加每用户条数上限 |
| Redis 序列化 | 本设计未新增 `Session` 字段，故 `_dump`/`_load`（`app/runtime/redis_session_store.py:28-208` / `:210-413`）无需改动。若后续新增字段，三处必须同改，否则重启后静默丢失 |

## 12. 实施顺序

**第 0 步先行**：把三个接口写进 `docs/api/agent-mobile/openapi.agent.yaml` 并交付前端。

前端已在用 mock 搭弹窗，契约晚给一天，他们的字段名就白猜一天。契约先行的成本是半小时，返工的成本是一轮联调。这也是本仓库唯一提交进 git 的 API 文档（手写 md 只留本地）。

### PR 1 —— 交付前端联调

| 步 | 内容 |
| --- | --- |
| 0 | `openapi.agent.yaml` 补三个接口，并澄清既有 `/sessions/{id}/history` 为审计语义 |
| 1 | `app/history/models.py` + `store.py`（Protocol、游标、内存实现）+ 单测 |
| 2 | `app/history/pg_store.py` + 建表 + 单测 |
| 3 | `_save_turn` 接入写路径 + 单测 |
| 4 | `GET /conversations` 列表接口 + 单测 |
| 5 | `GET /conversations/{id}/messages` 回放接口 + 单测 |

合并即可用：列表与回放跑通，标题为 `user_text[:30]` 截断值——不是占位符，是最终降级值，PR 2 上线后自动升级为语义标题，**客户端无需改动**。

### PR 2 —— 补齐能力

| 步 | 内容 |
| --- | --- |
| 6 | `app/history/titler.py` + 装配 + 单测 |
| 7 | 抽出凭证派发 helper（`create_session` 与 resume 共用）→ `POST /conversations/{id}/resume` + 单测 |
| 8 | `app/history/purger.py` + 配置 + 装配 + 单测 |

第 7 步含一次针对 `create_session` 的重构，改动面比其余步骤大。第 6、8 步互不依赖。

### 两个 PR 串行合并，不叠栈

PR 2 必须等 PR 1 合入 main 后再从新 main 切分支。本仓库的 squash merge 会让叠栈 PR 悬空——**显示 MERGED 但代码没进 main**，已踩过一次。合并后务必核对 main 上的实际内容，不要只看 PR 状态。
