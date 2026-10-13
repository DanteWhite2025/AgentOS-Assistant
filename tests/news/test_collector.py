"""采集器测试。重点：单源失败隔离、RoundLock、CancelledError 必须传播。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

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
    # ROUTES 本身不带 limit 键（limit 由 _collect 统一合并），
    # 运行时 limit 的上限校验见 test_limit_above_upstream_cap_rejected 等用例。


async def test_refresh_once_passes_the_configured_limit_to_fetch_news() -> None:
    """真正验证运行时行为：传给 fetch_news 的 limit 就是构造时传入的值。"""
    client = _StubClient({}, {}, {})
    await NewsCollector(client, InMemoryNewsStore(), limit=37).refresh_once()
    assert client.calls, "至少应发起一次调用"
    assert all(call["limit"] == 37 for call in client.calls)


@pytest.mark.parametrize("limit", [51, 500])
def test_limit_above_upstream_cap_rejected(limit: int) -> None:
    """上游 news 工具 limit 硬上限 50，超限必须在构造时直接报错，不静默截断。"""
    with pytest.raises(ValueError):
        NewsCollector(_StubClient(), InMemoryNewsStore(), limit=limit)


@pytest.mark.parametrize("limit", [0, -1])
def test_limit_below_minimum_rejected(limit: int) -> None:
    with pytest.raises(ValueError):
        NewsCollector(_StubClient(), InMemoryNewsStore(), limit=limit)


@pytest.mark.parametrize("limit", [1, 50])
def test_limit_within_bounds_accepted(limit: int) -> None:
    NewsCollector(_StubClient(), InMemoryNewsStore(), limit=limit)


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


# 修复 6：单 route 失败只记 warning 是对的——但三条 route 全挂时，此前也只是
# 三条 warning 混在日志里，没有任何东西提级。真机事故正是这样：新闻库停更了
# 一周，日志里翻不出一条能一眼看出"新闻整体停更"的记录。全灭这个信号必须
# 单独提级成 error，否则下次同样的静默停更还是要等真机反馈才能发现。
async def test_all_routes_failing_logs_error(caplog: pytest.LogCaptureFixture) -> None:
    client = _StubClient(
        FollowinUpstreamError("media 挂了", status_code=502),
        FollowinUpstreamError("research 挂了", status_code=502),
        FollowinUpstreamError("trending 挂了", status_code=502),
    )
    store = InMemoryNewsStore()
    with caplog.at_level("ERROR", logger="app.news.collector"):
        written = await NewsCollector(client, store, limit=50).refresh_once()
    assert written == 0
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert error_records, "全部 route 失败必须有一条 error 级别日志"
    assert "media" in error_records[0].message
    assert "research" in error_records[0].message
    assert "trending" in error_records[0].message


async def test_partial_route_failure_does_not_log_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """只挂一个 route 不该触发全灭的 error 提级——保持原有 warning 粒度。"""
    client = _StubClient(
        FollowinUpstreamError("media 挂了", status_code=502),
        _payload("followin_media.json"),
        _payload("followin_trending.json"),
    )
    store = InMemoryNewsStore()
    with caplog.at_level("WARNING", logger="app.news.collector"):
        await NewsCollector(client, store, limit=50).refresh_once()
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


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
    from app.news.collector import WARMUP_QUERIES

    client = _StubClient(*[_payload("followin_media.json")] * (len(WARMUP_QUERIES) * 2))
    await NewsCollector(client, InMemoryNewsStore(), limit=50).warmup()
    assert client.calls, "预热应至少发一次调用"
    assert all("query" in call for call in client.calls), "预热必须走带 query 的 search route"


def test_warmup_queries_cover_gold_stocks_and_crypto_etf() -> None:
    from app.news.collector import WARMUP_QUERIES

    joined = " ".join(WARMUP_QUERIES)
    assert "Gold" in joined
    assert "ETF" in joined
    assert any("stocks" in q.lower() or "NVIDIA" in q for q in WARMUP_QUERIES)


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


# 修复 8：warmup() 不走轮次锁——refresh_once 过了 round_lock，warmup 没有。
# 拒锁时 warmup 仍会发出 8 次上游调用；N 个副本 = 8N 次调用，首次部署／
# 滚动重启期间若 AGENT_NEWS_WARMUP_ON_START 仍为 true，就可能打爆上游配额。
async def test_warmup_skips_fetching_when_round_lock_denied() -> None:
    client = _StubClient(*[_payload("followin_media.json")] * 8)
    collector = NewsCollector(
        client, InMemoryNewsStore(), limit=50, round_lock=_DenyLock()
    )
    written = await collector.warmup()
    assert client.calls == [], "未拿到轮次锁就不该发起预热抓取"
    assert written == 0


# ---------------------------------------------------------------------------
# 入库前翻译英文标题（Followin trending：正文中文、标题英文）
# ---------------------------------------------------------------------------


def _trending_payload(*titles: str, content: str = "中文摘要正文") -> dict:
    return {
        "results": {
            "articles": [
                {
                    "title": title,
                    "content": f"{content}{index}",
                    "published_ts": 1787190519312,
                    "provenance": "followin_trending",
                }
                for index, title in enumerate(titles)
            ]
        }
    }


class _StubTranslator:
    """按固定映射翻译；记录每次收到的标题，用于断言"没有重复送翻译"。"""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._mapping = mapping or {}
        self.seen: list[list[str]] = []

    async def translate(self, titles):
        self.seen.append(list(titles))
        return {t: self._mapping[t] for t in titles if t in self._mapping}


async def test_english_trending_title_is_translated_before_insert() -> None:
    store = InMemoryNewsStore()
    translator = _StubTranslator({"BTC breaks 71500": "比特币突破 71500"})
    collector = NewsCollector(
        _StubClient({}, {}, _trending_payload("BTC breaks 71500")),
        store,
        translator=translator,
    )

    await collector.refresh_once()

    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 1
    assert items[0].title == "比特币突破 71500"
    assert items[0].title_en == "BTC breaks 71500", "英文原文必须留存"


async def test_chinese_titles_are_not_sent_for_translation() -> None:
    store = InMemoryNewsStore()
    translator = _StubTranslator()
    collector = NewsCollector(
        _StubClient({}, {}, _trending_payload("比特币突破 71500")), store, translator=translator
    )

    await collector.refresh_once()

    assert translator.seen == [], "已经是中文的标题不该送翻译"
    items, _ = await store.query(NewsQuery(limit=10))
    assert items[0].title == "比特币突破 71500" and items[0].title_en == ""


async def test_translation_failure_keeps_english_title_and_still_inserts() -> None:
    """翻译拿不到结果不能阻断入库——宁可显示英文，不能丢新闻。"""
    store = InMemoryNewsStore()
    collector = NewsCollector(
        _StubClient({}, {}, _trending_payload("BTC breaks 71500")),
        store,
        translator=_StubTranslator({}),  # 返回空映射
    )

    await collector.refresh_once()

    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 1
    assert items[0].title == "BTC breaks 71500" and items[0].title_en == ""


async def test_collector_without_translator_behaves_as_before() -> None:
    store = InMemoryNewsStore()
    await NewsCollector(
        _StubClient({}, {}, _trending_payload("BTC breaks 71500")), store
    ).refresh_once()

    items, _ = await store.query(NewsQuery(limit=10))
    assert items[0].title == "BTC breaks 71500" and items[0].title_en == ""


async def test_already_stored_titles_are_not_retranslated() -> None:
    """trending route 每 5 分钟回一次同样的 50 条。重复送翻译等于按轮烧钱。"""
    store = InMemoryNewsStore()
    translator = _StubTranslator({"BTC breaks 71500": "比特币突破 71500"})
    payload = _trending_payload("BTC breaks 71500")
    collector = NewsCollector(
        _StubClient({}, {}, payload, {}, {}, payload), store, translator=translator
    )

    await collector.refresh_once()
    await collector.refresh_once()

    assert len(translator.seen) == 1, "第二轮同一条新闻不该再送翻译"


async def test_same_title_on_two_rows_gets_one_translation_applied_to_both() -> None:
    """上游会给同一标题配不同正文（正文里带各自的实时行情），两条都要拿到
    译文。送翻译时的去重由翻译层负责（见 test_title_translator）。"""
    store = InMemoryNewsStore()
    translator = _StubTranslator({"BTC breaks 71500": "比特币突破 71500"})
    collector = NewsCollector(
        _StubClient({}, {}, _trending_payload("BTC breaks 71500", "BTC breaks 71500")),
        store,
        translator=translator,
    )

    await collector.refresh_once()

    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 2, "正文不同 → 两条独立新闻"
    assert all(item.title == "比特币突破 71500" for item in items)
    assert all(item.title_en == "BTC breaks 71500" for item in items)


async def test_translator_exception_does_not_break_the_route() -> None:
    """翻译层已自带兜底，但这里再钉一道：它若抛异常，本 route 仍要入库。"""

    class _BoomTranslator:
        async def translate(self, titles):
            raise RuntimeError("gateway down")

    store = InMemoryNewsStore()
    await NewsCollector(
        _StubClient({}, {}, _trending_payload("BTC breaks 71500")),
        store,
        translator=_BoomTranslator(),
    ).refresh_once()

    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 1 and items[0].title == "BTC breaks 71500"


async def test_translation_cancellation_propagates() -> None:
    class _CancelTranslator:
        async def translate(self, titles):
            raise asyncio.CancelledError()

    collector = NewsCollector(
        _StubClient({}, {}, _trending_payload("BTC breaks 71500")),
        InMemoryNewsStore(),
        translator=_CancelTranslator(),
    )
    with pytest.raises(asyncio.CancelledError):
        await collector.refresh_once()


# ---------------------------------------------------------------------------
# 英文正文/摘要联合翻译测试
# ---------------------------------------------------------------------------


class _StubItemTranslator:
    def __init__(self, mapping: dict[str, tuple[str, str]] | None = None) -> None:
        self._mapping = mapping or {}
        self.seen_items: list[list[Any]] = []

    async def translate_items(self, items):
        self.seen_items.append(list(items))
        res = {}
        for it in items:
            if it.title in self._mapping:
                res[it.news_id] = self._mapping[it.title]
            elif it.news_id in self._mapping:
                res[it.news_id] = self._mapping[it.news_id]
        return res


def _english_media_payload(title: str, content: str) -> dict:
    return {
        "results": {
            "articles": [
                {
                    "title": title,
                    "content": content,
                    "published_ts": 1787190519312,
                    "source_url": f"https://example.com/{title.replace(' ', '_')}",
                    "provenance": "media",
                }
            ]
        }
    }


async def test_english_body_is_translated_and_summarized_before_insert() -> None:
    store = InMemoryNewsStore()
    translator = _StubItemTranslator({
        "ARM stock surges": ("ARM 股价大涨", "受强劲的AI需求驱动，ARM股价大涨5%。")
    })
    client = _StubClient(
        _english_media_payload(
            "ARM stock surges",
            "ARM stock surged 5% today driven by strong AI demand.",
        ),
        {},
        {},
    )
    collector = NewsCollector(client, store, translator=translator)
    await collector.refresh_once()

    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 1
    assert items[0].title == "ARM 股价大涨"
    assert items[0].title_en == "ARM stock surges"
    assert items[0].content == "受强劲的AI需求驱动，ARM股价大涨5%。"
    assert items[0].content_en == "ARM stock surged 5% today driven by strong AI demand."


async def test_already_stored_english_body_items_are_not_retranslated() -> None:
    store = InMemoryNewsStore()
    translator = _StubItemTranslator({
        "ARM stock surges": ("ARM 股价大涨", "受强劲的AI需求驱动，ARM股价大涨5%。")
    })
    payload = _english_media_payload(
        "ARM stock surges",
        "ARM stock surged 5% today driven by strong AI demand.",
    )
    client = _StubClient(payload, {}, {})
    collector = NewsCollector(client, store, translator=translator)

    await collector.refresh_once()
    assert len(translator.seen_items) == 1

    # 第二轮
    client_round2 = _StubClient(payload, {}, {})
    collector_round2 = NewsCollector(client_round2, store, translator=translator)
    await collector_round2.refresh_once()

    assert len(translator.seen_items) == 1, "已有 content_en 的英文正文条目第二轮应跳过翻译"


async def test_existing_row_without_content_en_gets_body_translated_on_next_round() -> None:
    store = InMemoryNewsStore()
    from app.news.normalizer import rows_from, to_news_item

    payload = _english_media_payload(
        "ARM stock surges",
        "ARM stock surged 5% today driven by strong AI demand.",
    )
    row = rows_from(payload)[0]
    legacy_item = to_news_item(row, source="media")
    assert legacy_item is not None
    legacy_item = legacy_item.model_copy(
        update={"title": "ARM 股价大涨", "title_en": "ARM stock surges"}
    )
    await store.upsert_many([legacy_item])

    translator = _StubItemTranslator({
        "ARM stock surges": ("ARM 股价大涨", "受强劲的AI需求驱动，ARM股价大涨5%。")
    })
    client = _StubClient(payload, {}, {})
    collector = NewsCollector(client, store, translator=translator)
    await collector.refresh_once()

    assert len(translator.seen_items) == 1, "存量只有 title_en 但 content 是英文的条目，本轮必须被送翻译"
    items, _ = await store.query(NewsQuery(limit=10))
    assert items[0].content == "受强劲的AI需求驱动，ARM股价大涨5%。"
    assert items[0].content_en == "ARM stock surged 5% today driven by strong AI demand."

