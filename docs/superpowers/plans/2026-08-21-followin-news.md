# Followin 新闻聚合（第一期）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Followin 官方 MCP 端点替代已下线的 Grok，把新闻采集入库、打上标签体系，并提供全新的 `GET /v1/agent/news`（tag 过滤 / 关键词搜索 / 游标分页）。

**Architecture:** 后台采集器每 5 分钟经一个自研的 httpx 薄 MCP 客户端拉三条 route（media / research / trending），归一化后打标签、去重 upsert 进 PostgreSQL 的 `news` + `news_tags` 两张表；查询接口只读本地表，不透传上游。`/grok/*` 整套彻底删除，不留适配层。

**Tech Stack:** Python 3.12、FastAPI、pydantic v2、httpx、SQLAlchemy 2.x async Core、asyncpg（测试用 aiosqlite）、pytest + respx

**Spec:** `docs/superpowers/specs/2026-08-21-followin-news-design.md`

## Global Constraints

- Python `>=3.12`；ruff `target-version = py312`、`line-length = 100`。
- pytest `asyncio_mode = "auto"`——异步测试**不要**加 `@pytest.mark.asyncio`。
- 测试不得连真实 Redis / PG / Followin。PG 相关测试一律用 `sqlite+aiosqlite://` 内存库 + `StaticPool`（照 `tests/test_stores.py:132-140`）。
- **不可变写法**：不原地修改已有对象，整体换新（照 `app/services/grok_cache.py` 的 `InMemorySnapshotStore.put`）。
- 注释与错误文案用中文，与仓库现有风格一致。
- 单文件目标 200~400 行，硬上限 800 行。
- 目标测试覆盖率 80%+。
- httpx 客户端**一律由 `app/container.py` 创建并登记到 `_http_clients`**，由 `aclose()` 统一关闭；业务模块不自建、不关闭。
- 错误响应一律走 `app/api/errors.py:error_response(code, message, status_code, details=None)`。`code` 必须是 `app/api/schemas.py:ErrorCode` 的字面量之一：`INVALID_REQUEST` / `UNAUTHENTICATED` / `FORBIDDEN` / `AUTH_UPSTREAM_UNAVAILABLE` / `UNAUTHORIZED` / `TOOL_NOT_IN_SCOPE` / `SESSION_NOT_FOUND` / `IDEMPOTENCY_CONFLICT` / `SESSION_EXPIRED` / `RATE_LIMITED` / `INTERNAL` / `TOOL_UPSTREAM_TIMEOUT` / `POLICY_VIOLATION`。**没有 `NOT_FOUND`，不要凭直觉用。**
- 标签取值恒为这五个：`recommend` / `crypto` / `stocks` / `ai_tech` / `macro`。
- 上游 `news` 工具 `limit` 上限 **50**（硬约束，别写更大）。
- 常驻循环里 `asyncio.CancelledError` **必须 re-raise**，不能被宽泛的 `except Exception` 吞掉。

---

### Task 1: Grok 整套下线 + Followin 配置 + 日志脱敏

先做下线，让后续任务在干净的树上开工。本任务同时引入 `followin_api_key` 配置与它的日志脱敏规则——key 的配置和它的防泄漏必须同时落地，不能先让 key 存在、后补脱敏。

**Files:**
- Delete: `app/api/grok.py`、`app/services/grok.py`、`app/services/grok_monitor.py`、`app/services/grok_cache.py`、`scripts/grok_refresh.py`、`tests/test_grok_cache.py`、`tests/test_grok_monitor.py`、`tests/api/test_grok.py`
- Modify: `app/config.py`、`app/container.py`、`app/main.py:66`、`app/telemetry/logging.py:10-13`、`tests/test_realtime_security.py:58`
- Test: `tests/test_logging_redaction.py`（新建）
- **保留不动**：`app/services/webhook.py`、`tests/test_webhook_url.py`（`check_webhook_url` 是 SSRF 防护，二期做增量通知复用）、`app/services/round_lock.py`（Task 6 用）、`app/services/rate_limit.py`（Task 7 用）

**Interfaces:**
- Consumes: 无
- Produces: `Settings.followin_api_key: str`、`Settings.followin_mcp_url: str`、`Settings.followin_timeout_seconds: float`、`app.telemetry.logging.redact_secrets(value: str) -> str`（已存在，新增一条规则）

- [ ] **Step 1: 写失败测试——脱敏规则覆盖 x-api-key**

创建 `tests/test_logging_redaction.py`：

```python
"""日志脱敏规则测试。

Followin 的 key 是 32 字节无前缀 hex，走 x-api-key header 而非 Bearer；
仓库原有两条规则（Bearer / sk-ek 前缀）一条都匹配不到它。
"""

from __future__ import annotations

from app.telemetry.logging import redact_secrets

# 合成占位值，与任何真实 API key 无关 —— 仅用于测试脱敏规则
_FAKE_KEY = "deadbeefdeadbeefdeadbeefdeadbeef"


def test_redacts_x_api_key_header_value() -> None:
    line = f"POST https://mcp.followin.io/v2/mcp headers={{'x-api-key': '{_FAKE_KEY}'}}"
    assert _FAKE_KEY not in redact_secrets(line)


def test_redacts_x_api_key_in_env_style_assignment() -> None:
    assert _FAKE_KEY not in redact_secrets(f"AGENT_FOLLOWIN_API_KEY={_FAKE_KEY}")


def test_keeps_existing_bearer_rule_working() -> None:
    assert "abc123def456" not in redact_secrets("Authorization: Bearer abc123def456")


def test_leaves_ordinary_text_untouched() -> None:
    assert redact_secrets("news collected count=50") == "news collected count=50"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/pytest tests/test_logging_redaction.py -v`
Expected: `test_redacts_x_api_key_header_value` 与 `test_redacts_x_api_key_in_env_style_assignment` FAIL（key 原样留在输出里）；另两个 PASS。

- [ ] **Step 3: 新增脱敏规则**

修改 `app/telemetry/logging.py`，把 `_SECRET_PATTERNS` 改成：

```python
_SECRET_PATTERNS = (
    re.compile(r"(?i)Bearer\s+[^\s,;]+"),
    re.compile(r"\b(?:sk|ek)[-_][A-Za-z0-9_-]{6,}\b"),
    # Followin 的 key 是 32 字节无前缀 hex，走 x-api-key header，
    # 上面两条一条都匹配不到。按「键名 + 分隔符 + 值」整体替换。
    re.compile(r"(?i)(x[-_]api[-_]key|FOLLOWIN_API_KEY)([\"'\s:=]+)[A-Za-z0-9]{16,}"),
)
```

注意替换成 `[REDACTED]` 会把键名也吃掉，这是可接受的（日志里丢一个键名换不泄露），不必为保留键名再引入分组回填。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/pytest tests/test_logging_redaction.py -v`
Expected: 4 passed

- [ ] **Step 5: 删除 Grok 全部文件**

```bash
git rm app/api/grok.py app/services/grok.py app/services/grok_monitor.py \
       app/services/grok_cache.py scripts/grok_refresh.py \
       tests/test_grok_cache.py tests/test_grok_monitor.py tests/api/test_grok.py
```

- [ ] **Step 6: 清理 `app/config.py` 的 Grok 配置，换成 Followin**

删除从注释 `# xAI Grok (推特/X 实时热点与资讯监控能力)` 起到 `grok_monitor_category_list` property 结束的整段（含 `xai_api_key`、`xai_base_url`、`grok_model`、`grok_structured_output`、`grok_timeout_seconds`、`grok_monitor_interval_seconds`、`grok_cache_ttl_seconds`、`grok_expensive_*`、`grok_monitor_enabled`、`grok_monitor_webhook_url`、`grok_monitor_categories` 及那个 property）。原位换成：

```python
    # Followin 新闻聚合（MCP 端点，x-api-key 鉴权）。实测单次调用约 2s。
    followin_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("AGENT_FOLLOWIN_API_KEY", "FOLLOWIN_API_KEY"),
    )
    followin_mcp_url: str = "https://mcp.followin.io/v2/mcp"
    followin_timeout_seconds: float = Field(default=30.0, gt=0, le=120)

    # 采集默认关：由运维显式确认后再打开（沿用原 grok_monitor 的保守风格）
    news_collect_enabled: bool = False
    # 上游无分页、limit 上限 50，media 源实测 2 条/分 → 50 条约 25 分钟耗尽。
    # 300s 留 5x 余量，足以扛新闻潮密度翻倍。
    news_collect_interval_seconds: int = Field(default=300, ge=60, le=3600)
    news_collect_limit: int = Field(default=50, ge=1, le=50)
    # 一次性冷启动预热：上游无历史回填能力，首轮只能拿到最近约 25 分钟的内容
    news_warmup_on_start: bool = False
    # /news 未传 time_range 时的护栏：2 字中文关键词无法用 pg_trgm 索引，
    # 必须钉住扫描范围
    news_default_time_range_days: int = Field(default=30, ge=1, le=365)
    news_retention_days: int = Field(default=90, ge=1, le=3650)
    news_query_rate_limit: int = Field(default=60, ge=0, le=100000)
    news_query_rate_window_seconds: int = Field(default=60, ge=1, le=86400)
```

- [ ] **Step 7: 清理 `app/config.py` 的校验逻辑**

删除「快照缓存 TTL 必须大于监控间隔」那段 `if self.grok_monitor_enabled and self.grok_cache_ttl_seconds <= ...` 的告警块（约 `config.py:245`，含其上方注释）——`grok_cache_ttl_seconds` 已不存在，留着直接 `AttributeError`。

在 `common_missing` 列表里把 XAI 那项替换掉：

```python
                ("AGENT_OPENROUTER_API_KEY", self.openrouter_api_key),
                # 缺 Key 时 Followin 会静默退化成 Fake 新闻，喂给交易决策链很危险，
                # 宁可启动失败也不放模拟数据进 real 环境
                ("AGENT_FOLLOWIN_API_KEY", self.followin_api_key),
```

- [ ] **Step 8: 清理 `app/container.py`**

删除：`from app.services.grok import ...`、`from app.services.grok_cache import ...`、`from app.services.grok_monitor import ...` 三行 import；`Container` 数据类里的 `grok_service` / `grok_monitor` / `grok_monitor_task` 三个字段；`startup()` 里「X 热点监控」那个 `if` 块；`aclose()` 里 `grok_monitor_task` 的 cancel 块；`build_container()` 里从 `grok_http = httpx.AsyncClient(...)` 到 `grok_monitor = GrokMonitor(...)` 的整段装配，以及返回 `Container(...)` 时的 `grok_service=` / `grok_monitor=` 两个实参。

**这一步有个陷阱，务必看清。** `round_lock` 与 `rate_limiter` 的装配和 grok 的 `snapshot_store` **夹在同一个 if/else 块里**（约 `container.py:381-394`）。Task 6、7 要用 `round_lock` 和 `rate_limiter`，**不能把整块删掉**。只删 `snapshot_store` 的两行并改注释，把该块改成：

```python
    # 有 Redis 就让轮次锁与限流计数跨 worker 共享：否则多 worker 下采集每进程
    # 各拉一轮（上游配额白烧），限流额度也每进程各放一份。
    if redis_client is not None:
        round_lock: RoundLock = RedisRoundLock(redis_client, key="news:collect:round")
        rate_limiter: RateLimiter = RedisRateLimiter(redis_client, prefix="news:rl:")
    else:
        round_lock = SingleProcessRoundLock()
        rate_limiter = InMemoryRateLimiter()
        if settings.provider_mode == "real":
            logger.warning(
                "未配置 AGENT_REDIS_URL：新闻采集轮次锁与限流均降级为进程内，"
                "多 worker 部署会导致每个进程各自采集一轮"
            )
```

显式传 `key=` / `prefix=` 是必需的：这两个类的默认值分别是 `"grok:monitor:round"` 与 `"grok:rl:"`（`round_lock.py:42`、`rate_limit.py:47`），沿用会留下语义错乱的 Redis key。`round_lock.py` / `rate_limit.py` 两个文件本身不改。

同时删除 `SnapshotStore` / `RedisSnapshotStore` / `InMemorySnapshotStore` 的 import。

- [ ] **Step 9: 清理 `app/main.py`**

删除 `grok_router` 的 import 与 `app.include_router(grok_router, prefix="/v1/agent")`（`main.py:66`）。

- [ ] **Step 10: 同步安全断言测试**

修改 `tests/test_realtime_security.py` 的参数化列表（约 58 行），把

```python
        # 缺 Grok Key 时 /grok/* 会静默返回模拟热点，real 模式必须启动即失败
        {"xai_api_key": ""},
```

替换为

```python
        # 缺 Followin Key 时新闻链路会静默返回模拟数据，real 模式必须启动即失败
        {"followin_api_key": ""},
```

同时检查该文件顶部的 `REAL` 字典：若含 `"xai_api_key"` 键，改成 `"followin_api_key"`；若不含则须**新增** `"followin_api_key": "followin-key-for-test"`，否则 `REAL` 基线本身就缺这个必填项，所有参数化用例都会因同一个原因通过而失去区分力。

- [ ] **Step 11: 全量搜索残留引用**

```bash
grep -rn -i 'grok\|xai' app scripts tests --include='*.py' | grep -v __pycache__
```

Expected: 无输出。有输出就继续清，直到干净。

- [ ] **Step 12: 跑全量测试**

Run: `.venv/bin/pytest -q`
Expected: 全绿。若 `tests/test_rate_limit.py` / `tests/test_webhook_url.py` 因 key 前缀里的 `grok:` 字样断言而失败，那是断言在测常量值——改断言，不改 `round_lock.py` / `rate_limit.py`。

- [ ] **Step 13: Commit**

```bash
git add -A
git commit -m "refactor: 下线 Grok 全套，引入 Followin 配置与 x-api-key 日志脱敏"
```

---

### Task 2: 数据模型与标签器

纯函数、无 I/O，最快拿到测试反馈。词边界是本任务的核心风险点。

**Files:**
- Create: `app/news/__init__.py`（空文件）、`app/news/models.py`、`app/news/tagger.py`
- Test: `tests/news/__init__.py`（空文件）、`tests/news/test_tagger.py`、`tests/news/test_models.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `app.news.models.NewsTag`（`Literal["recommend","crypto","stocks","ai_tech","macro"]`）
  - `app.news.models.NEWS_TAGS: tuple[NewsTag, ...]`
  - `app.news.models.NewsItem`（内部完整记录）字段：`news_id: str`、`dedup_key: str`、`source: str`、`source_name: str`、`source_url: str`、`source_quality: str`、`title: str`、`content: str`、`lang: str`、`asset_type: str`、`upstream_category: str`、`published_at: datetime`、`tags: tuple[NewsTag, ...]`、`raw: dict[str, Any]`
  - `app.news.models.NewsCard`（对外投影）+ 类方法 `NewsCard.of(item: NewsItem) -> NewsCard`
  - `app.news.models.NewsPage`：`items: list[NewsCard]`、`next_cursor: str | None`
  - `app.news.models.NewsQuery`：`tag: NewsTag | None`、`q: str`、`lang: str`、`since: datetime | None`、`limit: int`、`cursor: str | None`
  - `app.news.tagger.tags_for(*, title: str, content: str, asset_type: str, provenance: str) -> tuple[NewsTag, ...]`
  - `app.news.tagger.keyword_tags(text: str) -> tuple[NewsTag, ...]`

- [ ] **Step 1: 写失败测试——标签器**

创建 `tests/news/__init__.py`（空）与 `tests/news/test_tagger.py`：

```python
"""标签器测试。重点是 ASCII 短词的词边界——这是最容易出错的地方。"""

from __future__ import annotations

from app.news.tagger import keyword_tags, tags_for


def test_ascii_acronym_requires_word_boundary() -> None:
    # "said" / "Thailand" / "maintain" 都含子串 ai，绝不能命中 ai_tech
    for text in ("he said nothing", "Thailand exports rise", "maintain the position"):
        assert "ai_tech" not in keyword_tags(text)


def test_ascii_acronym_matches_as_standalone_word() -> None:
    assert "ai_tech" in keyword_tags("AI model training costs surge")
    assert "ai_tech" in keyword_tags("an AI-driven rally")  # 连字符是词边界


def test_mixed_case_term_is_case_sensitive() -> None:
    # "Fed" 大小写敏感：否则 "he fed the dog" 会被判成宏观新闻
    assert "macro" not in keyword_tags("he fed the dog this morning")
    assert "macro" in keyword_tags("Fed signals a rate cut")


def test_all_lowercase_term_is_case_insensitive() -> None:
    assert "macro" in keyword_tags("Tariff threat escalates")
    assert "macro" in keyword_tags("tariff threat escalates")


def test_cjk_term_matches_as_substring() -> None:
    assert "ai_tech" in keyword_tags("英伟达发布新一代算力芯片")
    assert "macro" in keyword_tags("美联储宣布降息25个基点")


def test_multiple_tags_can_match() -> None:
    tags = keyword_tags("美联储降息推动英伟达股价上涨")
    assert "macro" in tags and "ai_tech" in tags


def test_asset_type_maps_to_crypto_and_stocks() -> None:
    assert tags_for(title="BTC up", content="", asset_type="crypto", provenance="feeds") == (
        "crypto",
    )
    assert tags_for(title="AAPL up", content="", asset_type="tradfi", provenance="feeds") == (
        "stocks",
    )


def test_trending_provenance_yields_recommend() -> None:
    tags = tags_for(
        title="BTC breaks 71500", content="", asset_type="crypto",
        provenance="followin_trending",
    )
    assert tags[0] == "recommend" and "crypto" in tags


def test_tags_are_deduplicated_and_order_stable() -> None:
    tags = tags_for(
        title="美联储降息", content="美联储再次降息", asset_type="", provenance="feeds",
    )
    assert tags == ("macro",)


def test_no_signal_yields_empty() -> None:
    assert tags_for(title="hello", content="world", asset_type="", provenance="feeds") == ()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/pytest tests/news/test_tagger.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.news'`

- [ ] **Step 3: 实现 `app/news/models.py`**

```python
"""新闻领域模型。

NewsItem 是内部完整记录（含 raw / dedup_key，供采集与存储用）；
NewsCard 是对外投影 —— raw 里有上游原始 HTML，绝不能进响应体。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, Field

NewsTag = Literal["recommend", "crypto", "stocks", "ai_tech", "macro"]
NEWS_TAGS: Final[tuple[NewsTag, ...]] = ("recommend", "crypto", "stocks", "ai_tech", "macro")


class NewsItem(BaseModel):
    """一条入库新闻的完整记录。"""

    news_id: str = Field(description="sha256(source_url)；无 url 时取 source_name+content 摘要")
    dedup_key: str = Field(description="规范化标题，用于跨 route 合并")
    source: str = Field(description="media | research | trending")
    title: str
    content: str = Field(description="剥 HTML 后的纯文本")
    published_at: datetime
    source_name: str = ""
    source_url: str = ""
    source_quality: str = ""
    lang: str = ""
    asset_type: str = ""
    upstream_category: str = ""
    tags: tuple[NewsTag, ...] = ()
    raw: dict[str, Any] = Field(default_factory=dict, description="上游原始行，含原始 HTML")


class NewsCard(BaseModel):
    """对外投影。不含 raw / dedup_key / source_quality / upstream_category。"""

    news_id: str
    title: str
    content: str
    source_name: str
    source_url: str
    published_at: datetime
    tags: list[NewsTag]
    asset_type: str
    lang: str

    @classmethod
    def of(cls, item: NewsItem) -> NewsCard:
        return cls(
            news_id=item.news_id,
            title=item.title,
            content=item.content,
            source_name=item.source_name,
            source_url=item.source_url,
            published_at=item.published_at,
            tags=list(item.tags),
            asset_type=item.asset_type,
            lang=item.lang,
        )


class NewsPage(BaseModel):
    items: list[NewsCard]
    next_cursor: str | None = Field(default=None, description="null 表示已到底")


class NewsQuery(BaseModel):
    """一次查询的全部条件。"""

    tag: NewsTag | None = None
    q: str = ""
    lang: str = ""
    since: datetime | None = None
    limit: int = 20
    cursor: str | None = None
```

- [ ] **Step 4: 实现 `app/news/tagger.py`**

```python
"""关键词规则打标。

`news` 工具不返回任何行业/主题维度（macro 只存在于 metrics 工具），
所以 ai_tech 与 macro 必须自建规则。crypto/stocks 吃上游 asset_type，
recommend 吃 provenance。

**大小写与词边界规则**（这是本模块唯一的复杂点）：
- 含 CJK 的词：直接子串匹配（中文没有词边界概念）
- 含大写字母的 ASCII 词：加 \\b 边界 + **大小写敏感**
  —— 否则 "Fed" 会命中 "he fed the dog"，"AI" 会命中 "said"/"Thailand"
- 全小写 ASCII 词：加 \\b 边界 + 大小写无关（如 tariff / inflation）

代价：全大写缩写要求原文大写（新闻标题里 CPI/GDP 基本都大写），
lowercase 的 "cpi" 不命中。这是刻意取舍，误报比漏报更难排查。
"""

from __future__ import annotations

import re
from typing import Final

from app.news.models import NewsTag

_KEYWORD_RULES: Final[dict[NewsTag, tuple[str, ...]]] = {
    "ai_tech": (
        "NVDA", "NVIDIA", "英伟达", "AMD", "TSMC", "台积电", "OpenAI", "ChatGPT",
        "Anthropic", "Claude", "Gemini", "LLM", "GPU", "算力", "芯片", "半导体",
        "AI", "人工智能", "大模型", "数据中心",
    ),
    "macro": (
        "CPI", "PPI", "PCE", "GDP", "非农", "NFP", "FOMC", "美联储", "Fed",
        "降息", "加息", "利率", "关税", "tariff", "通胀", "inflation", "失业率",
        "国债", "Treasury", "收益率", "yield", "央行", "财政部", "Powell", "鲍威尔",
    ),
}


def _compile(term: str) -> re.Pattern[str]:
    escaped = re.escape(term)
    if not term.isascii():
        return re.compile(escaped)
    flags = 0 if any(char.isupper() for char in term) else re.IGNORECASE
    return re.compile(rf"\b{escaped}\b", flags)


_COMPILED: Final[dict[NewsTag, tuple[re.Pattern[str], ...]]] = {
    tag: tuple(_compile(term) for term in terms) for tag, terms in _KEYWORD_RULES.items()
}


def keyword_tags(text: str) -> tuple[NewsTag, ...]:
    """按规则表匹配出 tag。命中多个则全给。"""
    return tuple(
        tag for tag, patterns in _COMPILED.items()
        if any(pattern.search(text) for pattern in patterns)
    )


def tags_for(
    *, title: str, content: str, asset_type: str, provenance: str
) -> tuple[NewsTag, ...]:
    """一条新闻的完整标签。去重且保序。"""
    found: list[NewsTag] = []
    if provenance == "followin_trending":
        found.append("recommend")
    normalized_asset = asset_type.strip().lower()
    if normalized_asset == "crypto":
        found.append("crypto")
    elif normalized_asset == "tradfi":
        found.append("stocks")
    found.extend(keyword_tags(f"{title}\n{content}"))
    return tuple(dict.fromkeys(found))
```

- [ ] **Step 5: 运行标签器测试确认通过**

Run: `.venv/bin/pytest tests/news/test_tagger.py -v`
Expected: 10 passed

- [ ] **Step 6: 写模型测试**

创建 `tests/news/test_models.py`：

```python
"""模型测试。重点：NewsCard 绝不能泄漏 raw。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.news.models import NewsCard, NewsItem


def _item() -> NewsItem:
    return NewsItem(
        news_id="abc",
        dedup_key="btc breaks 71500",
        source="media",
        title="BTC breaks 71500",
        content="纯文本正文",
        published_at=datetime(2026, 8, 21, tzinfo=UTC),
        source_name="cointelegraph",
        source_url="https://cointelegraph.com/x",
        source_quality="media",
        lang="zh-cn",
        asset_type="crypto",
        upstream_category="majorcoin",
        tags=("crypto", "recommend"),
        raw={"content": "<p><b>看涨观点</b></p>", "secret": "leak"},
    )


def test_card_omits_raw_and_internal_fields() -> None:
    dumped = NewsCard.of(_item()).model_dump()
    for leaked in ("raw", "dedup_key", "source_quality", "upstream_category"):
        assert leaked not in dumped


def test_card_preserves_public_fields() -> None:
    card = NewsCard.of(_item())
    assert card.news_id == "abc"
    assert card.source_url == "https://cointelegraph.com/x"
    assert card.tags == ["crypto", "recommend"]


def test_item_defaults_are_empty_not_none() -> None:
    item = NewsItem(
        news_id="i", dedup_key="d", source="media", title="t", content="c",
        published_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    assert item.source_url == "" and item.tags == () and item.raw == {}
```

- [ ] **Step 7: 运行测试确认通过**

Run: `.venv/bin/pytest tests/news/ -v`
Expected: 13 passed

- [ ] **Step 8: Commit**

```bash
git add app/news/__init__.py app/news/models.py app/news/tagger.py tests/news/
git commit -m "feat: 新闻领域模型与关键词标签器"
```

---

### Task 3: Followin MCP 客户端

**Files:**
- Create: `app/news/followin_client.py`、`tests/news/test_followin_client.py`、`tests/fixtures/followin_media.json`、`tests/fixtures/followin_trending.json`
- Test: `tests/news/test_followin_client.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `app.news.followin_client.FollowinUpstreamError(RuntimeError)`，属性 `status_code: int | None`、`is_timeout: bool`
  - `app.news.followin_client.FollowinClient(api_key: str, http_client: httpx.AsyncClient, mcp_url: str = "https://mcp.followin.io/v2/mcp")`
  - `FollowinClient.fetch_news(**arguments: Any) -> dict[str, Any]` — 返回上游 `results`/`meta` 那一层解析后的字典
  - `app.news.followin_client.FakeFollowinClient()`，同签名 `fetch_news`
  - `app.news.followin_client.parse_sse_json(body: str) -> dict[str, Any]`

- [ ] **Step 1: 造 fixtures**

创建 `tests/fixtures/followin_media.json`——**search route** 的真实形态（有 `source_url`/`source_lang`/`source_name`，无 `asset_types`）：

```json
{
  "results": {
    "articles": [
      {
        "category": "market",
        "content": "BTC 隔夜大涨，突破 6.9 万美元关口。<p><b>看涨观点</b>：机构资金持续流入。</p>",
        "provenance": "feeds",
        "published_ts": 1787190519312,
        "source_lang": "zh",
        "source_name": "cointelegraph",
        "source_quality": "media",
        "source_url": "https://cointelegraph.com/markets/bitcoin-price-reaches-725k",
        "title": "Bitcoin price reaches 72.5k as inflows accelerate"
      },
      {
        "category": "fundamentals",
        "content": "英伟达发布新一代算力芯片，数据中心营收指引上调。",
        "provenance": "feeds",
        "published_ts": 1787190526095,
        "source_lang": "en",
        "source_name": "benzinga",
        "source_quality": "media",
        "source_url": "https://www.benzinga.com/trading-ideas/movers/26/08/61335604/nvda",
        "title": "Nvidia lifts data center guidance"
      }
    ],
    "social": [
      {
        "category": "",
        "content": "Maya protocol exploit drains about 11M via six-flaw chain.",
        "kol_info": {"name": "muur_posts@telegram"},
        "provenance": "tg_kol_feeds",
        "published_ts": 1787242029793,
        "source_lang": "en",
        "source_name": "muur_posts@telegram",
        "source_quality": "social",
        "source_url": "https://twitter.com/CryptoKaleo/status/2090469542128742910",
        "title": ""
      }
    ]
  },
  "meta": {
    "mode": "search",
    "quota": {"consumed": 0, "used": 0, "limit": 100, "remaining": 100},
    "request_id": "07b924562f6c2f865d631384759e05ac",
    "status": "ok",
    "total": 3,
    "verbosity": "standard"
  }
}
```

创建 `tests/fixtures/followin_trending.json`——**trending route** 的真实形态（**无** `source_url`/`source_lang`/`source_name`，**有** `asset_types`）：

```json
{
  "results": {
    "articles": [
      {
        "asset_type": "crypto",
        "asset_types": ["BTC"],
        "category": "majorcoin",
        "content": "发生了什么？\nBTC 突破 71500，创 6 月以来新高。",
        "provenance": "followin_trending",
        "published_ts": 1787190519312,
        "source_quality": "media",
        "title": "BTC continued its upward trend, breaking through 71500"
      },
      {
        "asset_type": "crypto",
        "asset_types": ["牛来", "BSC", "MEME"],
        "category": "majorcoin",
        "content": "BSC 链上的 Meme 币市值短时突破 6000 万美元。",
        "provenance": "followin_trending",
        "published_ts": 1787215718515,
        "source_quality": "media",
        "title": "Niu Lai Meme market value surpasses 52 million"
      }
    ]
  },
  "meta": {
    "mode": "search",
    "quota": {"consumed": 0, "used": 0, "limit": 100, "remaining": 100},
    "status": "ok",
    "total": 2,
    "verbosity": "standard"
  }
}
```

- [ ] **Step 2: 写失败测试**

创建 `tests/news/test_followin_client.py`：

```python
"""Followin MCP 薄客户端测试。

关键路径：SSE 解析、session 自愈（4xx → 重握手 → 重试一次）、
二次失败抛错。session TTL 上游未说明，自愈是唯一可靠兜底。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.news.followin_client import (
    FakeFollowinClient,
    FollowinClient,
    FollowinUpstreamError,
    parse_sse_json,
)

_URL = "https://mcp.followin.io/v2/mcp"
_FIXTURES = Path(__file__).parent.parent / "fixtures"


def _sse(payload: dict) -> str:
    return f"event: message\ndata: {json.dumps(payload)}\n\n"


def _init_response() -> httpx.Response:
    body = _sse({
        "jsonrpc": "2.0", "id": 1,
        "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "followx-mcp"}},
    })
    return httpx.Response(200, text=body, headers={"mcp-session-id": "SESSION-A"})


def _tool_response(fixture: str) -> httpx.Response:
    inner = json.loads((_FIXTURES / fixture).read_text())
    body = _sse({
        "jsonrpc": "2.0", "id": 9,
        "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
    })
    return httpx.Response(200, text=body)


def test_parse_sse_json_extracts_data_line() -> None:
    assert parse_sse_json(_sse({"a": 1})) == {"a": 1}


def test_parse_sse_json_rejects_body_without_data() -> None:
    with pytest.raises(FollowinUpstreamError):
        parse_sse_json("event: ping\n\n")


async def test_fetch_news_handshakes_then_calls_tool() -> None:
    async with respx.mock(assert_all_called=True) as mock:
        route = mock.post(_URL)
        route.side_effect = [
            _init_response(),                 # initialize
            httpx.Response(202),              # notifications/initialized
            _tool_response("followin_media.json"),
        ]
        async with httpx.AsyncClient() as http:
            client = FollowinClient("key", http, mcp_url=_URL)
            data = await client.fetch_news(sources=["media"], limit=50)
    assert len(data["results"]["articles"]) == 2
    assert data["meta"]["status"] == "ok"


async def test_sends_api_key_and_dual_accept_header() -> None:
    async with respx.mock() as mock:
        route = mock.post(_URL)
        route.side_effect = [
            _init_response(), httpx.Response(202), _tool_response("followin_media.json"),
        ]
        async with httpx.AsyncClient() as http:
            await FollowinClient("secret-key", http, mcp_url=_URL).fetch_news(limit=1)
        request = route.calls[0].request
    assert request.headers["x-api-key"] == "secret-key"
    accept = request.headers["accept"]
    assert "application/json" in accept and "text/event-stream" in accept


async def test_stale_session_is_rehandshaked_and_retried_once() -> None:
    async with respx.mock() as mock:
        route = mock.post(_URL)
        route.side_effect = [
            _init_response(), httpx.Response(202),
            httpx.Response(404, text="session not found"),   # session 过期
            _init_response(), httpx.Response(202),           # 自愈重握手
            _tool_response("followin_trending.json"),        # 重试成功
        ]
        async with httpx.AsyncClient() as http:
            data = await FollowinClient("key", http, mcp_url=_URL).fetch_news(limit=50)
    assert len(data["results"]["articles"]) == 2


async def test_second_failure_raises_upstream_error() -> None:
    async with respx.mock() as mock:
        route = mock.post(_URL)
        route.side_effect = [
            _init_response(), httpx.Response(202),
            httpx.Response(404), _init_response(), httpx.Response(202), httpx.Response(404),
        ]
        async with httpx.AsyncClient() as http:
            with pytest.raises(FollowinUpstreamError) as excinfo:
                await FollowinClient("key", http, mcp_url=_URL).fetch_news(limit=1)
    assert excinfo.value.status_code == 404


async def test_timeout_is_flagged() -> None:
    async with respx.mock() as mock:
        mock.post(_URL).side_effect = httpx.ConnectTimeout("timed out")
        async with httpx.AsyncClient() as http:
            with pytest.raises(FollowinUpstreamError) as excinfo:
                await FollowinClient("key", http, mcp_url=_URL).fetch_news(limit=1)
    assert excinfo.value.is_timeout is True


async def test_tool_error_result_raises() -> None:
    body = _sse({"jsonrpc": "2.0", "id": 9, "error": {"code": -32602, "message": "bad args"}})
    async with respx.mock() as mock:
        route = mock.post(_URL)
        route.side_effect = [_init_response(), httpx.Response(202), httpx.Response(200, text=body)]
        async with httpx.AsyncClient() as http:
            with pytest.raises(FollowinUpstreamError):
                await FollowinClient("key", http, mcp_url=_URL).fetch_news(limit=1)


async def test_fake_client_returns_both_route_shapes() -> None:
    fake = FakeFollowinClient()
    search = await fake.fetch_news(sources=["media"], limit=5)
    trending = await fake.fetch_news(limit=5)
    assert search["results"]["articles"][0]["source_url"]
    # trending route 没有 source_url —— Fake 必须复刻这个差异，否则 normalizer 测不到
    assert "source_url" not in trending["results"]["articles"][0]
```

- [ ] **Step 3: 运行测试确认失败**

Run: `.venv/bin/pytest tests/news/test_followin_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.news.followin_client'`

- [ ] **Step 4: 实现 `app/news/followin_client.py`**

```python
"""Followin MCP 薄客户端（streamable HTTP + x-api-key）。

不用官方 `mcp` SDK：它目前只是 pydantic-ai 的传递依赖，未在 pyproject 声明，
在传递依赖上建生产采集路径不安全；而我们只用 tools/call 一个方法，
SDK 的 ClientSession 又是 async context manager，塞进常驻循环里 lifecycle
和仓库「httpx 客户端由容器创建、aclose() 统一关闭」的范式冲突。

实测要点：
- Accept 必须同时带 application/json 与 text/event-stream，少一个即失败
- 响应是 SSE 文本（event: message\\ndata: {...}），但内容一次给完，不必流式读
- 业务 JSON 被两层包裹：result.content[0].text 里是一个 JSON 字符串
- session TTL 上游未说明 → 4xx 时清 session、重握手、只重试一次
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION: Final = "2025-06-18"
_ACCEPT: Final = "application/json, text/event-stream"
_DEFAULT_URL: Final = "https://mcp.followin.io/v2/mcp"


class FollowinUpstreamError(RuntimeError):
    """Followin 上游调用失败。带足信息供采集器决定跳过还是告警。"""

    def __init__(
        self, message: str, *, status_code: int | None = None, is_timeout: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.is_timeout = is_timeout


def parse_sse_json(body: str) -> dict[str, Any]:
    """从 SSE 文本里取出第一条 data 行的 JSON。"""
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        payload = json.loads(line[len("data:"):].strip())
        if not isinstance(payload, dict):
            raise FollowinUpstreamError("Followin 响应的 data 不是 JSON 对象")
        return payload
    raise FollowinUpstreamError("Followin 响应中没有 data 行")


def _unwrap_tool_result(envelope: dict[str, Any]) -> dict[str, Any]:
    """剥掉 MCP 的两层包裹，拿到业务 JSON。"""
    if "error" in envelope:
        raise FollowinUpstreamError(f"Followin 工具返回错误: {envelope['error']}")
    contents = (envelope.get("result") or {}).get("content") or []
    for part in contents:
        if isinstance(part, dict) and part.get("type") == "text":
            parsed = json.loads(part.get("text") or "{}")
            if isinstance(parsed, dict):
                return parsed
    raise FollowinUpstreamError("Followin 工具响应里没有可解析的 text 内容")


class FollowinClient:
    """真实模式。http_client 由容器注入，本类不自建也不关闭。"""

    def __init__(
        self, api_key: str, http_client: httpx.AsyncClient, mcp_url: str = _DEFAULT_URL
    ) -> None:
        self.api_key = api_key
        self.http_client = http_client
        self.mcp_url = mcp_url
        self._session_id: str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": _ACCEPT,
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            return await self.http_client.post(
                self.mcp_url, headers=self._headers(), json=payload
            )
        except httpx.TimeoutException as exc:
            raise FollowinUpstreamError(f"Followin 调用超时: {exc}", is_timeout=True) from exc
        except httpx.HTTPError as exc:
            raise FollowinUpstreamError(f"Followin 调用失败: {exc}") from exc

    async def _handshake(self) -> None:
        """建立新 session。清掉旧的，避免带着失效 id 去握手。"""
        self._session_id = None
        resp = await self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agentos-agent-runtime", "version": "0.1"},
            },
        })
        if resp.status_code >= 400:
            raise FollowinUpstreamError(
                f"Followin 握手失败: HTTP {resp.status_code}", status_code=resp.status_code
            )
        session_id = resp.headers.get("mcp-session-id")
        if not session_id:
            raise FollowinUpstreamError("Followin 握手响应里没有 mcp-session-id")
        self._session_id = session_id
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> httpx.Response:
        return await self._post({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })

    async def fetch_news(self, **arguments: Any) -> dict[str, Any]:
        """调用 news 工具。session 失效则重握手并只重试一次。"""
        if self._session_id is None:
            await self._handshake()

        resp = await self._call_tool("news", arguments)
        if 400 <= resp.status_code < 500:
            # session 失效的典型表现。只重试一次：上游真挂了就别继续锤它。
            logger.info("Followin session 可能已失效（HTTP %d），重握手后重试一次", resp.status_code)
            await self._handshake()
            resp = await self._call_tool("news", arguments)

        if resp.status_code >= 400:
            raise FollowinUpstreamError(
                f"Followin news 调用失败: HTTP {resp.status_code}", status_code=resp.status_code
            )
        return _unwrap_tool_result(parse_sse_json(resp.text))


class FakeFollowinClient:
    """Fake 模式（无 API Key）。返回 spike 抓到的真实响应样本。

    必须复刻 route 差异：trending route 无 source_url / source_lang，
    search route 有 —— 手编 mock 编不出这个，而它正是 normalizer 最易错的地方。
    """

    def __init__(self) -> None:
        from pathlib import Path

        base = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"
        self._search = json.loads((base / "followin_media.json").read_text())
        self._trending = json.loads((base / "followin_trending.json").read_text())

    async def fetch_news(self, **arguments: Any) -> dict[str, Any]:
        has_scope = bool(arguments.get("sources") or arguments.get("query"))
        return self._search if has_scope else self._trending
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/bin/pytest tests/news/test_followin_client.py -v`
Expected: 9 passed

- [ ] **Step 6: Commit**

```bash
git add app/news/followin_client.py tests/news/test_followin_client.py tests/fixtures/
git commit -m "feat: Followin MCP 薄客户端（session 自愈 + SSE 解析）"
```

---

### Task 4: 归一化

**Files:**
- Create: `app/news/normalizer.py`、`tests/news/test_normalizer.py`

**Interfaces:**
- Consumes: `app.news.models.NewsItem`、`app.news.tagger.tags_for`
- Produces:
  - `app.news.normalizer.strip_html(raw: str) -> str`
  - `app.news.normalizer.normalize_lang(value: str) -> str`
  - `app.news.normalizer.normalize_title(title: str) -> str`
  - `app.news.normalizer.make_news_id(*, source_url: str, source_name: str, content: str) -> str`
  - `app.news.normalizer.to_news_item(row: dict[str, Any], *, source: str) -> NewsItem | None`
  - `app.news.normalizer.rows_from(payload: dict[str, Any]) -> list[dict[str, Any]]`

- [ ] **Step 1: 写失败测试**

创建 `tests/news/test_normalizer.py`：

```python
"""归一化测试。喂真实 fixtures，覆盖两个 route 的字段差异。"""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path

from app.news.normalizer import (
    make_news_id,
    normalize_lang,
    normalize_title,
    rows_from,
    strip_html,
    to_news_item,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures"


def _payload(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


def test_strip_html_removes_tags() -> None:
    assert strip_html("<p><b>看涨观点</b>：流入加速。</p>") == "看涨观点：流入加速。"


def test_strip_html_unescapes_then_strips_double_encoded() -> None:
    # 先反转义再剥标签：双重编码的 <script> 不能作为文本残留
    assert "<script>" not in strip_html("&lt;script&gt;alert(1)&lt;/script&gt;ok")


def test_strip_html_collapses_whitespace() -> None:
    assert strip_html("a\n\n  b\t c") == "a b c"


def test_normalize_lang_unifies_zh_variants() -> None:
    assert normalize_lang("zh") == "zh-cn"
    assert normalize_lang("ZH-CN") == "zh-cn"
    assert normalize_lang("en") == "en"
    assert normalize_lang("") == ""


def test_normalize_title_folds_case_and_whitespace() -> None:
    assert normalize_title("  BTC   Breaks  71500 ") == "btc breaks 71500"


def test_news_id_prefers_url_and_is_stable() -> None:
    first = make_news_id(source_url="https://a.com/x", source_name="a", content="c1")
    second = make_news_id(source_url="https://a.com/x", source_name="b", content="c2")
    assert first == second  # 有 url 时正文变化不改身份


def test_news_id_falls_back_to_name_and_content_without_url() -> None:
    a = make_news_id(source_url="", source_name="tg", content="hello")
    b = make_news_id(source_url="", source_name="tg", content="world")
    assert a != b and len(a) == 64


def test_rows_from_flattens_both_buckets() -> None:
    rows = rows_from(_payload("followin_media.json"))
    assert len(rows) == 3  # 2 articles + 1 social


def test_search_route_row_maps_fully() -> None:
    rows = rows_from(_payload("followin_media.json"))
    item = to_news_item(rows[0], source="media")
    assert item is not None
    assert item.source_url.startswith("https://cointelegraph.com")
    assert item.source_name == "cointelegraph"
    assert item.lang == "zh-cn"          # 上游给的是 zh
    assert "<p>" not in item.content     # HTML 已剥
    assert item.published_at.tzinfo is UTC
    assert item.raw["content"].startswith("BTC")  # 原始行保留（含原始 HTML）


def test_trending_route_row_tolerates_missing_columns() -> None:
    rows = rows_from(_payload("followin_trending.json"))
    item = to_news_item(rows[0], source="trending")
    assert item is not None
    assert item.source_url == "" and item.source_name == "" and item.lang == ""
    assert "recommend" in item.tags and "crypto" in item.tags


def test_row_without_title_is_dropped() -> None:
    rows = rows_from(_payload("followin_media.json"))
    social = [row for row in rows if not row.get("title")]
    assert social and to_news_item(social[0], source="media") is None


def test_row_without_published_ts_is_dropped() -> None:
    assert to_news_item({"title": "t", "content": "c"}, source="media") is None


def test_dedup_key_matches_normalized_title() -> None:
    item = to_news_item(
        {"title": "  BTC  Breaks 71500 ", "content": "c", "published_ts": 1787190519312},
        source="media",
    )
    assert item is not None and item.dedup_key == "btc breaks 71500"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/pytest tests/news/test_normalizer.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.news.normalizer'`

- [ ] **Step 3: 实现 `app/news/normalizer.py`**

```python
"""上游原始行 → NewsItem。

Followin 的字段是 route-dependent 的：带 query/sources 的 search route 有
source_url / source_lang / source_name，空 query 的 trending route 没有，
但独有 asset_types。所以这里对每一列都按缺失容忍处理，原始行整份留在 raw 里。

title 与 published_ts 是硬要求：没标题渲染不出新闻卡片（social bucket 的
title 实测 100% 为空），没时间戳排不了序也分不了页 —— 这两种行直接丢弃。
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from datetime import UTC, datetime
from typing import Any, Final

from app.news.models import NewsItem
from app.news.tagger import tags_for

logger = logging.getLogger(__name__)

_TAG_RE: Final = re.compile(r"<[^>]+>")
_WS_RE: Final = re.compile(r"\s+")


def strip_html(raw: str) -> str:
    """剥标签并折叠空白。

    先反转义再剥标签：上游正文里混着 <p><b> 这类真标签，也可能出现
    &lt;script&gt; 这类转义标签；先 unescape 能让两者都被同一条规则清掉，
    避免转义过的标记以文本形式残留下来。
    """
    return _WS_RE.sub(" ", _TAG_RE.sub("", html.unescape(raw or ""))).strip()


def normalize_lang(value: str) -> str:
    """上游 zh 与 zh-cn 混用，统一成 zh-cn。"""
    normalized = (value or "").strip().lower()
    return "zh-cn" if normalized == "zh" else normalized


def normalize_title(title: str) -> str:
    """去重键：去首尾空白、折叠内部空白、大小写无关。"""
    return _WS_RE.sub(" ", title or "").strip().casefold()


def make_news_id(*, source_url: str, source_name: str, content: str) -> str:
    """身份优先用 source_url（实测稳定唯一）；trending route 无 url 时退化。"""
    seed = source_url.strip() or f"{source_name.strip()}|{content[:256]}"
    return hashlib.sha256(seed.encode()).hexdigest()


def rows_from(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把 results 下的 articles / social 两个 bucket 摊平成一个列表。"""
    results = payload.get("results") or {}
    rows: list[dict[str, Any]] = []
    for bucket in results.values():
        if isinstance(bucket, list):
            rows.extend(row for row in bucket if isinstance(row, dict))
    return rows


def to_news_item(row: dict[str, Any], *, source: str) -> NewsItem | None:
    """单行归一化。脏行返回 None，由调用方跳过，不拖垮整批。"""
    title = strip_html(str(row.get("title") or ""))
    published_ts = row.get("published_ts")
    if not title or not isinstance(published_ts, (int, float)):
        return None

    content = strip_html(str(row.get("content") or ""))
    source_url = str(row.get("source_url") or "").strip()
    source_name = str(row.get("source_name") or "").strip()
    asset_type = str(row.get("asset_type") or "").strip()
    provenance = str(row.get("provenance") or "").strip()

    return NewsItem(
        news_id=make_news_id(
            source_url=source_url, source_name=source_name, content=content
        ),
        dedup_key=normalize_title(title),
        source=source,
        title=title,
        content=content,
        published_at=datetime.fromtimestamp(published_ts / 1000, UTC),
        source_name=source_name,
        source_url=source_url,
        source_quality=str(row.get("source_quality") or "").strip(),
        lang=normalize_lang(str(row.get("source_lang") or "")),
        asset_type=asset_type,
        upstream_category=str(row.get("category") or "").strip(),
        tags=tags_for(
            title=title, content=content, asset_type=asset_type, provenance=provenance
        ),
        raw=row,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/pytest tests/news/test_normalizer.py -v`
Expected: 14 passed

- [ ] **Step 5: Commit**

```bash
git add app/news/normalizer.py tests/news/test_normalizer.py
git commit -m "feat: 新闻归一化（容忍 route-dependent 缺列 + HTML 剥离）"
```

---

### Task 5: 存储层

**Files:**
- Create: `app/news/store.py`、`app/news/pg_store.py`、`tests/news/test_news_store.py`

**Interfaces:**
- Consumes: `app.news.models.NewsItem`、`NewsQuery`、`NewsTag`
- Produces:
  - `app.news.store.NewsStore`（Protocol）：`upsert_many(items: Sequence[NewsItem]) -> int`、`query(q: NewsQuery) -> tuple[list[NewsItem], str | None]`、`purge_before(cutoff: datetime) -> int`
  - `app.news.store.InMemoryNewsStore`（同接口）
  - `app.news.store.encode_cursor(published_at: datetime, news_id: str) -> str`
  - `app.news.store.decode_cursor(cursor: str) -> tuple[datetime, str]`（非法抛 `ValueError`）
  - `app.news.pg_store.PostgresNewsStore(engine: AsyncEngine)` + `create_schema() -> None`
  - `app.news.pg_store.news_table`、`news_tags_table`、`metadata`

- [ ] **Step 1: 写失败测试**

创建 `tests/news/test_news_store.py`：

```python
"""存储层测试。PG 实现走 sqlite+aiosqlite（照 tests/test_stores.py 的范式）。

dedup_key 合并是本层最容易写错的地方：trending route 无 source_url，
news_id 只能用内容摘要，同一条新闻会与 media route 的行撞出两个 id。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.news.models import NewsItem, NewsQuery
from app.news.store import decode_cursor, encode_cursor

_BASE = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _item(news_id: str, *, minutes: int = 0, title: str = "t", **kwargs) -> NewsItem:
    defaults = {
        "news_id": news_id,
        "dedup_key": title.casefold(),
        "source": "media",
        "title": title,
        "content": "正文",
        "published_at": _BASE + timedelta(minutes=minutes),
    }
    return NewsItem(**{**defaults, **kwargs})


async def _pg_store():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.news.pg_store import PostgresNewsStore

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    store = PostgresNewsStore(engine)
    await store.create_schema()
    return store


@pytest.fixture(params=["memory", "pg"])
async def store(request):
    if request.param == "memory":
        from app.news.store import InMemoryNewsStore

        return InMemoryNewsStore()
    return await _pg_store()


def test_cursor_round_trips() -> None:
    encoded = encode_cursor(_BASE, "abc")
    when, news_id = decode_cursor(encoded)
    assert when == _BASE and news_id == "abc"


def test_decode_cursor_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        decode_cursor("not-a-cursor")


async def test_upsert_then_query_returns_item(store) -> None:
    assert await store.upsert_many([_item("a", tags=("crypto",))]) == 1
    items, cursor = await store.query(NewsQuery(limit=10))
    assert [item.news_id for item in items] == ["a"]
    assert cursor is None


async def test_same_news_id_is_not_duplicated(store) -> None:
    await store.upsert_many([_item("a")])
    await store.upsert_many([_item("a")])
    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 1


async def test_dedup_key_merges_tags_instead_of_inserting_row(store) -> None:
    # media route 先入库（有 url，id=media-1），trending route 后到（无 url，id=trend-1）
    await store.upsert_many([_item("media-1", title="BTC breaks", tags=("crypto",))])
    await store.upsert_many([_item("trend-1", title="BTC breaks", tags=("recommend",))])
    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 1, "同 dedup_key 不该新建行"
    assert set(items[0].tags) == {"crypto", "recommend"}
    assert items[0].news_id == "media-1", "应合并到先入库的那行"


async def test_query_filters_by_tag(store) -> None:
    await store.upsert_many([
        _item("a", title="a", tags=("crypto",)),
        _item("b", title="b", tags=("macro",)),
    ])
    items, _ = await store.query(NewsQuery(tag="macro", limit=10))
    assert [item.news_id for item in items] == ["b"]


async def test_query_filters_by_keyword_case_insensitively(store) -> None:
    await store.upsert_many([
        _item("a", title="Bitcoin ETF inflow"),
        _item("b", title="英伟达发布新芯片"),
    ])
    items, _ = await store.query(NewsQuery(q="bitcoin", limit=10))
    assert [item.news_id for item in items] == ["a"]
    items, _ = await store.query(NewsQuery(q="芯片", limit=10))
    assert [item.news_id for item in items] == ["b"]


async def test_keyword_wildcards_are_escaped_not_interpreted(store) -> None:
    await store.upsert_many([_item("a", title="plain title")])
    # "%" 若未转义会变成通配符、把所有行都捞出来
    items, _ = await store.query(NewsQuery(q="%", limit=10))
    assert items == []


async def test_query_filters_by_lang(store) -> None:
    await store.upsert_many([
        _item("a", title="a", lang="zh-cn"),
        _item("b", title="b", lang="en"),
    ])
    items, _ = await store.query(NewsQuery(lang="en", limit=10))
    assert [item.news_id for item in items] == ["b"]


async def test_query_respects_since_window(store) -> None:
    await store.upsert_many([
        _item("old", minutes=0, title="old"),
        _item("new", minutes=60, title="new"),
    ])
    items, _ = await store.query(NewsQuery(since=_BASE + timedelta(minutes=30), limit=10))
    assert [item.news_id for item in items] == ["new"]


async def test_query_orders_newest_first(store) -> None:
    await store.upsert_many([
        _item("a", minutes=0, title="a"),
        _item("b", minutes=10, title="b"),
        _item("c", minutes=20, title="c"),
    ])
    items, _ = await store.query(NewsQuery(limit=10))
    assert [item.news_id for item in items] == ["c", "b", "a"]


async def test_cursor_pagination_has_no_gap_or_overlap(store) -> None:
    await store.upsert_many([_item(f"n{i}", minutes=i, title=f"t{i}") for i in range(5)])
    first, cursor = await store.query(NewsQuery(limit=2))
    assert cursor is not None
    second, cursor2 = await store.query(NewsQuery(limit=2, cursor=cursor))
    third, cursor3 = await store.query(NewsQuery(limit=2, cursor=cursor2))
    seen = [item.news_id for item in (*first, *second, *third)]
    assert seen == ["n4", "n3", "n2", "n1", "n0"]
    assert cursor3 is None


async def test_purge_before_removes_old_rows_and_their_tags(store) -> None:
    await store.upsert_many([
        _item("old", minutes=0, title="old", tags=("crypto",)),
        _item("new", minutes=60, title="new", tags=("macro",)),
    ])
    assert await store.purge_before(_BASE + timedelta(minutes=30)) == 1
    items, _ = await store.query(NewsQuery(limit=10))
    assert [item.news_id for item in items] == ["new"]
    # 标签过滤不该还能捞到已删的行（关联表须一并清掉）
    stale, _ = await store.query(NewsQuery(tag="crypto", limit=10))
    assert stale == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/pytest tests/news/test_news_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.news.store'`

- [ ] **Step 3: 实现 `app/news/store.py`**

```python
"""NewsStore 协议、游标编解码与进程内实现（Fake / 测试用）。"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from app.news.models import NewsItem, NewsQuery


def encode_cursor(published_at: datetime, news_id: str) -> str:
    """游标 = base64(ISO 时间|news_id)。不透明化，避免调用方拿它当 offset 用。"""
    raw = f"{published_at.isoformat()}|{news_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """解游标。非法一律抛 ValueError，由路由层转 400。"""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("游标格式不合法") from exc
    when, _, news_id = raw.partition("|")
    if not news_id:
        raise ValueError("游标缺少 news_id")
    return datetime.fromisoformat(when), news_id


class NewsStore(Protocol):
    async def upsert_many(self, items: Sequence[NewsItem]) -> int: ...

    async def query(self, q: NewsQuery) -> tuple[list[NewsItem], str | None]: ...

    async def purge_before(self, cutoff: datetime) -> int: ...


def _matches(item: NewsItem, q: NewsQuery) -> bool:
    if q.tag is not None and q.tag not in item.tags:
        return False
    if q.lang and item.lang != q.lang:
        return False
    if q.since is not None and item.published_at < q.since:
        return False
    if q.q:
        needle = q.q.casefold()
        if needle not in item.title.casefold() and needle not in item.content.casefold():
            return False
    return True


class InMemoryNewsStore:
    """进程内实现。Fake 模式与单测用；生产一律 PostgresNewsStore。"""

    def __init__(self) -> None:
        self._items: dict[str, NewsItem] = {}

    def _find_by_dedup_key(self, dedup_key: str) -> NewsItem | None:
        return next(
            (item for item in self._items.values() if item.dedup_key == dedup_key), None
        )

    async def upsert_many(self, items: Sequence[NewsItem]) -> int:
        written = 0
        for item in items:
            existing = self._items.get(item.news_id) or self._find_by_dedup_key(item.dedup_key)
            if existing is not None:
                merged = tuple(dict.fromkeys((*existing.tags, *item.tags)))
                # 整体换新，不原地改
                self._items = {
                    **self._items,
                    existing.news_id: existing.model_copy(update={"tags": merged}),
                }
                continue
            self._items = {**self._items, item.news_id: item}
            written += 1
        return written

    async def query(self, q: NewsQuery) -> tuple[list[NewsItem], str | None]:
        rows = sorted(
            (item for item in self._items.values() if _matches(item, q)),
            key=lambda item: (item.published_at, item.news_id),
            reverse=True,
        )
        if q.cursor:
            when, news_id = decode_cursor(q.cursor)
            rows = [
                item for item in rows
                if (item.published_at, item.news_id) < (when, news_id)
            ]
        page = rows[: q.limit]
        next_cursor = (
            encode_cursor(page[-1].published_at, page[-1].news_id)
            if len(rows) > q.limit and page
            else None
        )
        return page, next_cursor

    async def purge_before(self, cutoff: datetime) -> int:
        kept = {
            news_id: item
            for news_id, item in self._items.items()
            if item.published_at >= cutoff
        }
        removed = len(self._items) - len(kept)
        self._items = kept
        return removed
```

- [ ] **Step 4: 实现 `app/news/pg_store.py`**

```python
"""PostgreSQL 新闻存储（SQLAlchemy 2.x async Core）。

tags 走关联表而非 JSONB 数组：仓库的 PG store 测试全部基于
sqlite+aiosqlite，用 `tags @> '["crypto"]'` 这种 PG 专有语法会让核心过滤
逻辑脱离测试范式。关联表下过滤是普通 JOIN + WHERE，两边都能跑。

pg_trgm 索引与扩展只在 PostgreSQL 上建（sqlite 不认 gin_trgm_ops），
所以 create_schema 里按 dialect 分支。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.news.models import NewsItem, NewsQuery, NewsTag
from app.news.store import decode_cursor, encode_cursor

logger = logging.getLogger(__name__)

metadata = sa.MetaData()

news_table = sa.Table(
    "news",
    metadata,
    sa.Column("news_id", sa.String(64), primary_key=True),
    sa.Column("dedup_key", sa.Text, nullable=False),
    sa.Column("source", sa.String(32), nullable=False),
    sa.Column("source_name", sa.String(128), nullable=False, server_default=""),
    sa.Column("source_url", sa.Text, nullable=False, server_default=""),
    sa.Column("source_quality", sa.String(32), nullable=False, server_default=""),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("lang", sa.String(16), nullable=False, server_default=""),
    sa.Column("asset_type", sa.String(16), nullable=False, server_default=""),
    sa.Column("upstream_category", sa.String(64), nullable=False, server_default=""),
    sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("raw", sa.JSON, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_news_published", "published_at", "news_id"),
    sa.Index("ix_news_dedup", "dedup_key", "published_at"),
)

news_tags_table = sa.Table(
    "news_tags",
    metadata,
    sa.Column("news_id", sa.String(64), primary_key=True),
    sa.Column("tag", sa.String(32), primary_key=True),
    sa.Index("ix_news_tags_tag", "tag", "news_id"),
)

# ILIKE 里 % 与 _ 是通配符，用户输入必须转义，否则 q=% 会捞出全表
_LIKE_ESCAPE = "\\"


def _escape_like(value: str) -> str:
    escaped = value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
    for wildcard in ("%", "_"):
        escaped = escaped.replace(wildcard, f"{_LIKE_ESCAPE}{wildcard}")
    return escaped


def ensure_utc(value: datetime) -> datetime:
    """回读时间统一成带时区。

    PG 驱动给 tz-aware，SQLite 驱动给 naive；不归一化后续比较直接抛
    TypeError（与 app/strategy/equity_pg_store.py 同一个坑）。
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _row_to_item(row: Any, tags: tuple[NewsTag, ...]) -> NewsItem:
    raw = row.raw
    if isinstance(raw, str):  # SQLite 把 JSON 列回读成字符串
        raw = json.loads(raw)
    return NewsItem(
        news_id=row.news_id,
        dedup_key=row.dedup_key,
        source=row.source,
        title=row.title,
        content=row.content,
        published_at=ensure_utc(row.published_at),
        source_name=row.source_name,
        source_url=row.source_url,
        source_quality=row.source_quality,
        lang=row.lang,
        asset_type=row.asset_type,
        upstream_category=row.upstream_category,
        tags=tags,
        raw=raw or {},
    )


class PostgresNewsStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_schema(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            if conn.dialect.name != "postgresql":
                return
            # 关键词检索靠 trgm 索引。注意 2 字中文关键词提取不出完整 trigram，
            # 仍会退化为顺序扫描 —— 由 /news 的默认时间窗兜住扫描范围。
            await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            for column in ("title", "content"):
                await conn.execute(
                    sa.text(
                        f"CREATE INDEX IF NOT EXISTS ix_news_{column}_trgm "
                        f"ON news USING gin ({column} gin_trgm_ops)"
                    )
                )

    async def upsert_many(self, items: Sequence[NewsItem]) -> int:
        """写入并合并跨 route 重复。

        采集由 RoundLock 保证单写者，所以 SELECT-then-INSERT 是安全的，
        也就不必用 PG 专有的 on_conflict_do_nothing（那会破坏 sqlite 测试）。
        """
        if not items:
            return 0
        written = 0
        now = datetime.now(UTC)
        async with self._engine.begin() as conn:
            for item in items:
                target_id = await self._resolve_target(conn, item)
                if target_id is None:
                    await conn.execute(
                        news_table.insert().values(
                            news_id=item.news_id,
                            dedup_key=item.dedup_key,
                            source=item.source,
                            source_name=item.source_name,
                            source_url=item.source_url,
                            source_quality=item.source_quality,
                            title=item.title,
                            content=item.content,
                            lang=item.lang,
                            asset_type=item.asset_type,
                            upstream_category=item.upstream_category,
                            published_at=item.published_at,
                            raw=item.raw,
                            created_at=now,
                        )
                    )
                    target_id = item.news_id
                    written += 1
                await self._merge_tags(conn, target_id, item.tags)
        return written

    async def _resolve_target(self, conn: Any, item: NewsItem) -> str | None:
        """返回已存在的目标行 id；None 表示需要新建。"""
        by_id = await conn.execute(
            sa.select(news_table.c.news_id).where(news_table.c.news_id == item.news_id)
        )
        existing = by_id.scalar_one_or_none()
        if existing is not None:
            return existing
        by_key = await conn.execute(
            sa.select(news_table.c.news_id)
            .where(news_table.c.dedup_key == item.dedup_key)
            .order_by(news_table.c.published_at.asc())
            .limit(1)
        )
        return by_key.scalar_one_or_none()

    async def _merge_tags(
        self, conn: Any, news_id: str, tags: tuple[NewsTag, ...]
    ) -> None:
        if not tags:
            return
        existing = await conn.execute(
            sa.select(news_tags_table.c.tag).where(news_tags_table.c.news_id == news_id)
        )
        already = {row[0] for row in existing}
        fresh = [{"news_id": news_id, "tag": tag} for tag in tags if tag not in already]
        if fresh:
            await conn.execute(news_tags_table.insert(), fresh)

    async def query(self, q: NewsQuery) -> tuple[list[NewsItem], str | None]:
        stmt = sa.select(news_table)
        if q.tag is not None:
            stmt = stmt.join(
                news_tags_table,
                sa.and_(
                    news_tags_table.c.news_id == news_table.c.news_id,
                    news_tags_table.c.tag == q.tag,
                ),
            )
        if q.lang:
            stmt = stmt.where(news_table.c.lang == q.lang)
        if q.since is not None:
            stmt = stmt.where(news_table.c.published_at >= q.since)
        if q.q:
            pattern = f"%{_escape_like(q.q)}%"
            stmt = stmt.where(
                sa.or_(
                    news_table.c.title.ilike(pattern, escape=_LIKE_ESCAPE),
                    news_table.c.content.ilike(pattern, escape=_LIKE_ESCAPE),
                )
            )
        if q.cursor:
            when, news_id = decode_cursor(q.cursor)
            # 写成 OR 形式而不是行值比较：行值比较在各方言下支持度不一
            stmt = stmt.where(
                sa.or_(
                    news_table.c.published_at < when,
                    sa.and_(
                        news_table.c.published_at == when,
                        news_table.c.news_id < news_id,
                    ),
                )
            )
        stmt = stmt.order_by(
            news_table.c.published_at.desc(), news_table.c.news_id.desc()
        ).limit(q.limit + 1)  # 多取一条用于判断还有没有下一页

        async with self._engine.begin() as conn:
            rows = list((await conn.execute(stmt)).all())
            page_rows = rows[: q.limit]
            tag_map = await self._tags_for(conn, [row.news_id for row in page_rows])

        items = [_row_to_item(row, tag_map.get(row.news_id, ())) for row in page_rows]
        next_cursor = (
            encode_cursor(items[-1].published_at, items[-1].news_id)
            if len(rows) > q.limit and items
            else None
        )
        return items, next_cursor

    async def _tags_for(
        self, conn: Any, news_ids: Sequence[str]
    ) -> dict[str, tuple[NewsTag, ...]]:
        if not news_ids:
            return {}
        result = await conn.execute(
            sa.select(news_tags_table.c.news_id, news_tags_table.c.tag)
            .where(news_tags_table.c.news_id.in_(list(news_ids)))
            .order_by(news_tags_table.c.tag)
        )
        grouped: dict[str, tuple[NewsTag, ...]] = {}
        for news_id, tag in result:
            grouped[news_id] = (*grouped.get(news_id, ()), tag)
        return grouped

    async def purge_before(self, cutoff: datetime) -> int:
        async with self._engine.begin() as conn:
            doomed = await conn.execute(
                sa.select(news_table.c.news_id).where(news_table.c.published_at < cutoff)
            )
            ids = [row[0] for row in doomed]
            if not ids:
                return 0
            # 先删关联表：留下孤儿 tag 行会让 tag 过滤 JOIN 到已删的新闻
            await conn.execute(
                news_tags_table.delete().where(news_tags_table.c.news_id.in_(ids))
            )
            await conn.execute(news_table.delete().where(news_table.c.news_id.in_(ids)))
        return len(ids)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/bin/pytest tests/news/test_news_store.py -v`
Expected: 26 passed（13 个用例 × memory/pg 两种 store，加 2 个游标纯函数用例）

- [ ] **Step 6: Commit**

```bash
git add app/news/store.py app/news/pg_store.py tests/news/test_news_store.py
git commit -m "feat: 新闻存储层（tags 关联表 + dedup 合并 + 游标分页）"
```

---

### Task 6: 采集器

**Files:**
- Create: `app/news/collector.py`、`tests/news/test_collector.py`

**Interfaces:**
- Consumes: `FollowinClient.fetch_news`、`NewsStore`、`app.news.normalizer.rows_from`/`to_news_item`、`app.services.round_lock.RoundLock`/`SingleProcessRoundLock`/`lock_ttl_for`
- Produces:
  - `app.news.collector.NewsCollector(client, store, *, limit: int = 50, round_lock: RoundLock | None = None, retention_days: int = 90)`
  - `NewsCollector.refresh_once() -> int`（返回本轮新增条数）
  - `NewsCollector.warmup() -> int`
  - `NewsCollector.run_forever(interval_seconds: float) -> None`
  - `app.news.collector.ROUTES: tuple[tuple[str, dict[str, Any]], ...]`
  - `app.news.collector.WARMUP_QUERIES: tuple[str, ...]`

- [ ] **Step 1: 写失败测试**

创建 `tests/news/test_collector.py`：

```python
"""采集器测试。重点：单源失败隔离、RoundLock、CancelledError 必须传播。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.news.collector import ROUTES, NewsCollector
from app.news.followin_client import FollowinUpstreamError
from app.news.models import NewsQuery
from app.news.store import InMemoryNewsStore

_FIXTURES = Path(__file__).parent.parent / "fixtures"


def _payload(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


class _StubClient:
    """按调用序号返回预设结果；异常项直接抛。"""

    def __init__(self, *results) -> None:
        self._results = list(results)
        self.calls: list[dict] = []

    async def fetch_news(self, **arguments):
        self.calls.append(arguments)
        outcome = self._results.pop(0) if self._results else {}
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _DenyLock:
    async def acquire(self, ttl_seconds: int) -> bool:
        return False


def test_routes_cover_media_research_and_trending() -> None:
    names = [name for name, _ in ROUTES]
    assert names == ["media", "research", "trending"]
    # 上游 limit 上限是 50，别写更大
    assert all(args.get("limit", 50) <= 50 for _, args in ROUTES)


async def test_refresh_once_writes_all_routes() -> None:
    client = _StubClient(
        _payload("followin_media.json"),
        _payload("followin_media.json"),
        _payload("followin_trending.json"),
    )
    store = InMemoryNewsStore()
    written = await NewsCollector(client, store, limit=50).refresh_once()
    assert written > 0
    assert len(client.calls) == 3


async def test_one_failing_route_does_not_block_the_others() -> None:
    client = _StubClient(
        FollowinUpstreamError("media 挂了", status_code=502),
        _payload("followin_media.json"),
        _payload("followin_trending.json"),
    )
    store = InMemoryNewsStore()
    written = await NewsCollector(client, store, limit=50).refresh_once()
    assert written > 0, "media 失败不该让 research / trending 也停更"
    assert len(client.calls) == 3


async def test_social_rows_without_title_are_skipped() -> None:
    client = _StubClient(_payload("followin_media.json"), {}, {})
    store = InMemoryNewsStore()
    await NewsCollector(client, store, limit=50).refresh_once()
    items, _ = await store.query(NewsQuery(limit=50))
    # fixture 有 2 条 articles + 1 条无 title 的 social
    assert len(items) == 2


async def test_trending_rows_get_recommend_tag() -> None:
    client = _StubClient({}, {}, _payload("followin_trending.json"))
    store = InMemoryNewsStore()
    await NewsCollector(client, store, limit=50).refresh_once()
    items, _ = await store.query(NewsQuery(tag="recommend", limit=50))
    assert len(items) == 2


async def test_round_lock_denied_skips_fetching() -> None:
    client = _StubClient(_payload("followin_media.json"))
    collector = NewsCollector(
        client, InMemoryNewsStore(), limit=50, round_lock=_DenyLock()
    )
    task = asyncio.create_task(collector.run_forever(0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.calls == [], "未拿到轮次锁就不该发起抓取"


async def test_run_forever_propagates_cancellation() -> None:
    collector = NewsCollector(_StubClient({}, {}, {}), InMemoryNewsStore(), limit=50)
    task = asyncio.create_task(collector.run_forever(0.01))
    await asyncio.sleep(0.03)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_warmup_issues_query_scoped_calls() -> None:
    client = _StubClient(*[_payload("followin_media.json")] * 8)
    await NewsCollector(client, InMemoryNewsStore(), limit=50).warmup()
    assert client.calls, "预热应至少发一次调用"
    assert all("query" in call for call in client.calls), "预热必须走带 query 的 search route"


async def test_retention_purges_rows_older_than_window() -> None:
    from datetime import UTC, datetime, timedelta

    from app.news.models import NewsItem

    store = InMemoryNewsStore()
    stale = datetime.now(UTC) - timedelta(days=120)
    await store.upsert_many([
        NewsItem(
            news_id="old", dedup_key="old", source="media", title="old",
            content="c", published_at=stale,
        )
    ])
    await NewsCollector(
        _StubClient({}, {}, {}), store, limit=50, retention_days=90
    ).refresh_once()
    items, _ = await store.query(NewsQuery(limit=50))
    assert [item.news_id for item in items] == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/pytest tests/news/test_collector.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.news.collector'`

- [ ] **Step 3: 实现 `app/news/collector.py`**

```python
"""新闻采集循环。

上游没有分页（limit 上限 50，实测 media 源 2 条/分 → 50 条约 25 分钟耗尽），
所以间隔必须远小于 25 分钟；默认 300s 留 5x 余量。同理**历史无法回填** ——
首轮只能拿到最近约 25 分钟的内容，靠 warmup() 的带 query 检索补一点纵深。

单个 route 失败只跳过它自己：一个源挂了不该让其余源的数据跟着停更
（沿用原 grok_monitor.refresh_once 的做法，那个模式本身是对的）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

from app.news.models import NewsItem
from app.news.normalizer import rows_from, to_news_item
from app.news.store import NewsStore
from app.services.round_lock import RoundLock, SingleProcessRoundLock, lock_ttl_for

logger = logging.getLogger(__name__)

# 三条 route。trending 走空 query（上游据此返回它自己的热榜，供 recommend tag）。
ROUTES: Final[tuple[tuple[str, dict[str, Any]], ...]] = (
    ("media", {"sources": ["media"]}),
    ("research", {"sources": ["research"]}),
    ("trending", {}),
)

# 冷启动预热用的检索词。search route 有 relevance 排序，能捞到比时间窗更早的内容。
WARMUP_QUERIES: Final[tuple[str, ...]] = (
    "Fed CPI interest rate",
    "AI chips NVDA OpenAI",
    "bitcoin ethereum ETF",
    "US stocks earnings guidance",
)


class _NewsFetcher(Protocol):
    async def fetch_news(self, **arguments: Any) -> dict[str, Any]: ...


class NewsCollector:
    def __init__(
        self,
        client: _NewsFetcher,
        store: NewsStore,
        *,
        limit: int = 50,
        round_lock: RoundLock | None = None,
        retention_days: int = 90,
    ) -> None:
        self.client = client
        self.store = store
        self.limit = limit
        self.round_lock: RoundLock = round_lock or SingleProcessRoundLock()
        self.retention_days = retention_days

    async def _collect(self, source: str, arguments: dict[str, Any]) -> int:
        payload = await self.client.fetch_news(**{**arguments, "limit": self.limit})
        items: list[NewsItem] = []
        for row in rows_from(payload):
            item = to_news_item(row, source=source)
            if item is not None:
                items.append(item)
        written = await self.store.upsert_many(items)
        logger.debug("news 采集 route=%s 解析=%d 新增=%d", source, len(items), written)
        return written

    async def refresh_once(self) -> int:
        """跑一轮全部 route，返回新增条数。单 route 失败只跳过它自己。"""
        written = 0
        for source, arguments in ROUTES:
            try:
                written += await self._collect(source, arguments)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("news 采集 route=%s 本轮失败，继续下一个", source, exc_info=True)
        await self._purge_expired()
        logger.info("news 采集本轮新增 %d 条", written)
        return written

    async def _purge_expired(self) -> None:
        if self.retention_days <= 0:
            return
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        try:
            removed = await self.store.purge_before(cutoff)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("news 过期清理失败，等待下一轮", exc_info=True)
            return
        if removed:
            logger.info("news 清理过期数据 %d 条（保留 %d 天）", removed, self.retention_days)

    async def warmup(self) -> int:
        """一次性冷启动预热。上游无历史回填，这里靠 relevance 排序多捞一点。"""
        written = 0
        for query in WARMUP_QUERIES:
            for source_lang in ("zh-cn", "en"):
                try:
                    written += await self._collect(
                        "media", {"query": query, "source_lang": source_lang}
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("news 预热 query=%s 失败，跳过", query, exc_info=True)
        logger.info("news 冷启动预热新增 %d 条", written)
        return written

    async def run_forever(self, interval_seconds: float) -> None:
        """常驻循环。单轮失败只记日志，不中断后续轮次。"""
        ttl = lock_ttl_for(interval_seconds)
        logger.info(
            "news 采集已启动，间隔 %.0fs，每轮 %d 次上游调用",
            interval_seconds,
            len(ROUTES),
        )
        while True:
            try:
                if await self.round_lock.acquire(ttl):
                    await self.refresh_once()
                else:
                    logger.debug("news 采集本轮锁被其他实例持有，跳过")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("news 采集单轮失败，等待下一轮", exc_info=True)
            await asyncio.sleep(interval_seconds)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/pytest tests/news/test_collector.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add app/news/collector.py tests/news/test_collector.py
git commit -m "feat: 新闻采集循环（三 route + 单源失败隔离 + 保留期清理）"
```

---

### Task 7: 查询接口与容器装配

**Files:**
- Create: `app/api/news.py`、`tests/api/test_news.py`
- Modify: `app/container.py`、`app/main.py`

**Interfaces:**
- Consumes: 前六个 Task 的全部产出、`app.api.deps.get_container`、`app.api.errors.error_response`、`app.services.rate_limit.RateLimiter`
- Produces:
  - `app.api.news.router`（`APIRouter`，由 `main.py` 以 `prefix="/v1/agent"` 挂载 → `GET /v1/agent/news`）
  - `Container.news_store: NewsStore | None`、`Container.news_collector: NewsCollector | None`、`Container.news_collector_task: asyncio.Task[None] | None`

- [ ] **Step 1: 写失败测试**

创建 `tests/api/test_news.py`：

```python
"""GET /v1/agent/news 接口测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_container
from app.api.news import router
from app.news.models import NewsItem
from app.news.store import InMemoryNewsStore

_NOW = datetime.now(UTC)


class _Settings:
    news_default_time_range_days = 30
    news_query_rate_limit = 60
    news_query_rate_window_seconds = 60


class _Ctn:
    def __init__(self, store, rate_limiter=None) -> None:
        self.settings = _Settings()
        self.news_store = store
        self.rate_limiter = rate_limiter


def _item(news_id: str, *, minutes: int = 0, title: str = "t", **kwargs) -> NewsItem:
    defaults = {
        "news_id": news_id,
        "dedup_key": title.casefold(),
        "source": "media",
        "title": title,
        "content": "正文",
        "published_at": _NOW - timedelta(minutes=minutes),
    }
    return NewsItem(**{**defaults, **kwargs})


@pytest.fixture
async def client_and_store():
    store = InMemoryNewsStore()
    app = FastAPI()
    app.include_router(router, prefix="/v1/agent")
    app.dependency_overrides[get_container] = lambda: _Ctn(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, store


async def test_returns_items_newest_first(client_and_store) -> None:
    client, store = client_and_store
    await store.upsert_many([
        _item("a", minutes=30, title="a"), _item("b", minutes=1, title="b")
    ])
    body = (await client.get("/v1/agent/news")).json()
    assert [item["news_id"] for item in body["items"]] == ["b", "a"]
    assert body["next_cursor"] is None


async def test_response_never_leaks_raw(client_and_store) -> None:
    client, store = client_and_store
    await store.upsert_many([_item("a", raw={"secret": "leak", "content": "<b>x</b>"})])
    body = (await client.get("/v1/agent/news")).json()
    assert "raw" not in body["items"][0]
    assert "leak" not in str(body)


async def test_filters_by_tag(client_and_store) -> None:
    client, store = client_and_store
    await store.upsert_many([
        _item("a", title="a", tags=("crypto",)), _item("b", title="b", tags=("macro",))
    ])
    body = (await client.get("/v1/agent/news?tag=macro")).json()
    assert [item["news_id"] for item in body["items"]] == ["b"]


async def test_unknown_tag_returns_400(client_and_store) -> None:
    client, _ = client_and_store
    resp = await client.get("/v1/agent/news?tag=nonsense")
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_REQUEST"


async def test_keyword_search(client_and_store) -> None:
    client, store = client_and_store
    await store.upsert_many([
        _item("a", title="Bitcoin ETF"), _item("b", title="英伟达芯片")
    ])
    body = (await client.get("/v1/agent/news?q=芯片")).json()
    assert [item["news_id"] for item in body["items"]] == ["b"]


async def test_default_time_window_excludes_ancient_rows(client_and_store) -> None:
    client, store = client_and_store
    await store.upsert_many([
        _item("ancient", minutes=60 * 24 * 400, title="ancient"),
        _item("fresh", minutes=5, title="fresh"),
    ])
    body = (await client.get("/v1/agent/news")).json()
    assert [item["news_id"] for item in body["items"]] == ["fresh"]


async def test_cursor_pagination(client_and_store) -> None:
    client, store = client_and_store
    await store.upsert_many([_item(f"n{i}", minutes=i, title=f"t{i}") for i in range(5)])
    first = (await client.get("/v1/agent/news?limit=2")).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = (
        await client.get(f"/v1/agent/news?limit=2&cursor={first['next_cursor']}")
    ).json()
    ids = [item["news_id"] for item in (*first["items"], *second["items"])]
    assert ids == ["n0", "n1", "n2", "n3"]


async def test_bad_cursor_returns_400(client_and_store) -> None:
    client, _ = client_and_store
    resp = await client.get("/v1/agent/news?cursor=!!!not-base64!!!")
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_REQUEST"


async def test_limit_out_of_range_returns_422(client_and_store) -> None:
    client, _ = client_and_store
    assert (await client.get("/v1/agent/news?limit=999")).status_code == 422
    assert (await client.get("/v1/agent/news?limit=0")).status_code == 422


async def test_returns_503_when_store_unconfigured() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/v1/agent")
    app.dependency_overrides[get_container] = lambda: _Ctn(None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/agent/news")
    assert resp.status_code == 503


async def test_rate_limited_returns_429() -> None:
    class _AlwaysDeny:
        async def allow(self, key: str, limit: int, window_seconds: int) -> bool:
            return False

    app = FastAPI()
    app.include_router(router, prefix="/v1/agent")
    app.dependency_overrides[get_container] = lambda: _Ctn(
        InMemoryNewsStore(), _AlwaysDeny()
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/agent/news")
    assert resp.status_code == 429
    assert resp.json()["code"] == "RATE_LIMITED"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/pytest tests/api/test_news.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.api.news'`

- [ ] **Step 3: 实现 `app/api/news.py`**

```python
"""新闻检索 API。

只读本地 news 表，不透传上游 —— 上游 limit 上限 50、无分页，直接代理
给不了下拉加载。数据由 app/news/collector.py 的后台循环持续写入。

默认时间窗是性能护栏而非产品选择：2 字中文关键词提取不出完整 trigram，
pg_trgm 索引用不上、退化为顺序扫描，必须钉住扫描范围。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, get_args

from fastapi import APIRouter, Depends, Query, Request

from app.api import schemas
from app.api.deps import get_container
from app.api.errors import error_response
from app.container import Container
from app.news.models import NewsCard, NewsPage, NewsQuery, NewsTag

router = APIRouter(tags=["news"])

_ERR: dict[str, Any] = {"model": schemas.ErrorEnvelope}
Ctn = Annotated[Container, Depends(get_container)]
_ALLOWED_TAGS: tuple[str, ...] = get_args(NewsTag)


def _caller_id(request: Request) -> str:
    """限流身份。有 X-User-Id 用它，否则退化到对端 IP。"""
    user_id = request.headers.get("X-User-Id")
    if user_id and user_id.strip():
        return f"user:{user_id.strip()}"
    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


@router.get(
    "/news",
    response_model=NewsPage,
    summary="新闻检索（tag 过滤 / 关键词搜索 / 游标分页）",
    responses={400: _ERR, 429: _ERR, 503: _ERR},
)
async def list_news(
    container: Ctn,
    request: Request,
    tag: str = Query(default="", description=f"标签过滤，单值。可选：{'、'.join(_ALLOWED_TAGS)}"),
    q: str = Query(default="", max_length=100, description="关键词，匹配标题与正文"),
    lang: str = Query(default="", description="语言过滤：zh-cn | en"),
    days: int = Query(
        default=0, ge=0, le=365, description="时间窗（天）。0 = 用服务端默认（30 天）"
    ),
    limit: int = Query(default=20, ge=1, le=50, description="单页条数"),
    cursor: str = Query(default="", description="上一页返回的 next_cursor"),
):
    """按标签与关键词检索新闻，按发布时间倒序、游标分页。"""
    store = container.news_store
    if store is None:
        return error_response("INTERNAL", "新闻服务未配置或不可用", 503)

    if tag and tag not in _ALLOWED_TAGS:
        return error_response(
            "INVALID_REQUEST",
            f"标签 '{tag}' 不可用。可选：{'、'.join(_ALLOWED_TAGS)}",
            400,
            details={"tag": tag, "allowed": list(_ALLOWED_TAGS)},
        )

    limiter = container.rate_limiter
    if limiter is not None:
        window = container.settings.news_query_rate_window_seconds
        quota = container.settings.news_query_rate_limit
        if quota > 0 and not await limiter.allow(
            f"news:query:{_caller_id(request)}", quota, window
        ):
            return error_response(
                "RATE_LIMITED",
                f"新闻检索已达配额（{quota} 次 / {window}s），请稍后重试",
                429,
                details={"limit": quota, "window_seconds": window},
            )

    span = days or container.settings.news_default_time_range_days
    query = NewsQuery(
        tag=tag or None,  # type: ignore[arg-type]
        q=q.strip(),
        lang=lang.strip().lower(),
        since=datetime.now(UTC) - timedelta(days=span),
        limit=limit,
        cursor=cursor or None,
    )
    try:
        items, next_cursor = await store.query(query)
    except ValueError as exc:
        # 只有游标解析会抛 ValueError
        return error_response("INVALID_REQUEST", f"游标不合法：{exc}", 400)

    return NewsPage(items=[NewsCard.of(item) for item in items], next_cursor=next_cursor)
```

- [ ] **Step 4: 运行接口测试确认通过**

Run: `.venv/bin/pytest tests/api/test_news.py -v`
Expected: 11 passed

- [ ] **Step 5: 容器装配**

在 `app/container.py` 顶部加 import：

```python
from app.news.collector import NewsCollector
from app.news.followin_client import FakeFollowinClient, FollowinClient
from app.news.pg_store import PostgresNewsStore
from app.news.store import InMemoryNewsStore, NewsStore
```

`Container` 数据类里，在原 grok 三个字段的位置加：

```python
    news_store: NewsStore | None = None
    news_collector: NewsCollector | None = None
    news_collector_task: asyncio.Task[None] | None = None
```

`startup()` 里，在策略建表那几行之后加建表，并在 equity_sampler 之后加采集循环：

```python
        create_news_schema = getattr(self.news_store, "create_schema", None)
        if create_news_schema is not None:
            await create_news_schema()

        # 新闻采集：上游无分页也无历史回填，只能靠持续轮询攒出纵深
        news_interval = self.settings.news_collect_interval_seconds
        if (
            self.settings.news_collect_enabled
            and self.news_collector is not None
            and news_interval > 0
        ):
            if self.settings.news_warmup_on_start:
                # 一次性冷启动预热，不进常驻循环
                await self.news_collector.warmup()
            self.news_collector_task = asyncio.create_task(
                self.news_collector.run_forever(news_interval)
            )
            logger.info("news 采集已启动，间隔 %ds", news_interval)
```

`aclose()` 里，在 equity_sampler_task 的 cancel 块之后加：

```python
        if self.news_collector_task is not None:
            self.news_collector_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.news_collector_task
            self.news_collector_task = None
```

`build_container()` 里，在原 grok 装配的位置加（`http_clients` 与 `redis_client` / `engine` 变量沿用该函数中已有的同名局部变量）：

```python
    # Followin 新闻聚合。客户端在此创建并登记，由 aclose() 统一关闭。
    followin_http = httpx.AsyncClient(timeout=settings.followin_timeout_seconds)
    http_clients.append(followin_http)
    followin_client: Any
    if settings.followin_api_key.strip():
        followin_client = FollowinClient(
            settings.followin_api_key,
            followin_http,
            mcp_url=settings.followin_mcp_url,
        )
    else:
        logger.warning(
            "AGENT_FOLLOWIN_API_KEY 未配置，新闻链路降级为 Fake 模式，/news 返回的是模拟数据"
        )
        followin_client = FakeFollowinClient()

    news_store: NewsStore = (
        PostgresNewsStore(engine) if engine is not None else InMemoryNewsStore()
    )
    news_collector = NewsCollector(
        followin_client,
        news_store,
        limit=settings.news_collect_limit,
        round_lock=round_lock,  # Task 1 已把该块改为 news 语义，复用即可，勿重复装配
        retention_days=settings.news_retention_days,
    )
```

并在返回的 `Container(...)` 里补两个实参：`news_store=news_store,`、`news_collector=news_collector,`。

变量名已核对：`build_container()` 里就叫 `http_clients`（`container.py:177`）、`redis_client`（`:178`）、`engine`（`:179`），直接用。这段装配必须放在 Task 1 改好的 `round_lock` / `rate_limiter` if/else **之后**，否则引用未定义变量。

- [ ] **Step 6: 注册路由**

`app/main.py` 里加 import 与挂载（放在 strategy_router 之后）：

```python
    app.include_router(news_router, prefix="/v1/agent")
```

- [ ] **Step 7: 跑全量测试**

Run: `.venv/bin/pytest -q`
Expected: 全绿。

- [ ] **Step 8: 起服务冒烟（Fake 模式，不需要真 key）**

```bash
.venv/bin/python -c "
import asyncio, httpx
from httpx import ASGITransport
from app.main import create_app

async def main():
    app = create_app()
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as c:
        for path in ('/v1/agent/news', '/v1/agent/news?tag=crypto', '/v1/agent/grok/trends'):
            r = await c.get(path)
            print(path, r.status_code)
asyncio.run(main())
"
```

Expected: 前两个 200（Fake 模式下 store 为空则 `items: []`），`/v1/agent/grok/trends` 返回 **404**——证明 Grok 路径已彻底下线。（`create_app` 已核对为 `app/main.py:44` 的工厂函数，直接用。）

- [ ] **Step 9: Commit**

```bash
git add app/api/news.py tests/api/test_news.py app/container.py app/main.py
git commit -m "feat: GET /v1/agent/news 查询接口与容器装配"
```

---

### Task 8: 部署前置、文档与上线校验

**Files:**
- Modify: `docs/api/openapi.json`、`docs/deploy/`（按该目录现有文件组织增补）、`README.md`
- Create: `docs/deploy/followin-news.md`

**Interfaces:**
- Consumes: Task 7 完成后的运行时
- Produces: 部署清单与上线校验记录

- [ ] **Step 1: 重新导出 openapi.json**

```bash
.venv/bin/python -c "
import json
from app.main import create_app
spec = create_app().openapi()
with open('docs/api/openapi.json', 'w') as fh:
    json.dump(spec, fh, ensure_ascii=False, indent=2)
    fh.write('\n')
"
```

- [ ] **Step 2: 核对导出结果**

```bash
.venv/bin/python -c "
import json
paths = json.load(open('docs/api/openapi.json'))['paths']
assert '/v1/agent/news' in paths, 'news 未进 openapi'
assert not [p for p in paths if 'grok' in p], f'grok 残留: {[p for p in paths if \"grok\" in p]}'
print('ok, paths =', len(paths))
"
```

Expected: `ok, paths = 23`（原 22 条 + `/v1/agent/news`；grok 三条本就不在该文件里）

- [ ] **Step 3: 写部署文档**

创建 `docs/deploy/followin-news.md`：

```markdown
# Followin 新闻聚合部署说明

## 数据库前置

关键词检索依赖 `pg_trgm`。`PostgresNewsStore.create_schema()` 会在
PostgreSQL 上自动执行 `CREATE EXTENSION IF NOT EXISTS pg_trgm`，但该语句
**需要建库权限**。若运行账号无权限，请由 DBA 预先执行：

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

未安装扩展时服务仍可启动、检索仍可用，只是退化为顺序扫描。

## 环境变量

| 变量 | 建议值 | 说明 |
|---|---|---|
| `AGENT_FOLLOWIN_API_KEY` | 必填 | 走 secret manager，**不入 git**。`provider_mode=real` 时缺失会启动失败 |
| `AGENT_FOLLOWIN_MCP_URL` | 默认值即可 | `https://mcp.followin.io/v2/mcp` |
| `AGENT_NEWS_COLLECT_ENABLED` | `true` | 默认 false，部署时显式打开 |
| `AGENT_NEWS_COLLECT_INTERVAL_SECONDS` | `300` | 上游 50 条约 25 分钟耗尽，不要调到 1500 以上 |
| `AGENT_NEWS_WARMUP_ON_START` | 首次 `true`，随后改回 `false` | 一次性冷启动预热 |
| `AGENT_NEWS_RETENTION_DAYS` | `90` | 约 2900 条/天 |

## 首次部署顺序

1. 确认 `pg_trgm` 已就绪
2. `AGENT_NEWS_WARMUP_ON_START=true` 启动一次，日志出现「news 冷启动预热新增 N 条」
3. 改回 `false` 重启
4. 观察日志「news 采集本轮新增 N 条」按 5 分钟节奏出现

## 已知限制（须同步给产品）

- **上线首日新闻纵深有限**：上游无分页、无历史查询，`time_range` 只是过滤窗口不能翻页。
  首轮只能拿到最近约 25 分钟的内容，首日约 500 条（含预热），次日起约 2900 条/天。
  **这是上游能力所限，不是缺陷。**
- `twitter` / `telegram` 源不入库（实测 100% 无标题，卡片渲染不出来）。KOL 观点检索属二期。
- 2 字中文关键词（降息 / 关税）无法使用 `pg_trgm` 索引，走顺序扫描；由默认 30 天时间窗兜住范围。
```

- [ ] **Step 4: 在 README 增补新闻链路说明**

已核对：`README.md` 与 `docs/api/agent-mobile/` 均**未**提及 Grok，所以无需替换、只需增补。在 README 的接口/能力清单处加一段：数据来自 Followin MCP、后台采集入库、对外只有 `GET /v1/agent/news`，并指向 `docs/deploy/followin-news.md`。`docs/deploy/` 现有 `staging.md` 一个文件，新文档与它并列。

- [ ] **Step 5: 上线前校验一（配额周期）**

用真实 key 连续调用并观察 `meta.quota`，确认 `limit=100` 是日配额还是月配额。采集器每轮 3 次、每天约 864 次，**若为日配额则必然超限**，须据此调大间隔或找 Followin 提额。

```bash
.venv/bin/python - <<'PY'
import asyncio, os, httpx
from app.news.followin_client import FollowinClient

async def main():
    async with httpx.AsyncClient(timeout=30) as http:
        client = FollowinClient(os.environ["AGENT_FOLLOWIN_API_KEY"], http)
        for i in range(3):
            data = await client.fetch_news(sources=["media"], limit=5)
            print(i, data["meta"]["quota"])
asyncio.run(main())
PY
```

把观察结果记录到 `docs/deploy/followin-news.md` 的「已知限制」里。**consumed 一直为 0 不等于不限量**——须向 Followin 书面确认后再定论。

- [ ] **Step 6: 上线前校验二（中文检索延迟）**

采集跑满 24 小时后，在真实 PG 上实测 2 字中文关键词的查询延迟：

```sql
EXPLAIN ANALYZE
SELECT * FROM news
WHERE published_at >= now() - interval '30 days'
  AND (title ILIKE '%降息%' OR content ILIKE '%降息%')
ORDER BY published_at DESC, news_id DESC
LIMIT 21;
```

预期出现 Seq Scan（trigram 对 2 字词提取不出完整 gram，这是符合预期的）。**把实际耗时记进文档**；若超过 500ms，按 spec §10.2 评估 `pg_bigm` 或 pgvector。这一项**不得凭估算收尾**。

- [ ] **Step 7: 轮换 API Key**

设计讨论期间 key 曾以明文出现在协作对话中。在 Followin 后台轮换一次，新 key 只进 secret manager。轮换后重跑 Step 5 的脚本确认连通。

- [ ] **Step 8: Commit**

```bash
git add docs/api/openapi.json docs/deploy/followin-news.md README.md
git commit -m "docs: Followin 新闻部署说明与 openapi 同步"
```

---

## Self-Review

**1. Spec 覆盖检查**

| Spec 章节 | 落地位置 |
|---|---|
| §1 目标 1 采集入库 | Task 3、4、5、6 |
| §1 目标 2 标签体系 | Task 2（tagger）、§8 规则表 |
| §1 目标 3 查询接口 | Task 7 |
| §1 非目标（不留适配层） | Task 1 Step 5/9 + Task 7 Step 8 断言 `/grok/trends` 返 404 |
| §2 Spike 结论 | Task 3 fixtures 复刻两个 route 的真实形态 |
| §3 只采 media + research(+trending) | Task 6 `ROUTES` |
| §4 文件划分 | Task 2~7 的 Files 段 |
| §5 数据模型 / tags 关联表 / HTML 剥离 | Task 5 `pg_store.py`、Task 4 `strip_html` |
| §6 薄封装 / session 自愈 | Task 3 |
| §7 间隔 300s / dedup 合并 / 冷启动预热 | Task 6、Task 5 `_resolve_target` |
| §8 词边界规则 | Task 2 `_compile` + 对应测试 |
| §9 接口契约 / 单值 tag / 时间窗护栏 | Task 7 |
| §10 已知限制 | Task 8 Step 3/5/6 |
| §11 配置项 | Task 1 Step 6 |
| §12 Grok 下线清单 | Task 1 |
| §13 测试策略 | 各 Task 的测试步骤 |
| §15 安全（脱敏 / key 轮换 / like 转义） | Task 1 Step 1-4、Task 8 Step 7、Task 5 `_escape_like` |

无缺口。

**2. Placeholder 扫描**：无 TBD / TODO / "similar to Task N" / 无代码的实现步骤。写计划时留的三处「请核对变量名」已全部查实并填死：`create_app`（`main.py:44`）、`http_clients` / `redis_client` / `engine`（`container.py:177-179`）、README 未提 Grok 故改为增补而非替换。

另修正一处会导致执行失败的疏漏：`round_lock` / `rate_limiter` 的装配与 grok 的 `snapshot_store` 夹在同一个 if/else 块（`container.py:381-394`），初版 Task 1 只说「保留装配」，执行者极可能连块删除，导致 Task 6/7 引用未定义变量。已在 Task 1 Step 8 给出该块的完整替换代码，并把 Task 7 的 collector 装配改为复用 `round_lock` 而非重建。

**3. 类型一致性**

- `NewsStore` 三方法签名在 Task 5 定义，Task 6（`upsert_many`/`purge_before`）与 Task 7（`query`）调用一致。
- `to_news_item(row, *, source)` 在 Task 4 定义，Task 6 `_collect` 按此调用。
- `tags_for(*, title, content, asset_type, provenance)` 在 Task 2 定义，Task 4 按此调用。
- `fetch_news(**arguments)` 在 Task 3 定义，Task 6 `_NewsFetcher` Protocol 与之匹配。
- `encode_cursor`/`decode_cursor` 在 `store.py` 定义，`pg_store.py` 从 `app.news.store` 导入，未重复实现。
- `NewsCard.of` 在 Task 2 定义，Task 7 调用。
- `NewsItem.tags` 是 `tuple[NewsTag, ...]`，`NewsCard.tags` 是 `list[NewsTag]`（JSON 数组）——`of()` 里显式转换，非笔误。
