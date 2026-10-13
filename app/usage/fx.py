"""汇率折算。基准币种 USD（spec D4）。

折算在**流水写入时**做，汇率快照随流水存下来——供应商调价与汇率波动都不能
改写历史账单的金额（spec §4.2）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.usage.pricing import quantize_amount


@dataclass(frozen=True)
class FxRate:
    rate_date: date
    currency: str
    rate_to_usd: Decimal
    source: str


class FxTable:
    """按币种存一串按日汇率，取「不晚于目标日的最近一条」。

    周末与节假日没有当日汇率，若要求严格等于当日会大面积取不到值。
    """

    def __init__(
        self, rates: Iterable[FxRate], *, fallback: Mapping[str, Decimal] | None = None
    ) -> None:
        self._rates = tuple(sorted(rates, key=lambda r: r.rate_date))
        self._fallback = dict(fallback or {})

    def rate_for(self, currency: str, on: datetime) -> FxRate | None:
        target = on.date()
        hits = [r for r in self._rates if r.currency == currency and r.rate_date <= target]
        if hits:
            return hits[-1]
        fallback = self._fallback.get(currency)
        if fallback is None:
            return None
        return FxRate(rate_date=target, currency=currency, rate_to_usd=fallback, source="fallback")


def convert_to_usd(amount: Decimal, currency: str, rate: FxRate | None) -> Decimal | None:
    """折算为 USD。拿不到汇率返回 None（留空，不当成 0）。"""
    if currency == "USD":
        return quantize_amount(amount)
    if rate is None:
        return None
    return quantize_amount(amount * rate.rate_to_usd)
