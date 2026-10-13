"""市场证据组装（T1）：并发抓取 Core 行情快照 + 本地新闻，不做分析、不调 LLM，永不抛异常。

见 docs/plans/2026-09-02-investment-expert-evidence/TECH_DESIGN.md §4.2。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from types import SimpleNamespace

from app.news.models import NewsItem
from app.services.market_evidence import build_market_evidence


def _now() -> datetime:
    return datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)


def _ticker(symbol: str, **overrides: object) -> SimpleNamespace:
    base = {
        "symbol": symbol,
        "last_price": "92400.00",
        "change_percent_24h": "-3.20",
        "high_24h": "96000.00",
        "low_24h": "91000.00",
        "funding_rate": "-0.0120",
        "open_interest": "1200000000.00",
        "quote_volume_24h": "5000000000.00",
        "as_of": "2026-09-02T12:00:00Z",
        "stale": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _news_item(
    title: str = "美联储维持利率不变", content: str = "内容" * 10, *, news_id: str = "n1"
) -> NewsItem:
    return NewsItem(
        news_id=news_id,
        dedup_key=title,
        source="media",
        title=title,
        content=content,
        published_at=_now(),
        source_name="Followin",
        source_url=f"https://example.com/{news_id}",
    )


class _StubCoreClient:
    def __init__(
        self,
        tickers: list[SimpleNamespace] | None = None,
        *,
        delay: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self._tickers = tickers or []
        self._delay = delay
        self._error = error
        self.received_symbols: list[str] | None = None

    async def get_market_tickers(self, symbols: list[str], request_id: str | None = None):
        self.received_symbols = symbols
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return SimpleNamespace(data=[t for t in self._tickers if t.symbol in symbols])


class _StubNewsStore:
    def __init__(
        self,
        items: list[NewsItem] | None = None,
        *,
        delay: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self._items = items or []
        self._delay = delay
        self._error = error

    async def query(self, q):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return (list(self._items), None)

    async def upsert_many(self, items):  # pragma: no cover - not exercised here
        raise NotImplementedError

    async def purge_before(self, cutoff):  # pragma: no cover - not exercised here
        raise NotImplementedError


async def test_fetches_market_and_news_concurrently() -> None:
    core = _StubCoreClient([_ticker("BTC")], delay=0.05)
    news_store = _StubNewsStore([_news_item()], delay=0.05)

    started = time.monotonic()
    await build_market_evidence(
        question="比特币为什么跌了", symbols=["BTC"], core=core, news_store=news_store, now=_now()
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.08, f"两路应并行抓取，实际耗时 {elapsed:.3f}s 疑似串行执行"


async def test_market_failure_does_not_poison_news_branch() -> None:
    core = _StubCoreClient(error=RuntimeError("core 挂了"))
    news_store = _StubNewsStore([_news_item()])

    evidence = await build_market_evidence(
        question="比特币为什么跌了", symbols=["BTC"], core=core, news_store=news_store, now=_now()
    )

    assert evidence["market"]["error"]
    assert evidence["news"]["error"] is None
    assert evidence["news"]["items"]


async def test_news_failure_does_not_poison_market_branch() -> None:
    core = _StubCoreClient([_ticker("BTC")])
    news_store = _StubNewsStore(error=RuntimeError("新闻库挂了"))

    evidence = await build_market_evidence(
        question="比特币为什么跌了", symbols=["BTC"], core=core, news_store=news_store, now=_now()
    )

    assert evidence["news"]["error"]
    assert evidence["market"]["error"] is None
    assert evidence["market"]["data"]


async def test_non_crypto_symbol_goes_to_unavailable_without_breaking_batch() -> None:
    """直接对应用户投诉原始问题："黄金和比特币近期行情大变动的原因"——黄金没有
    Core 行情，必须优雅降级，不能拖累 BTC 那部分真实数据。"""
    core = _StubCoreClient([_ticker("BTC")])
    news_store = _StubNewsStore([_news_item()])

    evidence = await build_market_evidence(
        question="黄金和比特币为什么大涨大跌",
        symbols=["黄金", "BTC"],
        core=core,
        news_store=news_store,
        now=_now(),
    )

    assert evidence["market"]["unavailable_symbols"] == ["黄金"]
    assert len(evidence["market"]["data"]) == 1
    assert evidence["market"]["data"][0]["symbol"] == "BTC"
    assert core.received_symbols == ["BTC"]


async def test_fake_core_client_runtime_error_degrades_to_market_error() -> None:
    from app.tools.core_client import FakeCoreClient

    evidence = await build_market_evidence(
        question="比特币为什么跌了",
        symbols=["BTC"],
        core=FakeCoreClient(),
        news_store=_StubNewsStore([_news_item()]),
        now=_now(),
    )

    assert evidence["market"]["error"]
    assert evidence["market"]["data"] == []


async def test_market_soft_timeout_degrades_instead_of_blocking() -> None:
    core = _StubCoreClient([_ticker("BTC")], delay=0.3)
    news_store = _StubNewsStore([_news_item()])

    started = time.monotonic()
    evidence = await build_market_evidence(
        question="比特币为什么跌了",
        symbols=["BTC"],
        core=core,
        news_store=news_store,
        market_timeout_seconds=0.05,
        now=_now(),
    )
    elapsed = time.monotonic() - started

    assert evidence["market"]["error"]
    assert evidence["market"]["data"] == []
    assert elapsed < 0.2, "软超时必须提前放弃，不能被行情接口的抖动拖住整轮对话"


async def test_empty_symbols_returns_news_only() -> None:
    news_store = _StubNewsStore([_news_item()])

    evidence = await build_market_evidence(
        question="最近有什么宏观新闻", symbols=[], core=_StubCoreClient(), news_store=news_store, now=_now()
    )

    assert evidence["market"]["data"] == []
    assert evidence["market"]["unavailable_symbols"] == []
    assert evidence["news"]["items"]


async def test_news_items_and_excerpts_respect_budget() -> None:
    items = [
        _news_item(title=f"新闻{i}", content="正文内容" * 200, news_id=f"n{i}") for i in range(10)
    ]
    news_store = _StubNewsStore(items)

    evidence = await build_market_evidence(
        question="比特币为什么跌了",
        symbols=[],
        core=_StubCoreClient(),
        news_store=news_store,
        max_news_items=5,
        max_excerpt_chars=300,
        now=_now(),
    )

    assert len(evidence["news"]["items"]) <= 5
    for item in evidence["news"]["items"]:
        assert len(item["excerpt"]) <= 301  # 300 + 省略号


class _BaseExceptionOnly(BaseException):
    """不是 Exception 的子类——专门用来验证 gather 层的最后一道防线，而不是
    _fetch_market/_fetch_news 内部那层 `except Exception` 兜底。"""


async def test_never_raises_when_market_branch_raises_a_bare_base_exception() -> None:
    class _BrokenCoreClient:
        async def get_market_tickers(self, symbols, request_id=None):
            raise _BaseExceptionOnly("不是 Exception 的异常")

    evidence = await build_market_evidence(
        question="比特币为什么跌了",
        symbols=["BTC"],
        core=_BrokenCoreClient(),
        news_store=_StubNewsStore([_news_item()]),
        now=_now(),
    )

    assert evidence["market"]["error"]
    assert evidence["news"]["error"] is None


async def test_never_raises_even_when_all_dependencies_are_broken() -> None:
    evidence = await build_market_evidence(
        question="比特币为什么跌了", symbols=["BTC"], core=None, news_store=None, now=_now()
    )

    assert evidence["source"] == "MARKET_EVIDENCE"
    assert evidence["market"]["error"]
    assert evidence["news"]["error"]
    assert evidence["market"]["data"] == []
    assert evidence["news"]["items"] == []


async def test_logs_observability_summary_after_fetch(caplog) -> None:
    """T7：上线后要能靠日志核对延迟预算，以及"黄金这类标的到底有没有召回到新闻"。"""
    core = _StubCoreClient([_ticker("BTC")])
    news_store = _StubNewsStore([_news_item(), _news_item(title="第二条", news_id="n2")])

    with caplog.at_level(logging.INFO, logger="app.services.market_evidence"):
        await build_market_evidence(
            question="黄金和比特币为什么大涨大跌",
            symbols=["黄金", "BTC"],
            core=core,
            news_store=news_store,
            now=_now(),
        )

    records = [r for r in caplog.records if r.name == "app.services.market_evidence"]
    assert records, "缺少可观测日志"
    message = records[0].getMessage()
    assert "market_evidence" in message
    assert "market_ok=True" in message
    assert "news_count=2" in message
    assert "unavailable_symbols=['黄金']" in message
    assert "elapsed_ms=" in message


async def test_evidence_text_contains_key_metrics() -> None:
    core = _StubCoreClient([_ticker("BTC")])
    news_store = _StubNewsStore([_news_item()])

    evidence = await build_market_evidence(
        question="比特币为什么跌了", symbols=["BTC"], core=core, news_store=news_store, now=_now()
    )

    text = evidence["evidence_text"]
    assert "资金费率" in text
    assert "24h" in text
    assert "持仓量" in text
    assert "92400.00" in text
