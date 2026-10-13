# Followin 新闻聚合接入设计

- 日期：2026-08-21
- 状态：待评审
- 范围：第一期 = 采集入库 + 标签体系 + 查询接口。Grok 整套彻底下线。

## 1. 目标与非目标

**目标**

1. 接入 Followin 官方 MCP 端点，稳定拉取新闻入库（PostgreSQL）。
2. 给每条新闻打标签：`recommend` / `crypto` / `stocks` / `ai_tech` / `macro`。
3. 提供一套全新的干净接口 `GET /v1/agent/news`，支持 tag 过滤、关键词搜索、游标分页。

**非目标（明确不做）**

- 不保留、不适配任何 `/grok/*` 路径与字段。`/grok/trends`、`/grok/analyze`、`/grok/push` 全部下线，不留适配层、不留字段映射。
- 不做 webhook 推送（原 `/grok/push` 的能力）。第一期需求定义里没有，二期需要时采集器已天然知道增量，届时再加。
- 不做新闻详情端点（列表直接返回全文，见 §5）。
- 不做 Agent 按需检索工具注册（twitter / telegram / KOL 观点）——这是二期，走 MCP 工具形式，不需要入库。
- 不做 LLM 打标、不做 pgvector 语义检索、不做富文本 HTML 渲染。

## 2. Spike 实测结论（2026-08-21，本设计的事实基础）

端点 `https://mcp.followin.io/v2/mcp`，`x-api-key` header 鉴权，streamable HTTP MCP，`followx-mcp v2.0.0`。
握手返回 `mcp-session-id`，后续 `tools/call` 需携带。**单次调用实测约 2 秒**。

工具面：`news` / `metrics` / `signal` / `subscription` / `twitter`。第一期只用 `news`。

**四个源的实测形态**

| source | 密度 | title | source_url | bucket |
|---|---|---|---|---|
| `media` | 2.00 条/分 | 50/50 | 有 | `articles` |
| `research` | 0.03 条/分 | 8/8 | 有 | `articles` |
| `twitter` | 1.58 条/分 | **0/50** | 有 | `social` |
| `telegram` | 0.03 条/分 | **0/25** | **0/25** | `social` |

**五个硬约束**

1. **无分页。** `inputSchema` 是 `additionalProperties:false` 且无 cursor 字段，meta 里也没有 next_cursor。`limit` 上限 50。实测 `limit=50 + time_range=1d` 只回溯 **25 分钟**。→ 采集间隔必须远小于 25 分钟；**历史无法回填**。
2. **无 id。** 但 `source_url` 稳定且唯一（连调两次 20/20 交集），指向原站。→ 用作主身份。
3. **字段 route-dependent。** 带 `query`/`sources` 的 search route 有 `source_url`/`source_lang`/`source_name`；空 query 的 trending route 没有，但独有 `asset_types:["BTC"]`。→ 归一化层必须容忍缺列，`raw` 全留。
4. **`category` 不可直接当 tag。** 实测取值 `market/social/majorcoin/research/fundamentals/event/''/null`，混乱且有空值。
5. **`content` 含 HTML。** 实测有 `<p><b>看涨观点</b>…`。透传即 XSS 面。

`meta.quota` 一路 `consumed:0, remaining:100`，news 检索似乎不计费，但 100 的周期未标注（见 §10 风险）。

## 3. 范围内的第一期采集源：只 `media` + `research`

`twitter`/`telegram` 100% 无 title，新闻卡片渲染不出来；`telegram` 连 url 都没有。而这两个源恰好是二期「Agent 按需检索 KOL / 推特」的主场，**那个场景不需要入库**。第一期不采，二期也不返工。

`recommend` tag 由空 query 的 trending route 提供（`provenance=followin_trending`，Followin 自己的热榜），不需要我们造排序算法。

## 4. 架构与文件划分

```
app/news/
  models.py           NewsItem / NewsQuery / NewsPage（pydantic）
  followin_client.py  MCP 薄封装 + FakeFollowinClient
  normalizer.py       Followin 原始行 → NewsItem（容忍 route-dependent 缺列）
  tagger.py           关键词规则表 → tags[]
  store.py            NewsStore Protocol + InMemoryNewsStore
  pg_store.py         建表 + upsert + 查询（tag / q / cursor）
  collector.py        后台采集循环
app/api/news.py       GET /v1/agent/news
```

每个文件单一职责，目标 200~400 行。复用现有基建：`app/services/round_lock.py`（多 worker 单实例采集）、`app/services/rate_limit.py`（`/news` 限流）。

## 5. 数据模型

**tags 走关联表，不走 JSONB 数组。** 现有 PG store 测试全部基于 `sqlite+aiosqlite`（见 `tests/test_stores.py`）。若用 `tags JSONB` + `@> '["crypto"]'`，核心查询是 PG 专有语法，在 sqlite 上跑不了，tag 过滤逻辑就脱离了现有测试范式。拆成关联表后过滤是普通 `JOIN + WHERE tag = ?`，两边都能跑，还吃 B-tree 索引。

```
news(
  news_id       TEXT PRIMARY KEY   -- sha256(source_url)；无 url 时 sha256(source_name + content[:256])
  dedup_key     TEXT NOT NULL      -- normalize(title)：跨 route 合并用，见 §7
  source        TEXT NOT NULL      -- media | research | trending
  source_name   TEXT               -- cointelegraph / benzinga；trending route 为空
  source_url    TEXT               -- 可空
  source_quality TEXT
  title         TEXT NOT NULL
  content       TEXT NOT NULL      -- 剥 HTML 标签后的纯文本
  lang          TEXT               -- 归一化：上游 zh 与 zh-cn 混用，统一成 zh-cn
  asset_type    TEXT               -- crypto | tradfi
  upstream_category TEXT           -- Followin 原始 category，存档但不参与过滤
  published_at  TIMESTAMPTZ NOT NULL
  raw           JSON NOT NULL      -- 原始行全留（含原始 HTML），为二期升级预留
  created_at    TIMESTAMPTZ NOT NULL
)
news_tags(news_id, tag, PRIMARY KEY(news_id, tag))
```

索引：
- `news(published_at DESC, news_id DESC)` — 游标分页
- `news(dedup_key, published_at DESC)` — 跨 route 合并查询
- `news_tags(tag, news_id)` — tag 过滤
- PG 额外：`gin_trgm_ops` on `title`、`content` — 关键词检索

`content` 存**剥标签后的纯文本**。上游 HTML 不可信，透传给客户端渲染是 XSS 面；原始 HTML 保留在 `raw` 里，二期要富文本再从 `raw` 补。安全的那一侧作默认。

## 6. Followin MCP 客户端

**用 httpx 薄封装，不用 `mcp` SDK。** 理由不是风格：`mcp` 目前只是 pydantic-ai 的传递依赖，未在 `pyproject.toml` 声明，在传递依赖上建生产采集路径，上游哪天不带它就断。而我们只用 `tools/call name=news` 一个方法，SDK 的 tools/list 缓存、prompts、resources、sampling 全不用；它的 `ClientSession` 是 async context manager，塞进常驻循环里 lifecycle 要么长撑要么每轮重建，都和仓库现有「httpx 客户端由容器创建、`aclose()` 统一关闭」的范式冲突。

薄封装约 100 行：`initialize` → `notifications/initialized` → `tools/call`，加 SSE 文本解析（响应格式 `event: message\ndata: {json}`）。**不做流式读取**——请求-响应式、2 秒、7KB，一次性取完整 body 再解析更简单。

实测坑：`Accept` 头必须同时带 `application/json, text/event-stream`，少一个即失败。

**Session 生命周期：lazy + 自愈。** TTL 官方未说明，也测不出来，因此不能假设长期有效。`_ensure_session()` 无 session 则握手；`tools/call` 收到 4xx（session 失效典型为 404/400）则清 session、重握手、**只重试一次**（避免上游故障时打死它）；二次失败抛 `FollowinUpstreamError`，由采集器记日志跳过本轮该源、不中断循环。

## 7. 采集器

**间隔由实测密度推导**：media 2.00 条/分 ÷ limit 上限 50 = 25 分钟耗尽容量。取 **300 秒**（5x 余量，足以扛美联储决议那种密度翻倍的新闻潮）。

每轮 3 次 `tools/call`（各约 2 秒，单轮约 6 秒）：

| # | 参数 | 用途 |
|---|---|---|
| 1 | `news(sources=["media"], limit=50)` | 主力 |
| 2 | `news(sources=["research"], limit=50)` | 稀疏但便宜 |
| 3 | `news(limit=50)` | 空 query trending，供 `recommend` tag |

**跨 route 重复入库的处理。** trending route 无 `source_url`，`news_id` 只能用 content hash；同一条新闻若同时出现在 media route（有 url），会产出两个不同 `news_id` → 重复。对策：`dedup_key = normalize(title)`（去首尾空白、折叠内部空白、casefold），upsert 前按 `dedup_key` 查最近 24h，命中则把 `recommend` tag 合并到已有行、**不新建行**。这沿用仓库既有 `grok_monitor.dedup_key` 的思路，且现在有稳定 url 当主身份，比 Grok 时代可靠。

**容错**：单源失败只跳过它自己（沿用 `grok_monitor.refresh_once` 的模式）；`asyncio.CancelledError` 必须 re-raise；`RoundLock` 保证多 worker 只有一个真去拉（Followin 虽不计费，但重复采集是纯浪费加 upsert 冲突噪音）。

**冷启动。** 上游无历史查询能力（`time_range` 只是过滤窗口，不能翻页），**回填不可能**。首轮只能拿到最新 50 条 media（约最近 25 分钟）+ 8 条 research + 50 条 trending，**首日约 100 条，铺满首屏但下拉两页到底**；一天后约 2900 条恢复正常。

唯一加速手段：首次启动额外发几次带 `query` 的搜索——search route 有 relevance 排序，能捞到比时间窗更早的内容。每个 tag 配一组预设词（`macro`: `"Fed CPI interest rate"`；`ai_tech`: `"AI chips NVDA OpenAI"` 等），各 `limit=50`，预计把首日从约 100 条抬到约 500 条。做成一次性开关 `AGENT_NEWS_WARMUP_ON_START`（默认 false，仅首次部署开一次），非常驻逻辑。

**这条须同步给产品：首日纵深有限是上游能力所限，不是缺陷。**

## 8. 标签体系

| tag | 判定依据 |
|---|---|
| `recommend` | `provenance == "followin_trending"`（Followin 原生热榜） |
| `crypto` | `asset_type == "crypto"` |
| `stocks` | `asset_type == "tradfi"` |
| `ai_tech` | 关键词规则表 |
| `macro` | 关键词规则表 |

`news` 工具不返回任何行业/主题维度（`macro` 只存在于 `metrics` 工具），所以后两个 tag 必须自建规则。一条新闻可命中多个 tag。

**规则表匹配须区分中英文词边界。** ASCII 短词做子串匹配会大量误命中——`"AI"` 会命中 `said`、`maintain`、`Thailand`。因此规则统一编译成正则：**纯 ASCII 词自动加 `\b` 边界，含 CJK 字符的不加**（中文无词边界概念）。这个判定可自动化，收在 `tagger.py` 里。

初始规则表（可迭代，改动后可全表重跑打标，因为 `raw` 全留）：

- `ai_tech`：`NVDA` `NVIDIA` `英伟达` `AMD` `TSMC` `台积电` `OpenAI` `ChatGPT` `Anthropic` `Claude` `Gemini` `LLM` `GPU` `算力` `芯片` `半导体` `AI` `人工智能` `大模型` `数据中心`
- `macro`：`CPI` `PPI` `PCE` `GDP` `非农` `NFP` `FOMC` `美联储` `Fed` `降息` `加息` `利率` `关税` `tariff` `通胀` `inflation` `失业率` `国债` `Treasury` `收益率` `yield` `央行` `财政部` `Powell` `鲍威尔`

匹配范围：`title + content`，大小写无关。

## 9. 对外接口

```
GET /v1/agent/news
  ?tag=recommend|crypto|stocks|ai_tech|macro   可选单值，不传 = 全部；清单外的值返 400
  &q=<关键词>                                   可选
  &lang=zh-cn|en                                可选
  &days=30                                      可选整型，0 或不传 = 服务端默认 30；上限 365
  &limit=20                                     1~50，默认 20
  &cursor=<opaque>                              可选
```

响应：

```json
{
  "items": [{
    "news_id": "...", "title": "...", "content": "...",
    "source_name": "cointelegraph", "source_url": "https://...",
    "published_at": "2026-08-21T00:25:00Z",
    "tags": ["crypto", "recommend"],
    "asset_type": "crypto", "lang": "zh-cn"
  }],
  "next_cursor": "..." 
}
```

`tag` **只接受单值**（对应截图上 chip 的单选交互），不支持多 tag 交集/并集——需要时二期再加，第一期不预留。注意这与「一条新闻可有多个 tag」不矛盾：存储侧多标签，查询侧单选。

`next_cursor` 为 `null` 表示到底。游标是 `(published_at, news_id)` 的不透明编码，避免 offset 分页在持续写入下的漂移/重复。

**不做详情端点。** `verbosity=standard` 下 `content` 仅约 600 字（且是 Followin 自产的中文结构化摘要「发生了什么 / 市场情绪 / 交易指南」，对中文用户比原站英文原文更有价值），20 条 × 600 字约 12KB，列表直接全量返回。要看原文有 `source_url` 跳原站。省一个端点和一次往返。

**`time_range` 默认 30d 是性能护栏，不是产品选择**：见 §10 的中文检索限制。

**限流**：`/news` 打的是我们自己的 PG，`q=` 的顺序扫描是真实成本，保留 `rate_limit` 但按正常浏览量级定档（`60 次 / 60 秒`）。原 `_guard_expensive` 那套（5 次/小时，为 token 计费 + 60s 超时设计）随 grok 一并删除。

## 10. 已知限制

1. **无历史回填**，首日纵深有限（§7）。
2. **2 字中文关键词无法用 `pg_trgm` 索引。** trigram 需 3 字符，`ILIKE '%降息%'` 提取不出完整 trigram → 退化为顺序扫描。而中文 2 字词极常见（降息/关税/减产）。按实测密度，一个月约 8.6 万行，顺序扫 + ILIKE 在 PG 上预计几十至几百毫秒，第一期可接受；`time_range` 默认 30d 把扫描范围钉死。**此数字须在实现阶段用真实数据量实测一次，不得凭估算收尾。** 彻底解决需 `pg_bigm`（2-gram，专为 CJK，但多数托管 PG 不支持）或 pgvector 语义检索——二期。
3. ~~**`meta.quota.limit=100` 的周期未标注**（日/月/其它）~~ —— **已闭环（2026-08-21）**：Followin 官方后台明确标注「新闻和订阅不限量、不消耗配额」，响应里的 `limit: 100` 与后台的 30 次限制只作用于 `metrics` / `signal` 与 `twitter` 工具。第一期只用 `news`，**不受配额约束**，每天约 864 次调用无风险。注意此结论**不覆盖二期**：接入 `metrics` / `signal` / `twitter` 时那些工具是计配额的，需重新核算。
4. **MCP session TTL 未知**，靠 §6 的自愈兜。
5. **`dedup_key` 靠标题规范化**，不同源转载时措辞差异会漏合并（产生重复条目）；反之标题相同的不同事件会被误合并。有 `source_url` 的行以 url 为主身份，此风险仅限 trending route。
6. `twitter`/`telegram` 不入库（无 title），KOL 观点在第一期查不到。

## 11. 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `AGENT_FOLLOWIN_API_KEY` | `""` | 空则降级 Fake 模式；`provider_mode=real` 时必填 |
| `AGENT_FOLLOWIN_MCP_URL` | `https://mcp.followin.io/v2/mcp` | |
| `AGENT_FOLLOWIN_TIMEOUT_SECONDS` | `30` | 实测 2s，30s 已是宽松上限 |
| `AGENT_NEWS_COLLECT_ENABLED` | `false` | 沿用 grok_monitor 的保守风格，部署时显式开 |
| `AGENT_NEWS_COLLECT_INTERVAL_SECONDS` | `300` | 范围 60~3600 |
| `AGENT_NEWS_COLLECT_LIMIT` | `50` | 上游上限，范围 1~50 |
| `AGENT_NEWS_WARMUP_ON_START` | `false` | 一次性冷启动预热 |
| `AGENT_NEWS_DEFAULT_TIME_RANGE_DAYS` | `30` | `/news` 未传 `time_range` 时的护栏 |
| `AGENT_NEWS_RETENTION_DAYS` | `90` | 约 2900 条/天，一年约 100 万行；超期清理随采集轮次执行 |
| `AGENT_NEWS_QUERY_RATE_LIMIT` | `60` | 配 `AGENT_NEWS_QUERY_RATE_WINDOW_SECONDS=60` |

## 12. Grok 下线清单

**删除**

- `app/api/grok.py`、`app/services/grok.py`、`app/services/grok_monitor.py`、`app/services/grok_cache.py`
- `scripts/grok_refresh.py`
- `tests/test_grok_cache.py`、`tests/test_grok_monitor.py`、`tests/api/test_grok.py`（共约 1633 行测试）
- `app/config.py`：全部 `grok_*` / `xai_*` 配置项，含缓存 TTL 与监控间隔的交叉校验（约 `config.py:245`）
- `app/container.py`：`grok_service` / `grok_monitor` / `grok_monitor_task` 及其装配与关闭逻辑
- `app/main.py`：`grok_router` 注册

**必须同步修改（漏了会出问题）**

- `app/config.py` 约 271 行：`provider_mode=real` 的必填凭据清单里有 `AGENT_XAI_API_KEY`，须换成 `AGENT_FOLLOWIN_API_KEY`。不改则 real 模式会因一个已废弃的 key 启动失败，或漏掉新 key 的校验。
- `tests/test_realtime_security.py` 约 58 行：参数化项 `{"xai_api_key": ""}` 须换成 `{"followin_api_key": ""}`。

**保留：`app/services/webhook.py` + `tests/test_webhook_url.py`**

删掉 grok 后它**没有生产调用方**（原调用方只有 grok 三处）。仍建议保留：`check_webhook_url` 是 SSRF 防护，有独立测试覆盖，逻辑重写易出错，二期做推送即需要。它是「暂无调用方的受测工具库」，不是死代码——但这是一个判断，评审时可推翻改为一并删除。

`round_lock.py`（采集器要用）、`rate_limit.py`（`/news` 要用）均保留。

## 13. 测试策略（目标覆盖率 80%+）

fixtures 取本次 spike 的**真实响应**，存 `tests/fixtures/followin_*.json`。这是 spike 唯一刻意保留的产物：手编 mock 编不出 route-dependent 缺列那种真实形态，而那恰好是 `normalizer` 最易出错的地方。

| 模块 | 重点用例 |
|---|---|
| `normalizer` | trending route 缺 `source_url`/`source_lang`；`social` bucket 空 title；HTML 剥离；`zh` → `zh-cn` 归一化 |
| `tagger` | 词边界：`"AI"` 不得命中 `said`/`Thailand`；中文子串正常命中；多 tag 并发命中 |
| `followin_client` | session 自愈（respx：首次 404 → 重握手 → 二次成功）；二次失败抛 `FollowinUpstreamError`；SSE 解析 |
| `pg_store` | tag 过滤、`q` 检索、游标分页无重无漏、`dedup_key` 合并 tag 而非新建行（`sqlite+aiosqlite`） |
| `collector` | 单源失败跳过其余继续；`RoundLock` 未获锁时不拉；`CancelledError` 正常传播 |
| `api/news` | 参数组合、非法 tag 返 400、`limit` 边界、游标往返一致 |
| `telemetry/logging` | 新增的 `x-api-key` 脱敏规则确实命中 32 字节无前缀 hex（§15） |

## 14. 落地步骤

按依赖顺序，每步可独立提交、独立测试（TDD：先写测试）。

1. **Grok 下线**（§12）。含 `config.py` 必填清单与 `test_realtime_security.py` 的同步修改。此步跑通全量测试，确认无残留引用。
2. **`models.py` + `tagger.py`**。纯函数，无 I/O，测试最快。词边界是重点。
3. **`followin_client.py` + fixtures**。含 `FakeFollowinClient`。session 自愈路径必须有测试。
4. **`normalizer.py`**。喂 §13 的真实 fixtures，覆盖两个 route 的字段差异。
5. **`store.py` + `pg_store.py`**。建表、upsert、`dedup_key` 合并、三种查询。走 `sqlite+aiosqlite`。
6. **`collector.py`**。接 `RoundLock`，容错与冷启动预热。
7. **`api/news.py` + 容器装配 + 路由注册**。限流接 `rate_limit`。
8. **部署前置**：PG 执行 `CREATE EXTENSION IF NOT EXISTS pg_trgm`；`AGENT_FOLLOWIN_API_KEY` 进 secret manager；写入 `docs/deploy`。更新 `docs/api/openapi.json`。
9. **上线前必办**：~~① 向 Followin 确认 `quota.limit=100` 的周期~~（**已闭环，见 §10.3**：news 不限量、不计配额）；② 用真实数据量实测 2 字中文关键词查询延迟（§10.2）——**此项仍未完成，不得凭估算收尾**。
10. **首次部署**：开一次 `AGENT_NEWS_WARMUP_ON_START`，随后关闭。

## 15. 安全

- **API Key 已在协作对话中以明文出现，上线前须在 Followin 后台轮换一次。** 新 key 只进环境变量 / secret manager，不入 git。
- 上游 `content` 含 HTML，入库前剥标签（§5），原始留 `raw`，不向客户端透传可渲染 HTML。
- `q=` 参数经 SQLAlchemy Core 参数化绑定，不做字符串拼接。
- `/news` 保留限流（§9）。
- **日志脱敏须新增规则（现有规则覆盖不到）。** `app/telemetry/logging.py` 的 `_SECRET_PATTERNS` 目前只有两条：`Bearer\s+[^\s,;]+` 与 `\b(?:sk|ek)[-_][A-Za-z0-9_-]{6,}\b`。Followin 的 key 是 **32 字节无前缀 hex，且走 `x-api-key` header 而非 `Bearer`**，两条规则**一条都匹配不到**。须新增一条覆盖 `x-api-key` header 值（形如 `(?i)x-api-key["'\s:=]+[A-Za-z0-9]{16,}`），否则 key 一旦随 httpx 异常或请求日志落盘即为明文。这是 Grok 时代不存在的新暴露面——xAI 走的是 `Authorization: Bearer`，恰好被第一条规则兜住了。

---

## 附录：执行期的契约修正（2026-08-21）

**`time_range=30d` → `days=30`（整型）。** 原 §9 的 `time_range=30d` 字符串格式是从上游
Followin 工具的入参格式沿用来的，不适合作我们自己的对外契约：整型可由框架直接校验、
无需解析。实现采用 `days: int`（`0` 或不传 = 服务端默认 30 天，上限 365）。

`/v1/agent/news` 是本次新增的端点，此前不在 `docs/api/openapi.json` 里，**没有任何既有
客户端依赖**，因此该修正无兼容代价。移动端请按 `days` 整型实现。
