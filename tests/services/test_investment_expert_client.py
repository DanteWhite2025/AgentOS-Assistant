"""投研证据抓取客户端单测（T2）。

client 不再做独立 LLM 深度分析，只并发组装行情与新闻证据 —— 见
docs/plans/2026-09-02-investment-expert-evidence/TECH_DESIGN.md §4.3。
"""

from __future__ import annotations

import inspect

from app.services.investment_expert_client import (
    FakeInvestmentExpertClient,
    InvestmentExpertClient,
)


async def test_collect_evidence_returns_structured_bundle() -> None:
    client = InvestmentExpertClient(core=None, news_store=None)

    result = await client.collect_evidence("比特币为什么跌了")

    assert result["source"] == "MARKET_EVIDENCE"
    assert "market" in result and "news" in result
    assert "evidence_text" in result


async def test_collect_evidence_passes_symbols_through() -> None:
    from types import SimpleNamespace

    class _StubCoreClient:
        def __init__(self) -> None:
            self.received_symbols: list[str] | None = None

        async def get_market_tickers(self, symbols, request_id=None):
            self.received_symbols = symbols
            return SimpleNamespace(data=[])

    core = _StubCoreClient()
    client = InvestmentExpertClient(core=core, news_store=None)

    await client.collect_evidence("BTC 和 ETH 怎么样", symbols=["BTC", "ETH"])

    assert core.received_symbols == ["BTC", "ETH"]


async def test_collect_evidence_degrades_when_core_missing() -> None:
    client = InvestmentExpertClient(core=None, news_store=None)

    result = await client.collect_evidence("比特币为什么跌了", symbols=["BTC"])

    assert result["market"]["error"]
    assert result["market"]["data"] == []


async def test_collect_evidence_degrades_when_news_store_missing() -> None:
    client = InvestmentExpertClient(core=None, news_store=None)

    result = await client.collect_evidence("比特币为什么跌了")

    assert result["news"]["error"]
    assert result["news"]["items"] == []


def test_client_does_not_take_an_llm_anymore() -> None:
    # 防止有人为了"图方便"悄悄把 LLM 深度分析加回来——这个客户端的职责
    # 就是纯数据组装，深度分析工作已经转移给主脑那一轮。
    params = inspect.signature(InvestmentExpertClient.__init__).parameters
    assert "llm" not in params


async def test_collect_evidence_preserves_chat_id_signature_for_compatibility() -> None:
    # chat_id 目前不影响返回内容，但签名必须继续接受它——调用方（工具层）
    # 传参不应因为这次改造而报错。
    client = InvestmentExpertClient(core=None, news_store=None)
    result = await client.collect_evidence("比特币为什么跌了", chat_id="c1")
    assert result["source"] == "MARKET_EVIDENCE"


async def test_fake_client_returns_evidence_shaped_bundle() -> None:
    client = FakeInvestmentExpertClient()

    result = await client.collect_evidence("BTC 走势如何？", symbols=["BTC"])

    assert result["source"] == "MARKET_EVIDENCE"
    assert result["market"]["data"]
    assert result["news"]["items"]


async def test_fake_client_does_not_contain_conclusive_wording() -> None:
    # fake 数据只放事实，不放结论性措辞——照搬"综合看短期波动加大"这类话
    # 会掩盖"主脑没在做分析"这个 bug，而且它本身就命中新写的套话负面清单。
    client = FakeInvestmentExpertClient()
    result = await client.collect_evidence("BTC 走势如何？")

    banned_phrases = ("短期波动加大", "建议关注关键阻力位", "受宏观情绪影响", "多空博弈加剧")
    text = result["evidence_text"]
    for phrase in banned_phrases:
        assert phrase not in text
