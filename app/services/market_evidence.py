"""市场证据组装 —— 并发抓取 Core 行情快照与本地新闻，不做分析、不调 LLM。

`ask_investment_expert` 曾经是一个独立的"迷你分析师"：只拿用户原话、不给任何数据、
自己跑一次 LLM 憋出一段套话，再被主脑压缩转述一次。这里把它降级成纯粹的"跑腿取证员"：
只负责把 Core 行情快照和本地新闻并发抓回来、拼成结构化证据包，深度归因分析工作
留给主脑在最终回复那一轮里完成（见 docs/plans/2026-09-02-investment-expert-evidence/）。

被上层（ask_investment_expert 工具）调用的路径上绝不允许抛异常：任一分支失败都要
优雅降级为 error 字段，不能把整个 LLM turn 或 websocket 连接带崩。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from app.news.local_search import search_local_news
from app.news.store import NewsStore
from app.tools.agentos.trade_flow import MarketInstrumentResolutionError, catalog_asset_query
from app.tools.core_client import CoreClient

logger = logging.getLogger(__name__)

_DEFAULT_MAX_SYMBOLS = 8
_DEFAULT_MAX_NEWS_ITEMS = 5
_DEFAULT_MAX_EXCERPT_CHARS = 300
_DEFAULT_NEWS_LOOKBACK_HOURS = 24
# 低于 core_timeout_seconds=10（那个超时是为交互式对话整体设的，行情这一路
# 不该单独占满它）：一个抖动的行情接口不能让整轮深度分析都卡住。
_MARKET_SOFT_TIMEOUT_SECONDS = 6.0

_EVIDENCE_POLICY = (
    "只能基于以上字段作答；unavailable_symbols 中的标的必须明说暂无实时行情、"
    "只能依据新闻定性，严禁编造点位、涨跌幅或资金费率。"
)


async def build_market_evidence(
    *,
    question: str,
    symbols: Sequence[str] = (),
    core: CoreClient | None = None,
    news_store: NewsStore | None = None,
    news_lookback_hours: int = _DEFAULT_NEWS_LOOKBACK_HOURS,
    max_news_items: int = _DEFAULT_MAX_NEWS_ITEMS,
    max_excerpt_chars: int = _DEFAULT_MAX_EXCERPT_CHARS,
    market_timeout_seconds: float = _MARKET_SOFT_TIMEOUT_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """并发抓取行情快照与新闻证据，拼成给主脑做深度分析用的证据包。永不抛异常。"""

    started = time.perf_counter()
    # return_exceptions=True：_fetch_market/_fetch_news 内部已经各自兜底了已知
    # 异常，但这里是模块对外承诺"永不抛异常"的最后一道防线——没有它，任何一路
    # 未预料到的异常都会让 gather 直接向上传播，砸穿 ask_investment_expert
    # "失败一律降级为 error dict"的约定。
    # 显式标注：typeshed 对 asyncio.gather(..., return_exceptions=True) 的重载
    # 推不出 tuple 解包后每个变量的类型，不标注 mypy 会直接报 has-type 错误。
    market_result: dict[str, Any] | BaseException
    news_result: dict[str, Any] | BaseException
    market_result, news_result = await asyncio.gather(
        _fetch_market(list(symbols)[:_DEFAULT_MAX_SYMBOLS], core, market_timeout_seconds),
        _fetch_news(question, news_store, news_lookback_hours, max_news_items, max_excerpt_chars),
        return_exceptions=True,
    )
    if isinstance(market_result, BaseException):
        logger.warning("market_evidence 行情分支未预期异常: %s", market_result)
        market = {"data": [], "unavailable_symbols": list(symbols), "error": "行情暂不可用"}
    else:
        market = market_result
    if isinstance(news_result, BaseException):
        logger.warning("market_evidence 新闻分支未预期异常: %s", news_result)
        news = {"source": "LOCAL_NEWS_STORE", "items": [], "error": "新闻检索暂不可用"}
    else:
        news = news_result
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "market_evidence symbols=%s unavailable_symbols=%s market_ok=%s news_count=%d elapsed_ms=%d",
        list(symbols),
        market["unavailable_symbols"],
        market["error"] is None,
        len(news["items"]),
        elapsed_ms,
    )

    return {
        "source": "MARKET_EVIDENCE",
        "question": question,
        "as_of": (now or datetime.now(UTC)).isoformat(),
        "market": market,
        "news": news,
        "coverage": {
            "has_market": bool(market["data"]),
            "has_news": bool(news["items"]),
        },
        "evidence_policy": _EVIDENCE_POLICY,
        "evidence_text": _render_evidence_text(market, news),
    }


async def _fetch_market(
    symbols: list[str], core: CoreClient | None, timeout_seconds: float
) -> dict[str, Any]:
    if core is None:
        return {"data": [], "unavailable_symbols": list(symbols), "error": "行情客户端未配置"}
    if not symbols:
        return {"data": [], "unavailable_symbols": [], "error": None}

    venue_symbols: list[str] = []
    unavailable: list[str] = []
    for raw in symbols:
        try:
            venue_symbols.append(catalog_asset_query(raw))
        except MarketInstrumentResolutionError:
            unavailable.append(raw)

    if not venue_symbols:
        return {"data": [], "unavailable_symbols": unavailable, "error": None}

    try:
        response = await asyncio.wait_for(
            core.get_market_tickers(venue_symbols), timeout=timeout_seconds
        )
    except TimeoutError:
        logger.warning("market_evidence 行情抓取超时 symbols=%s", venue_symbols)
        return {"data": [], "unavailable_symbols": list(symbols), "error": "行情抓取超时"}
    except Exception as exc:
        logger.warning("market_evidence 行情抓取失败 symbols=%s: %s", venue_symbols, exc)
        return {"data": [], "unavailable_symbols": list(symbols), "error": "行情暂不可用"}

    data = [
        {
            "symbol": ticker.symbol,
            "last_price": ticker.last_price,
            "change_percent_24h": ticker.change_percent_24h,
            "high_24h": ticker.high_24h,
            "low_24h": ticker.low_24h,
            "funding_rate": ticker.funding_rate,
            "open_interest": ticker.open_interest,
            "quote_volume_24h": ticker.quote_volume_24h,
            "as_of": ticker.as_of,
            "stale": ticker.stale,
        }
        for ticker in response.data
    ]
    return {"data": data, "unavailable_symbols": unavailable, "error": None}


async def _fetch_news(
    question: str,
    news_store: NewsStore | None,
    lookback_hours: int,
    max_items: int,
    max_excerpt_chars: int,
) -> dict[str, Any]:
    if news_store is None:
        return {"source": "LOCAL_NEWS_STORE", "items": [], "error": "新闻库未配置"}

    try:
        items = await search_local_news(
            news_store, question, lookback_hours=lookback_hours, limit=max_items
        )
    except Exception as exc:
        logger.warning("market_evidence 新闻检索失败: %s", exc)
        return {"source": "LOCAL_NEWS_STORE", "items": [], "error": "新闻检索暂不可用"}

    rendered = []
    for item in items[:max_items]:
        excerpt = item.content
        if len(excerpt) > max_excerpt_chars:
            excerpt = excerpt[:max_excerpt_chars] + "…"
        rendered.append(
            {
                "title": item.title,
                "published_at": item.published_at.isoformat(),
                "source_name": item.source_name,
                "url": item.source_url,
                "excerpt": excerpt,
            }
        )
    return {"source": "LOCAL_NEWS_STORE", "items": rendered, "error": None}


def _render_evidence_text(market: dict[str, Any], news: dict[str, Any]) -> str:
    """把结构化证据渲染成紧凑纯文本 —— 模型读预渲染文本远好于读嵌套 JSON 转义串。"""

    lines: list[str] = []

    if market["data"]:
        lines.append("【行情快照】")
        for row in market["data"]:
            stale_tag = "（数据延迟）" if row["stale"] else ""
            lines.append(
                f"- {row['symbol']}：现价 {row['last_price']}，24h涨跌 {row['change_percent_24h']}%，"
                f"24h区间 {row['low_24h']}~{row['high_24h']}，资金费率 {row['funding_rate']}，"
                f"持仓量 {row['open_interest']}，成交额 {row['quote_volume_24h']}{stale_tag}"
            )
    if market["unavailable_symbols"]:
        lines.append(
            f"【无实时行情】{', '.join(market['unavailable_symbols'])}："
            "仅可依据下方新闻做定性分析，不得编造点位。"
        )
    if market["error"]:
        lines.append(f"【行情抓取异常】{market['error']}")

    if news["items"]:
        lines.append("【相关新闻】")
        for item in news["items"]:
            lines.append(
                f"- [{item['published_at']}] {item['title']}（{item['source_name']}）：{item['excerpt']}"
            )
    if news["error"]:
        lines.append(f"【新闻检索异常】{news['error']}")

    if not lines:
        lines.append("未检索到任何行情或新闻证据，只能明确告知用户当前无法获取实时数据。")

    return "\n".join(lines)
