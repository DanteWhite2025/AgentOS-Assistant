"""容器接线回归。

最关键的一条是 test_runner_receives_usage_recorder：漏掉那一步接线，前面所有
记账代码都在，但一条流水都不会产生，而且没有任何报错——静默失效。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.config import Settings, get_settings
from app.container import build_container
from app.usage.price_seed import default_fx_table, default_price_book
from app.usage.recorder import UsageRecorder
from app.usage.store import InMemoryUsageStore

AT = datetime(2026, 9, 10, tzinfo=UTC)


@pytest.fixture
async def container() -> AsyncIterator[object]:
    """每个测试一套独立容器，用完必须 aclose——它持有 httpx client。"""
    ctn = build_container(get_settings())
    try:
        yield ctn
    finally:
        await ctn.aclose()


async def test_fake_mode_uses_in_memory_usage_store(container) -> None:
    assert isinstance(container.usage_store, InMemoryUsageStore)
    assert isinstance(container.usage_recorder, UsageRecorder)


async def test_runner_receives_usage_recorder(container) -> None:
    """接线漏这一步 = 记账静默失效，没有任何报错。"""
    assert container.runner._usage_recorder is container.usage_recorder
    assert container.runner._usage_recorder is not None


async def test_runner_receives_configured_usage_provider(container) -> None:
    assert container.runner._usage_provider == get_settings().usage_llm_provider


async def test_startup_creates_usage_schema(container) -> None:
    """建表要跟着 startup 走，照 test_memory_integration.py 的既有写法验证。"""
    called: list[bool] = []

    class SpyStore(InMemoryUsageStore):
        async def create_schema(self) -> None:
            called.append(True)

    container.usage_store = SpyStore()
    await container.startup()
    assert called == [True]


def test_default_price_book_covers_every_wired_provider() -> None:
    """每个会产生流水的 provider 都得有价，否则报表里全是 unpriced。"""
    book = default_price_book(Settings())
    assert book.lookup(provider="anthropic", kind="llm", metric="input_tokens", model=None, at=AT)
    assert book.lookup(provider="anthropic", kind="llm", metric="output_tokens", model=None, at=AT)
    assert book.lookup(provider="volc", kind="voice", metric="duration_minute", model=None, at=AT)
    assert book.lookup(
        provider="gemini", kind="voice", metric="duration_minute", model=None, at=AT
    )
    assert book.lookup(provider="tavily", kind="external_api", metric="call", model=None, at=AT)


def test_core_is_priced_zero_not_unpriced() -> None:
    """spec D2：Core 是内部服务，单价 0；0 与「查不到价」必须区分。"""
    entry = default_price_book(Settings()).lookup(
        provider="core", kind="external_api", metric="call", model=None, at=AT
    )
    assert entry is not None
    assert entry.unit_price == Decimal(0)


def test_internal_and_unknown_providers_are_priced_zero() -> None:
    """unknown 也要有价：漏配工具的成本记 0，但 provider 标 unknown 仍可见。"""
    book = default_price_book(Settings())
    for provider in ("internal", "unknown"):
        entry = book.lookup(
            provider=provider, kind="external_api", metric="call", model=None, at=AT
        )
        assert entry is not None, provider
        assert entry.unit_price == Decimal(0)


def test_fx_table_falls_back_to_configured_cny_rate() -> None:
    """CNY 供应商没有每日汇率时用配置的固定值，否则成本全落 unpriced。"""
    rate = default_fx_table(Settings()).rate_for("CNY", AT)
    assert rate is not None
    assert rate.source == "fallback"
    assert rate.rate_to_usd == Decimal(str(Settings().usage_fx_cny_to_usd))


def test_recorder_prices_a_real_llm_call_end_to_end() -> None:
    """初始单价表能真的把一次 LLM 调用算出非零金额——光有条目不代表配对了。"""
    from app.usage.models import UsageQuantity
    from app.usage.pricing import price_quantity

    priced = price_quantity(
        UsageQuantity(input_tokens=1_000_000, output_tokens=1_000_000),
        default_price_book(Settings()),
        provider="anthropic",
        kind="llm",
        model="claude-opus-5",
        at=AT,
    )
    assert priced.priced is True
    assert priced.amount is not None
    assert priced.amount > 0
