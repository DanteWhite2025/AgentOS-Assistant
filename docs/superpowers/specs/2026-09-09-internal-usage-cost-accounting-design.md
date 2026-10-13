# 内部用量与成本核算（PRD / 设计）

- 日期：2026-09-09
- 状态：定稿，待 Bruce 评审
- 范围：仅对内成本核算。**不含对外计费、不含对外定价策略。**

---

## 1. 背景

甲方要为语音、聊天、模型分析三类能力做对外定价，向我们索要成本事实：
「电话 1 分钟多少钱、聊天怎么收费、模型分析怎么收费」。

Bruce 的三项诉求：① API 聚合账单数据 ② 语音模型账单数据 ③ 模型分析调用数据。

经与 Bruce 对齐，核算必须具体到**单用户维度**，逻辑对标**水电煤抄表账单**：
给定一个 user_id 和一个账期，答得出「用了多少量、折算多少钱」。
只给 scene/provider 级别的宏观平均成本不满足需求。

### 1.1 现状盘点（已核对代码）

| 链路 | 已有 | 缺口 |
|---|---|---|
| 语音 | `app/voice/usage.py` 有 `VoiceUsageRecord`（session/user/provider/时长/audio_tokens/text_tokens/billed_units）、`compute_billed_units()` 计价单元抽象、`VoiceUsageStore` 协议；`app/api/voice.py:109` 已按 `session_id` 幂等结算 | 只有 `InMemoryVoiceUsageStore`，**进程重启即丢**；无 PG 实现；无单价、无金额；volc / gemini 未分供应商计价 |
| 模型分析（LLM） | `app/agents/llm.py:51` `LLMUsage`（input/output/cache_read/cache_write），流式与非流式两条路均已提取；`llm.py:54` 注释明确「计费侧另有同名类型」——本就是给计费预留 | 仅进 OTel span + 日志（`record_llm_usage`），**未落库**；无单价表；无 user_id 归因 |
| 外部 API | `app/audit/pg_store.py` `audit_log` 已落 PG（trace_id / session_id / tool_name / args_hash / result_ref / status / created_at） | 只能数**调用次数**；**无 user_id**、无耗时、无 token、无金额 |

结论：地基有一半，缺的是「落库 + 定价 + 单用户归因」这三件事。

---

## 2. 目标与非目标

### 2.1 目标

- **G1 单用户账单**：给定 `user_id` + 账期，输出该用户的用量与折算成本（USD），可下钻到明细。
- **G2 三类用量全采集**：LLM token、语音时长/token、外部 API 调用次数，统一进一张流水表。
- **G3 成本可复算**：任一历史账单可用当时的单价与汇率重新算出同一个数。
- **G4 单位经济学**：输出「每分钟通话 / 每次聊天 / 每次分析」的成本均值与 P50/P95，直接回答甲方的三个问题。
- **G5 用量排行**：支持按用量/成本对用户排序，识别高消耗用户。
- **G6 偏差可见**：未采集到用量的调用必须可见且可计数，不得被静默当成零成本。

### 2.2 非目标（一期明确不做）

- 对外计费、扣费、余额、限流（`voice_quota_units_per_user` 现有配额逻辑保持原样，不扩展）
- 对外定价策略、毛利率模型、套餐设计
- 历史数据回填（见 §10 已决议 D3）
- 面向非技术同事的内部看板前端（一期交付 API + SQL，看图接 Grafana 读 PG）

---

## 3. 核心场景

| # | 角色 | 场景 | 验收 |
|---|---|---|---|
| S1 | 运营/BD | 查某用户 2026-09 的账单 | 返回该用户当月三类用量合计与 `amount_usd`，含分类明细 |
| S2 | 运营/BD | 查某用户自定义区间（如 09-01~09-15）用量 | 同上，区间任意 |
| S3 | 产品（甲方问） | 一分钟语音通话成本是多少 | 返回单位成本均值 + P50/P95 + 样本量 |
| S4 | 产品（甲方问） | 一次聊天、一次模型分析成本是多少 | 同 S3，按 scene 切分 |
| S5 | 运营 | 谁最烧钱 | 按 `amount_usd` 降序的用户排行，支持账期与 scene 过滤 |
| S6 | 财务/技术 | 这个账单怎么算出来的 | 下钻到流水明细，每条带单价快照与汇率快照 |
| S7 | 技术 | 这批数可信吗 | 报表附带 `usage_missing` 条数与占比 |

---

## 4. 计费口径定义

这是与 Bruce 讨论的核心，口径不定则数字无意义。

### 4.1 三类 kind 的量纲

| kind | 计量单位 | 单价维度 | 数据来源 |
|---|---|---|---|
| `llm` | input / output / cache_read / cache_write tokens（四者分开） | USD / 1M tokens，**按 token 类型分别定价** | `LLMResult.usage` |
| `voice` | `duration_seconds` + `audio_tokens` + `text_tokens` | USD / 分钟、USD / 1M tokens | 客户端上报 + 服务端兜底（§7.1） |
| `external_api` | `call_count`（次） | USD / 次 | 调用点显式记账 |

**关键口径决定**：一期流水表存**原始量纲**（tokens、seconds、count），不再存
`billed_units` 这类聚合单元。现有 `compute_billed_units()`（时长单元 + token 单元
相加）是语音配额的口径，与成本核算无关——把两种量纲相加会丢失定价所需的结构。
该函数保留服务于现有配额逻辑，成本核算不复用。

### 4.2 单价与币种

- 基准币种 **USD**（已决议 D4）。
- CNY 供应商（火山等）在**流水写入时**按当日汇率折算为 `amount_usd`，同时保存
  `amount_original` + `currency_original` + `fx_rate_snapshot`，以便复算与审计。
- 汇率来源：每日汇率（T-1），配置固定汇率作为 fallback；`fx_rate` 表记录
  `rate_date` / `source`，流水引用其快照值。
- Core 定性为内部服务，`unit_price = 0`，但**流水照常采集**，单价可后续配置（已决议 D2）。

### 4.3 精度

金额与单价一律 `Decimal` / PG `NUMERIC(18,8)`。
理由：token 单价量级为 `$0.000003/token`，float 与低标度 NUMERIC 都会静默截断
（此前 strategy 侧已因 Decimal 默认精度静默截断踩过一次）。禁止用 float 存金额。

---

## 5. 数据模型

### 5.1 `usage_ledger`（用量流水，只追加，不更新）

```
id                  BIGSERIAL PK
occurred_at         TIMESTAMPTZ  NOT NULL     -- 用量发生时间（非写入时间）
recorded_at         TIMESTAMPTZ  NOT NULL     -- 落库时间（用于识别迟到数据）

user_id             VARCHAR(64)  NOT NULL     -- 核心归因维度
session_id          VARCHAR(64)
turn_id             VARCHAR(64)               -- 一轮对话唯一 id，把该轮多次 LLM+工具调用归为一次「聊天」（需新增，见 §6.2.0）
agent_id            VARCHAR(64)

kind                VARCHAR(16)  NOT NULL     -- llm | voice | external_api
provider            VARCHAR(32)  NOT NULL     -- anthropic | volc | gemini | tavily | core | followin
model               VARCHAR(64)               -- claude-opus-5 / volc-tts / gemini-live ...
scene               VARCHAR(32)  NOT NULL     -- chat | voice | analysis | news | strategy | title

input_tokens        INTEGER
output_tokens       INTEGER
cache_read_tokens   INTEGER
cache_write_tokens  INTEGER
duration_seconds    NUMERIC(12,3)
call_count          INTEGER

price_version       VARCHAR(32)               -- price_book 版本，复算依据
unit_price_snapshot JSONB                     -- 本条实际适用的单价明细
amount_original     NUMERIC(18,8)
currency_original   VARCHAR(8)
fx_rate_snapshot    NUMERIC(18,8)
amount_usd          NUMERIC(18,8)             -- 报表核算列

status              VARCHAR(16)  NOT NULL     -- ok | usage_missing | duplicate | unpriced
dedup_key           VARCHAR(128) UNIQUE       -- 幂等
```

**索引**
- `(user_id, occurred_at DESC)` — S1/S2 单用户账期查询主索引
- `(occurred_at, kind)` — S3/S4 单位经济学聚合
- `(session_id)` — S6 下钻
- `dedup_key` UNIQUE — 幂等落库

**`dedup_key` 构造**（教训：followin 曾因 `dedup_key` 口径变更导致存量迁移抖动，
故此处一次定死并写入文档）：
- `llm`：`llm:{turn_id}:{step_index}`
- `voice`：`voice:{session_id}`（沿用现有按 session 幂等口径）
- `external_api`：`api:{trace_id}:{tool_name}:{seq}`

**不可变性**：流水只追加。更正通过写一条反向冲抵记录实现，不 UPDATE 历史行。

### 5.2 `price_book`（单价表，带生效期）

```
id, price_version, provider, model, kind
metric              -- input_tokens | output_tokens | cache_read | cache_write | duration_minute | call
unit_price          NUMERIC(18,8)
currency            VARCHAR(8)
effective_from      TIMESTAMPTZ NOT NULL
effective_to        TIMESTAMPTZ            -- NULL = 当前生效
```

匹配不到单价时，流水以 `status=unpriced` 落库（金额留空，**不填 0**），报表单列告警。
这与 `llm.py` 既有红线一致：拿不到真实值就空着，绝不编数字。

### 5.3 `fx_rate`

```
rate_date DATE PK, currency VARCHAR(8), rate_to_usd NUMERIC(18,8), source VARCHAR(32)
```

---

## 6. 采集点与技术可行性

### 6.1 写入方式

**显式用量事件 + 后台批量落 PG**，不从 OTel span 抽取。

理由：`llm.py:218` 的既有注释记录了 span 在首 token 前就关闭、导致
`record_llm_usage` 静默 no-op 的坑。把计费挂在一条已证明会静默丢数的路径上不可接受；
且 span 上没有 `user_id` / `scene`，无法满足 G1。OTel 继续用于排障，不作为计费源。

### 6.2 各链路改造点（已核对代码，含工作量判断）

#### 6.2.0 前置改造：turn_id（一期阻塞项，已决议 D9）

**文本对话链路当前没有 turn_id。** `ConversationTurn`（`app/runtime/session.py:50`）
只有 `user_text` / `assistant_text` / `created_at`，无标识符；代码中现存的 `turn_id`
仅在 `app/voice/pipeline_provider.py` 内，是 `int` 类型的 provider 内部轮次计数器
（用于打断判定），**语义不同，不可复用**。

而 S4「一次聊天多少钱」要求把一轮对话内的多次 LLM 调用（`max_steps` 多步循环）
与多次工具调用归并计价——没有贯穿全轮的 id 就归并不了，只能算出「每次 LLM 调用成本」，
那不是甲方问的东西。

因此一期须先新增：每轮对话入口生成一个 `turn_id`（UUID），贯穿该轮的 LLM 记账、
工具记账与 audit。语音侧沿用 `session_id` 作为通话粒度，其 provider 内部 `turn_id`
不进 `usage_ledger`，避免两套语义混淆。

**LLM（`scene=chat|analysis|title`）**
- `LLMProvider.complete/stream` 签名**不动**（与既有「不动 run_turn 签名」口径一致）。
- 在 runner 记账，两处：`app/agents/runner.py:461`（非流式）、
  `runner.py:1051`~`1060` 的 `LLMStreamDone` 分支（流式）。runner 持有 `Session`，
  `Session.user_id`（`app/runtime/session.py:65`）可得 → **user_id 归因无阻塞**。
- 陷阱：流式 usage 只在最后一个 usage-only chunk 里，提前 `break` 会丢
  （async generator 提前退出不关闭内层的问题此前已重复出现四次，均由测试捕获）。
  记账必须落在 `LLMStreamDone`；`result is None` 分支须落 `usage_missing`。
- 多步循环（`for step in range(self._max_steps)`）每步一条流水，`step_index` 入 `dedup_key`。

**语音（`scene=voice`）**
- 新增 `PgVoiceUsageStore` 实现现有 `VoiceUsageStore` 协议（协议已在
  `app/voice/usage.py:64`，`container.py:298` 已注入 → 替换实现即可，调用方不动）。
- `VoiceUsageRecord` 已含 `user_id` → **无阻塞**。
- 结算时同步写一条 `usage_ledger`（`kind=voice`）。
- 服务端兜底见 §7.1。

**外部 API（`scene` 随调用方）**
- **一期主要工作量在此**：`record_tool_call` 有 **11 个调用点**
  （`runner.py` 4 处：831 / 870 / 923 / 957；`api/agent.py` 4 处、
  `api/voice.py` / `api/strategy.py` / `api/tool_execution.py` 各 1 处），
  而 `AuditRecord`（`app/audit/store.py`）**没有 user_id 字段**。
- 方案：收敛到单一记账入口 `UsageRecorder`，由它同时写 audit 与 usage_ledger，
  避免 11 处各自拼装上下文而漏字段。
- `ToolExecutor.execute(user_id=...)`（`app/tools/executor.py:117`）已带 user_id
  → 工具侧 user_id 可得；API 层调用点需从请求上下文补齐。
- 计成本的外部供应商：Tavily（按次）、Followin、火山 OpenAPI；Core 单价 0（D2）。

### 6.3 失败隔离

记账失败**不得影响主链路**：写库异常只记日志 + 计数器，不向用户报错、不中断对话。
反向要求是漏记必须可观测——暴露 `usage_record_failed_total` 指标，否则漏数会静默。

---

## 7. 已知偏差与兜底

这一节是本 PRD 最需要 Bruce 确认的部分：**不处理这些偏差，单用户账单会失真。**

### 7.1 语音时长漏报（已决议 D1：做服务端兜底）

数据面直连，Runtime 看不到音频流，时长依赖客户端挂断时上报
（`app/voice/usage.py` 模块注释）。客户端崩溃、被系统杀进程、断网未重试
= **这次通话零成本**。做单用户抄表账单时，这类漏报直接落在具体用户头上，
不是宏观平均能摊掉的误差。

兜底方案：以火山 room 事件回调为服务端时长来源。
`app/voice/volc_callbacks.py` 已有回调基础设施（含 `provider_trace_id`）与
`callback_auth.py` / `callback_metrics.py`，改造成本可控。

- 口径：**服务端时长优先**，客户端上报作为交叉校验。
- 两者差值超阈值时落 `status` 标记并告警（客户端少报 = 疑似漏报，多报 = 疑似异常）。
- 仍沿用 `session_id` 幂等，两条来源不重复计费。
- Gemini Live 通道无等价回调，兜底一期仅覆盖火山；gemini 通道的漏报率需在报表中标注。

### 7.2 usage_missing 必须落库

`llm.py` 的红线是「拿不到真实值就空着，绝不编数字」——这对排障正确，但用于
成本核算时，「空着」等于这次调用免费，成本会被**系统性低估**。

要求：拿不到 usage 的调用**也要落一条流水**，`status=usage_missing`，量纲字段留空。
所有报表必须附带 `usage_missing` 条数与占比。宁可给出「成本 X，另有 3% 调用未采集」，
也不给一个看起来干净但偏低的假数——后者会直接误导甲方的定价。

### 7.3 迟到数据与账期（已决议 D8）

服务端兜底回调可能在账期结算后才到。口径：按 `occurred_at` 写回**原账期**，
账单带 `revision` 版本号，重算后版本递增；`recorded_at` 与 `occurred_at` 的差值
用于识别迟到数据。**不顺延到下期**——顺延会让单用户账单与实际用量错月，
违背抄表账单的月度真实性。

因此账单查询接口须返回 `revision`，调用方不得缓存无版本号的账单结果。

---

## 8. 交付接口

内部端点，走现有内部鉴权，不对外暴露。

```
GET /internal/usage/users/{user_id}/bill?from=&to=
  → 单用户账期账单（S1/S2）
    { user_id, period, total_amount_usd,
      by_kind:  [{kind, quantity_summary, amount_usd}],
      by_scene: [{scene, amount_usd}],
      data_quality: {usage_missing_count, usage_missing_ratio, unpriced_count} }

GET /internal/usage/users/{user_id}/ledger?from=&to=&kind=&cursor=
  → 明细下钻，游标分页（S6）

GET /internal/usage/summary?group_by=scene,provider&from=&to=
  → 宏观聚合

GET /internal/usage/unit-economics?from=&to=
  → 单位经济学（S3/S4）
    每分钟通话 / 每次聊天（按 turn_id 归并）/ 每次分析的
    mean、p50、p95、样本量、usage_missing_ratio

GET /internal/usage/top-users?from=&to=&order_by=amount_usd&limit=
  → 用量排行（S5）
```

一期用明细表实时聚合（当前量级足够）。预留按月物化聚合表，待明细行数超阈值再上，
不在一期实现。

---

## 9. 分期与验收

**一期（本 PRD 范围）**
0. **前置**：文本链路新增贯穿一轮的 `turn_id`（§6.2.0），否则 S4 无法归并
1. `usage_ledger` / `price_book` / `fx_rate` 三张表 + `UsageRecorder` 统一记账入口
2. LLM 记账（runner 两处）+ `PgVoiceUsageStore` + 外部 API 记账点收敛
3. 火山 room 事件回调服务端时长兜底
4. 5 个查询端点
5. 单价表初始化（Anthropic / 火山 / Gemini / Tavily 官方价）

**验收**
- 单用户任意账期账单可查，金额可用单价+汇率快照复算出同一个数
- 三类用量均有流水，`usage_missing` 与 `unpriced` 在报表中可见
- 语音服务端兜底生效：客户端不上报的通话仍产生流水
- 重复上报不产生重复计费（幂等）
- 记账失败不影响主链路
- 测试覆盖 ≥ 80%，含流式 usage 丢失、迟到数据、幂等、汇率折算精度四类用例

**二期（不在本 PRD）**
对外计费与扣费、内部看板前端、月度物化聚合、Gemini 通道兜底、真实账单对账校准

---

## 10. 已决议项（2026-09-09 Wesley × Bruce）

| # | 议题 | 结论 |
|---|---|---|
| D0 | 核算粒度 | **单用户维度**，对标水电煤抄表账单；user_id 为聚合与报表的核心维度 |
| D1 | 语音服务端兜底 | **做**。客户端漏报会使单用户账单严重失真，规划火山 room 事件回调兜底 |
| D2 | external_api 中的 Core | 定性为内部服务，流水照常采集，单价暂记 0，保留后续配置单价能力 |
| D3 | 历史数据 | 从上线开始算，不回填；历史日志补录不作为一期阻塞项 |
| D4 | 币种 | 统一 USD 基准；CNY 供应商在写入时按汇率折算存 `amount_usd`，保留原币与汇率快照 |
| D5 | 成本口径 | 自建单价表估算（非第三方账单对账）；月度对账校准列入二期 |
| D6 | 范围 | 仅对内成本统计，不含对外定价 |
| D7 | 交付形态 | PG 明细表 + 查询 API + SQL 报表；看图接 Grafana 读 PG |
| D8 | 迟到数据账期 | 按 `occurred_at` 写回原账期，账单带 `revision` 版本号重算，**不顺延下期**（§7.3） |
| D9 | turn_id | **作为一期前置项做**。对话入口生成 UUID 贯穿整轮，归并该轮全部 LLM 与工具调用，保证 S4「每次聊天成本」可准确核算（§6.2.0） |
| D10 | 量纲 | 流水表**不存 `billed_units`**，成本核算保持原始量纲（tokens / seconds / count）；`compute_billed_units()` 仅继续服务现有语音配额逻辑（§4.1） |

---

## 11. 风险

| 风险 | 影响 | 应对 |
|---|---|---|
| 供应商调价未同步到 `price_book` | 成本失真且无感 | 单价带生效期；匹配不到落 `unpriced` 并告警 |
| Gemini 通道无服务端兜底 | 该通道单用户账单偏低 | 报表标注该通道漏报率；二期补 |
| 11 个 audit 调用点漏补 user_id | 部分外部 API 用量无法归因到用户 | 收敛到单一 `UsageRecorder` 入口；对 `user_id` 缺失落 `usage_missing` 并计数 |
| 流式 usage 丢最后一块 | LLM 成本系统性偏低 | 记账落在 `LLMStreamDone`；专项测试用例 |
| 估算与真实账单存在系统性偏差 | 甲方定价基线偏移 | 报表明示「估算值」；二期做月度对账校准系数 |
| `turn_id` 为新增字段，需改动对话主链路入口 | S4「每次聊天成本」交付不了 | 列为一期前置阻塞项（§6.2.0）；与语音侧同名 int 字段严格隔离 |
