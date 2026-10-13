"""本地新闻库检索单测 —— 作为 Tavily 联网搜索的前置降本提速方案。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.news.local_search import local_news_tool_output, search_local_news
from app.news.models import NewsItem
from app.news.store import InMemoryNewsStore

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _item(news_id: str, *, title: str, content: str = "正文", minutes_ago: int = 10) -> NewsItem:
    return NewsItem(
        news_id=news_id,
        dedup_key=title.casefold(),
        source="media",
        title=title,
        content=content,
        source_url=f"https://example.com/{news_id}",
        published_at=_NOW - timedelta(minutes=minutes_ago),
    )


async def test_search_local_news_matches_first_keyword_with_hits():
    store = InMemoryNewsStore()
    await store.upsert_many([_item("n1", title="黄金价格创新高")])

    items = await search_local_news(store, "黄金和比特币为什么涨了又跌了", now=_NOW)

    assert len(items) == 1
    assert items[0].news_id == "n1"


async def test_search_local_news_falls_through_keywords_until_hit():
    store = InMemoryNewsStore()
    await store.upsert_many([_item("n1", title="比特币突破十万美元")])

    # "黄金" 查不到，应继续尝试下一个关键词 "比特币"
    items = await search_local_news(store, "黄金和比特币最新消息", now=_NOW)

    assert len(items) == 1
    assert items[0].news_id == "n1"


async def test_search_local_news_skips_single_character_keywords():
    """单字关键词子串匹配几乎必然命中无关文章，宁可回退 Tavily 也不能瞎命中。"""
    store = InMemoryNewsStore()
    await store.upsert_many([_item("n1", title="某无关新闻里恰好包含跌字")])

    items = await search_local_news(store, "跌", now=_NOW)

    assert items == []


async def test_search_local_news_merges_results_across_multiple_matching_keywords():
    """多资产查询（如"黄金和比特币"）时，两个资产各自的新闻都要能拿到；不能
    因为第一个关键词命中就放弃后面的关键词——那会让另一个资产的新闻证据
    完全拿不到（investment-expert-evidence 改造实测暴露的真实缺口）。"""
    store = InMemoryNewsStore()
    await store.upsert_many(
        [
            _item("n1", title="黄金价格创新高"),
            _item("n2", title="比特币突破十万美元"),
        ]
    )

    items = await search_local_news(store, "黄金和比特币最新消息", now=_NOW)

    assert {item.news_id for item in items} == {"n1", "n2"}


async def test_search_local_news_deduplicates_items_matched_by_multiple_keywords():
    store = InMemoryNewsStore()
    await store.upsert_many([_item("n1", title="黄金和比特币同步大跌")])

    items = await search_local_news(store, "黄金和比特币最新消息", now=_NOW)

    assert [item.news_id for item in items] == ["n1"]


async def test_search_local_news_respects_limit_across_merged_keywords():
    store = InMemoryNewsStore()
    await store.upsert_many(
        [
            _item("n1", title="黄金价格创新高", minutes_ago=5),
            _item("n2", title="黄金持续走强", minutes_ago=6),
            _item("n3", title="比特币突破十万美元", minutes_ago=7),
            _item("n4", title="比特币成交量激增", minutes_ago=8),
        ]
    )

    items = await search_local_news(store, "黄金和比特币最新消息", limit=3, now=_NOW)

    assert len(items) == 3


async def test_search_local_news_returns_empty_when_nothing_matches():
    store = InMemoryNewsStore()
    await store.upsert_many([_item("n1", title="无关新闻标题")])

    items = await search_local_news(store, "黄金和比特币最新消息", now=_NOW)

    assert items == []


async def test_search_local_news_ignores_stale_items_outside_lookback_window():
    store = InMemoryNewsStore()
    await store.upsert_many([_item("n1", title="黄金价格", minutes_ago=60 * 24 * 3)])

    items = await search_local_news(store, "黄金", lookback_hours=24, now=_NOW)

    assert items == []


def test_local_news_tool_output_matches_tavily_search_shape():
    items = [_item("n1", title="黄金价格创新高", content="x" * 600)]

    output = local_news_tool_output(items, "黄金")

    assert output["source"] == "LOCAL_NEWS_STORE"
    assert output["query"] == "黄金"
    assert output["results"][0]["title"] == "黄金价格创新高"
    assert output["results"][0]["url"] == "https://example.com/n1"
    assert len(output["results"][0]["content"]) == 501
    assert output["results"][0]["content"].endswith("…")
