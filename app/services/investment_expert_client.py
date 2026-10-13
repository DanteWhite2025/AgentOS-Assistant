"""投研证据抓取客户端（基于 Core 行情 + 本地新闻，零 LLM）。

原来这里是一个独立的"迷你分析师"：只拿用户原话、不给任何数据，自己跑一次 LLM
憋出一段套话，再被主脑压缩转述一次——三跳 LLM 串联却换不回一句有数据支撑的
分析。现在降级成纯粹的证据组装：并发抓取 Core 行情快照与本地新闻，拼成结构化
证据包，深度归因分析工作交给主脑在最终回复那一轮里完成。

见 docs/plans/2026-09-02-investment-expert-evidence/TECH_DESIGN.md §4.3。
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any, Final

from app.news.store import NewsStore
from app.services.market_evidence import build_market_evidence
from app.tools.core_client import CoreClient

_DEFAULT_NEWS_LOOKBACK_HOURS = 24


class InvestmentExpertUpstreamError(RuntimeError):
    """投研证据抓取失败。带足信息供调用方决定降级还是告警。"""

    def __init__(
        self, message: str, *, status_code: int | None = None, is_timeout: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.is_timeout = is_timeout


# 保持向后兼容别名
MinaraUpstreamError = InvestmentExpertUpstreamError


class InvestmentExpertClient:
    """投研证据抓取客户端。不做分析、不调 LLM，只并发组装行情与新闻证据。"""

    def __init__(
        self,
        *,
        core: CoreClient | None = None,
        news_store: NewsStore | None = None,
        news_lookback_hours: int = _DEFAULT_NEWS_LOOKBACK_HOURS,
    ) -> None:
        self._core = core
        self._news_store = news_store
        self._news_lookback_hours = news_lookback_hours

    async def collect_evidence(
        self,
        question: str,
        *,
        symbols: Sequence[str] = (),
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        return await build_market_evidence(
            question=question,
            symbols=symbols,
            core=self._core,
            news_store=self._news_store,
            news_lookback_hours=self._news_lookback_hours,
        )


# 保持向后兼容别名
MinaraClient = InvestmentExpertClient


_FAKE_EVIDENCE_RESPONSE: Final[dict[str, Any]] = {
    "source": "MARKET_EVIDENCE",
    "question": "(fake)",
    "as_of": "2026-01-01T00:00:00Z",
    "market": {
        "data": [
            {
                "symbol": "BTC",
                "last_price": "65000.00",
                "change_percent_24h": "-1.50",
                "high_24h": "66500.00",
                "low_24h": "64200.00",
                "funding_rate": "0.0050",
                "open_interest": "800000000.00",
                "quote_volume_24h": "3000000000.00",
                "as_of": "2026-01-01T00:00:00Z",
                "stale": False,
            }
        ],
        "unavailable_symbols": [],
        "error": None,
    },
    "news": {
        "source": "LOCAL_NEWS_STORE",
        "items": [
            {
                "title": "（Fake 新闻）美联储议息会议维持利率不变",
                "published_at": "2026-01-01T00:00:00Z",
                "source_name": "Fake Source",
                "url": "https://example.com/fake",
                "excerpt": "（Fake 摘要）市场对本次决议已有充分预期，声明措辞与上次基本一致。",
            }
        ],
        "error": None,
    },
    "coverage": {"has_market": True, "has_news": True},
    "evidence_policy": (
        "只能基于以上字段作答；unavailable_symbols 中的标的必须明说暂无实时行情、"
        "只能依据新闻定性，严禁编造点位、涨跌幅或资金费率。"
    ),
    "evidence_text": (
        "【行情快照】\n"
        "- BTC：现价 65000.00，24h涨跌 -1.50%，24h区间 64200.00~66500.00，"
        "资金费率 0.0050，持仓量 800000000.00，成交额 3000000000.00\n"
        "【相关新闻】\n"
        "- [2026-01-01T00:00:00Z] （Fake 新闻）美联储议息会议维持利率不变（Fake Source）："
        "（Fake 摘要）市场对本次决议已有充分预期，声明措辞与上次基本一致。"
    ),
}


class FakeInvestmentExpertClient:
    """Fake 模式。返回内联样例证据包，不调用任何依赖，测试/离线场景用。

    内容只放数据，不放结论性措辞——照搬"综合行情看短期波动加大"这类话会
    掩盖"主脑没在做分析"这个 bug，而且它本身就命中新写的套话负面清单。
    """

    async def collect_evidence(
        self,
        question: str,
        *,
        symbols: Sequence[str] = (),
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        return copy.deepcopy(_FAKE_EVIDENCE_RESPONSE)


# 保持向后兼容别名
FakeMinaraClient = FakeInvestmentExpertClient
