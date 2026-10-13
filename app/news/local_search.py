"""本地 Followin 新闻库检索 —— 作为 Tavily 联网搜索的前置降本提速方案。

只做「查得到就秒回」的浅层子串匹配；查不到时由调用方原样回退到 Tavily，
本地库的召回率上限绝不能拖累用户能拿到的答案质量。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.news.models import NewsItem, NewsQuery
from app.news.store import NewsStore
from app.tools.query_normalization import extract_search_keywords

_DEFAULT_LOOKBACK_HOURS = 24
_DEFAULT_LIMIT = 5
_MAX_CONTENT_LENGTH = 500
# 短于这个长度的"关键词"（如剥不出词后回退成的单字）在子串匹配下几乎必然
# 命中一堆无关文章；宁可老实回退 Tavily，也不能拿一个假命中糊弄用户。
_MIN_KEYWORD_LENGTH = 2


async def search_local_news(
    store: NewsStore,
    query: str,
    *,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
    limit: int = _DEFAULT_LIMIT,
    now: datetime | None = None,
) -> list[NewsItem]:
    """按全部候选关键词并发查本地库，跨关键词合并去重，最多返回 limit 条。

    改造前是"命中第一个有结果的关键词就返回，不做跨关键词合并"——多资产查询
    （如"黄金和比特币最新消息"）时，只要"黄金"先命中，"比特币"那部分新闻
    证据就完全拿不到（investment-expert-evidence 改造实测暴露的真实缺口，
    详见 docs/plans/2026-09-02-investment-expert-evidence/）。改成并发查询
    全部关键词，按关键词优先级顺序合并并按 news_id 去重，命中即可秒回的
    延迟特性不变（并发而非串行）。
    """
    since = (now or datetime.now(UTC)) - timedelta(hours=lookback_hours)
    keywords = [kw for kw in extract_search_keywords(query) if len(kw) >= _MIN_KEYWORD_LENGTH]
    if not keywords:
        return []

    results = await asyncio.gather(
        *(store.query(NewsQuery(q=kw, since=since, limit=limit)) for kw in keywords)
    )

    seen_ids: set[str] = set()
    merged: list[NewsItem] = []
    for items, _cursor in results:
        for item in items:
            if item.news_id in seen_ids:
                continue
            seen_ids.add(item.news_id)
            merged.append(item)
            if len(merged) >= limit:
                return merged
    return merged


def local_news_tool_output(items: list[NewsItem], query: str) -> dict[str, object]:
    """裁剪成与 tavily_search 一致的返回形状，下游总结逻辑不用区分数据来源。"""
    results = []
    for item in items:
        content = item.content
        if len(content) > _MAX_CONTENT_LENGTH:
            content = content[:_MAX_CONTENT_LENGTH] + "…"
        results.append({"title": item.title, "url": item.source_url, "content": content})
    return {
        "source": "LOCAL_NEWS_STORE",
        "query": query,
        "answer": "",
        "results": results,
    }
