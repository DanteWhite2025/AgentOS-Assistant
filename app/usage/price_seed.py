"""单价与汇率初始值。

这里的数字是**起点，不是事实**：上线后必须与供应商实际报价核对，调价也要来改。
匹配不到价时流水会落 status=unpriced 并在报表里露出条数，所以漏配不会变成
静默的 0（spec §5.2）。

改价时**新增**一条 effective_from 更晚的条目，不要改已有那条——历史流水已经
快照了当时的单价，但重算时仍要按当时的价来（spec §4.2）。

单位口径见 app/usage/pricing.py 的 _METRICS：token 类按 1M tokens，
语音按分钟，外部 API 按次。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.usage.fx import FxTable
from app.usage.pricing import PriceBook, PriceEntry

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
_VERSION = "seed-2026-09"

#: 兜底汇率，配置未给时用。
_DEFAULT_CNY_TO_USD = Decimal("0.1385")


def _entry(
    provider: str, kind: str, metric: str, price: str, currency: str = "USD"
) -> PriceEntry:
    return PriceEntry(
        provider=provider,
        kind=kind,
        metric=metric,
        unit_price=Decimal(price),
        currency=currency,
        effective_from=_EPOCH,
        price_version=_VERSION,
        model=None,
    )


def default_price_book(settings: Any) -> PriceBook:
    """provider 级兜底价。

    需要按模型细分时，加 model 非空的条目即可覆盖同 provider 的兜底价
    （PriceBook.lookup 精确 model 优先）。
    """
    return PriceBook(
        [
            # LLM（USD / 1M tokens）
            _entry("anthropic", "llm", "input_tokens", "15"),
            _entry("anthropic", "llm", "output_tokens", "75"),
            _entry("anthropic", "llm", "cache_read", "1.5"),
            _entry("anthropic", "llm", "cache_write", "18.75"),
            _entry("gemini", "llm", "input_tokens", "1.25"),
            _entry("gemini", "llm", "output_tokens", "10"),
            _entry("gemini", "llm", "cache_read", "0.31"),
            _entry("gemini", "llm", "cache_write", "1.625"),
            # 语音：按分钟 + 按 token（record_voice 把 audio/text token 存进
            # input_tokens / output_tokens 两列，故 metric 同名但 kind 不同）
            _entry("volc", "voice", "duration_minute", "0.6", currency="CNY"),
            _entry("volc", "voice", "input_tokens", "0", currency="CNY"),
            _entry("volc", "voice", "output_tokens", "0", currency="CNY"),
            _entry("gemini", "voice", "duration_minute", "0.03"),
            _entry("gemini", "voice", "input_tokens", "3"),
            _entry("gemini", "voice", "output_tokens", "12"),
            _entry("openai", "voice", "duration_minute", "0.06"),
            _entry("openai", "voice", "input_tokens", "40"),
            _entry("openai", "voice", "output_tokens", "80"),
            # 外部 API（USD / 次）
            _entry("tavily", "external_api", "call", "0.008"),
            _entry("followin", "external_api", "call", "0"),
            # Core 是内部服务，单价 0，但流水照采（spec D2）
            _entry("core", "external_api", "call", "0"),
            _entry("internal", "external_api", "call", "0"),
            # 未登记的工具：计 0，但 provider 标 unknown，报表里能发现漏配
            _entry("unknown", "external_api", "call", "0"),
        ]
    )


def default_fx_table(settings: Any) -> FxTable:
    """一期只需 CNY→USD 的固定兜底汇率（spec §4.2）。

    每日汇率接入属二期；固定值先保证 CNY 供应商的成本不会因为缺汇率全落 unpriced。
    """
    rate = getattr(settings, "usage_fx_cny_to_usd", None)
    fallback = _DEFAULT_CNY_TO_USD if rate is None else Decimal(str(rate))
    return FxTable([], fallback={"CNY": fallback})
