# Followin 新闻聚合部署说明

对外只有一个只读端点 `GET /v1/agent/news`：标签过滤 / 关键词搜索 / 游标分页。数据来源是
Followin MCP（`app/news/followin_client.py`），由后台采集循环（`app/news/collector.py`）
按固定间隔拉取、清洗、打标后写入本地 `news` 表；查询接口只读本地表，不透传上游。

## 数据库前置：`pg_trgm`

关键词检索依赖 `pg_trgm`。`PostgresNewsStore.create_schema()`
（`app/news/pg_store.py:111`）会在 PostgreSQL 上自动尝试：

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

这条语句**需要建库权限**。若运行账号无权限，请由 DBA 在部署前手动执行一次。

**未安装扩展时服务仍可启动、检索仍可用**，只是所有 `ILIKE` 检索退化为顺序扫描
（Seq Scan），性能下降但功能不受影响。

## 环境变量

以下字段名与 `app/config.py` 当前实现逐一核对，均为 `AGENT_` 前缀（表中省略前缀）：

| 变量 | 建议值 | 说明 |
|---|---|---|
| `FOLLOWIN_API_KEY` | 必填（Secret） | Followin MCP 鉴权 key（`x-api-key`）。`provider_mode=real` 时缺失会启动失败（`app/config.py` `validate_runtime_mode`），因为一旦静默退化为 Fake 新闻会污染交易决策链 |
| `FOLLOWIN_MCP_URL` | 默认值即可 | 默认 `https://mcp.followin.io/v2/mcp` |
| `FOLLOWIN_TIMEOUT_SECONDS` | 默认值即可 | 默认 `30.0`（`gt=0, le=120`）；实测单次调用约 2s |
| `NEWS_COLLECT_ENABLED` | `true` | 默认 `false`，部署时需显式打开（沿用原 grok_monitor 的保守风格） |
| `NEWS_COLLECT_INTERVAL_SECONDS` | `300` | 默认 300，取值范围 `[60, 3600]`。上游 `limit` 上限 50、无分页，media 源实测约 2 条/分 → 50 条约 25 分钟耗尽；300s 留 5x 余量 |
| `NEWS_COLLECT_LIMIT` | `50` | 默认 50，`pydantic` 已限死 `ge=1, le=50`；`NewsCollector` 构造时越界会直接抛 `ValueError`，不是运行时才报错 |
| `NEWS_WARMUP_ON_START` | 首次部署 `true`，随后改回 `false` | 一次性冷启动预热；上游无历史回填能力 |
| `NEWS_DEFAULT_TIME_RANGE_DAYS` | `30` | 默认 30（`[1, 365]`）。`/news` 未传 `days` 时的护栏：2 字中文关键词无法用 `pg_trgm` 索引，必须钉住扫描范围，见下文「已知限制」 |
| `NEWS_RETENTION_DAYS` | `90` | 默认 90（`[1, 3650]`）。约 2900 条/天，90 天量级可控 |
| `NEWS_QUERY_RATE_LIMIT` | `60` | 默认 60（`[0, 100000]`），配合下方窗口；`0` 表示不限流 |
| `NEWS_QUERY_RATE_WINDOW_SECONDS` | `60` | 默认 60（`[1, 86400]`) |
| `NEWS_TRANSLATE_TITLES` | `true` | 默认 `true`。入库时把英文标题翻成中文（见下文「标题翻译」）。仅 `provider_mode=real` 生效；关掉只影响新入库条目的标题语言，采集本身不受影响 |

## 标题翻译（推荐流全英文标题的解法）

Followin 的 trending route（推荐 tab 的唯一来源）**只给英文标题，正文却是中文
结构化摘要**——staging 实测标题中文率 0/50、正文中文率 50/50。上游不提供中文标题
字段，所以只能在我们这边补。

- **在哪翻**：采集入库前（`app/news/collector.py` `_with_zh_titles`），不是查询时。
  查询是用户点开新闻页的同步路径，而统一网关单次实测 1.5~3 分钟；同一条新闻被读
  成千次、只入库一次。
- **用什么翻**：统一网关的同一个 LLM 实例（`AGENT_LLM_*`），不额外配模型或客户端。
- **翻哪些**：判据是「标题无中文」（`title_translator.needs_zh_title`）。
  无论推荐流还是各分类 tab（AI 与科技、股票市场等海外媒体源），只要标题不含中文一律翻译为中文标题，
  满足全 tab 的中文浏览体验；原始英文标题存入 `title_en` 备查。
- **成本闸门**：已有译文的条目不再送翻译（`NewsStore.translated_ids`）。trending
  route 每 5 分钟回同样的 50 条，少了这道闸门同一条新闻一天要翻 288 次。稳态下每轮
  只翻当轮新出现的热榜条目。
- **失败行为**：翻译拿不到结果时**保留英文原标题照常入库**，不阻断采集。日志
  `news 标题翻译 待翻=N 成功=M`；网关异常时是 `news 标题翻译异常，本轮保留英文原标题`。

原始英文标题存进 `news.title_en` 列，中文进 `title`。`GET /v1/agent/news?lang=en`
返回 `title_en`（没有译文的条目回落到 `title`，绝不返回空标题）。关键词检索同时匹配
`title` / `title_en` / `content`，所以标题翻成中文后英文关键词依然搜得到。

### `lang` 参数语义已变更（前端需知）

`lang` 从**数据过滤条件**改成**标题展示语言**：

| 传值 | 行为 |
|---|---|
| 不传 / `cn` / `zh` / `zh-cn` | 中文标题（默认） |
| `en` | 上游英文原标题 |

改动原因：旧实现把 `lang` 翻成 `WHERE lang = ?`，而 trending route 不返回
`source_lang`、那批条目的 `lang` 列恒为空——推荐 tab 传 `lang=zh-cn` 会返回
**0 条**（staging 实测），恰好滤掉最需要中文标题的那一批。现在它只决定标题取哪一列，
**不会减少返回条数**。

### 存量数据

`title_en` 是新列，`create_schema()` 用 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
补上（每次启动都跑，可重复执行）。

存量行不需要手工处理：同一 `news_id` 再次被采集到时，正文与译文会写回该行
（`app/news/store.py` `refreshed_row_fields`），所以摘要前缀清洗与标题翻译对仍在
热榜上的存量条目会在下一轮采集后自动生效。已经掉出热榜的旧条目保持原样，随
`NEWS_RETENTION_DAYS` 自然过期。

需要立刻全干净的话，可一次性 `DELETE FROM news WHERE source = 'trending'`（连带
`DELETE FROM news_tags WHERE news_id NOT IN (SELECT news_id FROM news)`），推荐流
数据约 25 分钟内由采集循环重建。**不建议清空整表**：media/research 那 2900 条/天
没有历史回填能力，删了就真没了。

## 运维注意：限流只按对端 IP

`GET /v1/agent/news` 无鉴权，限流键**只按对端 IP 计**（`app/api/news.py` `_caller_id`），
不采用 `X-User-Id`——该 header 由调用方任意指定，用它做限流键等于放开配额（攻击者每次换一个
值就能落进新的配额桶）。代价是**同一 NAT 后的所有用户共享同一份配额**，公司网络、校园网等
场景下容易互相顶配额。这在 `q=` 命中 2 字中文关键词、`pg_trgm` 退化为顺序扫描的场景下尤其
敏感：配额=保护 PG 不被反复打顺序扫描的唯一开关，调大 `NEWS_QUERY_RATE_LIMIT` 前请评估这一权衡。

## 首次部署顺序

1. 确认 `pg_trgm` 已就绪（见上）。
2. `NEWS_WARMUP_ON_START=true` 启动一次，日志出现「news 冷启动预热新增 N 条」。
3. 改回 `false` 重启。
4. 观察日志「news 采集本轮新增 N 条」按 5 分钟节奏出现。

## 已知限制（须同步给产品）

- **上线首日新闻纵深有限，这是上游能力所限，不是缺陷**：上游无分页、无历史回填，
  `time_range` 只是过滤窗口、不能翻页。实测 `limit=50 + time_range=1d` 只回溯约 25 分钟
  的内容；首日约 500 条（含预热），次日起约 2900 条/天。
- `twitter` / `telegram` 源不入库：实测这两个源 100% 无标题，新闻卡片渲染不出来。
  KOL 观点检索属二期需求，本轮不做。
- **非推荐 tab 的标题语言仍然混杂**，这是上游各原站直出、不是缺陷：staging 实测
  货币/AI 与科技/宏观三个 tab 的标题中文率约 48%~52%，其中还混有韩语条目。这些
  条目正文也是原文语言，按上面的判据不会被翻译。若产品要求全站中文，需要连正文
  一起翻，成本与延迟是另一个量级，须单独评估。
- 2 字中文关键词（如「降息」「关税」）无法提取出完整 trigram，`pg_trgm` 索引用不上，
  退化为顺序扫描；由 `/news` 默认 30 天时间窗（`NEWS_DEFAULT_TIME_RANGE_DAYS`）兜住扫描范围，
  防止全表扫描失控。
- `pg_trgm` 扩展需要建库权限，服务账号通常没有；已在上方「数据库前置」写明由 DBA 预先执行。

## 上线前校验一：配额周期（已实测，只读探测）

用真实 key 对 Followin MCP 的 `news` 工具连续发起 5 次只读请求
（`sources=["media"], limit=5`，未落任何数据），观察 `meta.quota` 字段：

```text
请求 1: {'consumed': 0, 'used': 0, 'limit': 100, 'remaining': 100}
请求 2: {'consumed': 0, 'used': 0, 'limit': 100, 'remaining': 100}
请求 3: {'consumed': 0, 'used': 0, 'limit': 100, 'remaining': 100}
请求 4: {'consumed': 0, 'used': 0, 'limit': 100, 'remaining': 100}
请求 5: {'consumed': 0, 'used': 0, 'limit': 100, 'remaining': 100}
```

观察：`limit=100`，但 `consumed`/`used` 在连续 5 次调用后始终为 `0`、`remaining` 始终为
`100`。响应 `meta` 里没有周期/重置时间字段（`filters_applied`、`mode`、`request_id`、
`search_strategy`、`status`、`total`、`verbosity`，均无 period/reset）。

仅凭这次探测无法判定该配额是日配额、月配额还是完全不限量——`consumed` 恒为 0 既可能是
配额极宽松，也可能是该字段本就不随调用更新。所以当时的结论是「须向 Followin 书面确认后
再定论」。

### 结论（2026-08-21，已确认闭环）

在 Followin 官方后台控制台核对，官方明确标注：**新闻与订阅不限量、不消耗配额**。
响应里那个 `limit: 100`（以及后台显示的 30 次限制）只作用于**指标与信号**（`metrics` /
`signal`）和 **Twitter** 工具，与我们第一期用的 `news` 工具无关。

因此：采集器每轮 3 次、每天约 864 次 `news` 调用**不受配额约束**，无需为此调大
`NEWS_COLLECT_INTERVAL_SECONDS`。这也解释了探测中 `consumed` 为何恒为 0 ——
`news` 本就不计数。

**这条约束对二期有效**：二期若接入 `metrics` / `signal` / `twitter`（Agent 按需检索
KOL 观点、代币行情），那些工具**是**计配额的，届时必须重新核算调用预算。

## 上线前校验二：中文检索延迟（未完成，需运维执行）

**本会话无法完成，不得凭估算收尾。** 原因：本地测试环境只有 SQLite 内存库，没有真实
PostgreSQL，也没有跑满 24 小时、量级约 2900 条/天的真实采集数据；SQLite 与 PostgreSQL 的
查询计划器、索引行为完全不同，任何在本地测出的数字都不能代表生产延迟。

**待办（部署到真实 PG 并采集运行 ≥24 小时后执行）：**

```sql
EXPLAIN ANALYZE
SELECT * FROM news
WHERE published_at >= now() - interval '30 days'
  AND (title ILIKE '%降息%' OR content ILIKE '%降息%')
ORDER BY published_at DESC, news_id DESC
LIMIT 21;
```

判据：
- 预期出现 `Seq Scan`（2 字中文词提取不出完整 trigram，`pg_trgm` 索引用不上，这是符合预期的
  退化，不代表配置错误）。
- 记录实际耗时。若 **超过 500ms**，按 spec §10.2 评估引入 `pg_bigm`（对 CJK 更友好的分词
  索引）或 pgvector（语义检索路线）。
- 这一项在部署上线前必须补测实际数字，不得用本文档的估算或 SQLite 结果替代。

## 上线前校验三：轮换 API Key（未完成，需用户/运维在 Followin 后台执行）

设计讨论期间，当前 `AGENT_FOLLOWIN_API_KEY` 曾以明文形式出现在协作对话上下文中，
应视为已泄露，需要轮换。**这是 Followin 后台控制台的操作，Runtime 代码或本次任务范围内
无法代为执行**，待办如下：

1. 登录 Followin 开发者后台，为本服务生成新的 API key。
2. 新 key 只写入 secret manager（如 GitHub Secret / Vault），**不进 `.env`、不进 git、
   不进任何文档**。
3. 更新部署环境的 `AGENT_FOLLOWIN_API_KEY` 后重新部署。
4. 重跑「上线前校验一」的探测脚本（只读，`sources=["media"], limit=5`），确认新 key
   握手成功、`meta.quota` 正常返回。
5. 在 Followin 后台确认旧 key 已失效/吊销。
