"""单价表与计价。

匹配不到单价时返回 priced=False、amount=None，由调用方落 status=unpriced。
**绝不返回 0 冒充免费**——那会让成本被系统性低估（spec §5.2）。
单价 0（如 Core 内部服务，spec D2）是有效价格，与「查不到价」严格区分。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from app.usage.models import UsageQuantity

#: 金额与单价统一保留 8 位小数，与 DB 的 Numeric(scale=8) 对齐。
AMOUNT_EXPONENT = Decimal("0.00000001")

TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)
SECONDS_PER_MINUTE = Decimal(60)

#: quantity 字段 → (metric 名, 把原始量换算成「计价单位数」的除数)
_METRICS: tuple[tuple[str, str, Decimal], ...] = (
    ("input_tokens", "input_tokens", TOKENS_PER_PRICE_UNIT),
    ("output_tokens", "output_tokens", TOKENS_PER_PRICE_UNIT),
    ("cache_read_tokens", "cache_read", TOKENS_PER_PRICE_UNIT),
    ("cache_write_tokens", "cache_write", TOKENS_PER_PRICE_UNIT),
    ("duration_seconds", "duration_minute", SECONDS_PER_MINUTE),
    ("call_count", "call", Decimal(1)),
)


def quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(AMOUNT_EXPONENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class PriceEntry:
    provider: str
    kind: str
    metric: str
    unit_price: Decimal
    currency: str
    effective_from: datetime
    price_version: str
    #: None 表示该 provider+kind 的兜底价；精确 model 优先。
    model: str | None = None
    effective_to: datetime | None = None


@dataclass(frozen=True)
class PricedAmount:
    """计价结果。``priced=False`` 表示缺价，此时 amount 必为 None。"""

    priced: bool
    amount: Decimal | None
    currency: str | None
    price_version: str | None
    snapshot: dict[str, str]


class PriceBook:
    """内存态单价表。条目不可变，构造后不改。"""

    def __init__(self, entries: Iterable[PriceEntry]) -> None:
        self._entries: Sequence[PriceEntry] = tuple(entries)

    def lookup(
        self, *, provider: str, kind: str, metric: str, model: str | None, at: datetime
    ) -> PriceEntry | None:
        """取 at 时刻生效的单价。生效区间是左闭右开 [from, to)。"""
        candidates = [
            entry
            for entry in self._entries
            if entry.provider == provider
            and entry.kind == kind
            and entry.metric == metric
            and entry.effective_from <= at
            and (entry.effective_to is None or at < entry.effective_to)
            and (entry.model is None or entry.model == model)
        ]
        if not candidates:
            return None
        # 精确 model 优先于兜底价；同档取最近生效的一条。
        candidates.sort(key=lambda e: (e.model is not None, e.effective_from))
        return candidates[-1]


def price_quantity(
    quantity: UsageQuantity,
    book: PriceBook,
    *,
    provider: str,
    kind: str,
    model: str | None,
    at: datetime,
) -> PricedAmount:
    """按原始量纲逐 metric 计价并求和。

    只有「量非 None 且非 0」的 metric 才需要单价：cache 两项常为 None 或 0，
    不该因为没配 cache 价就把整条流水判成 unpriced。
    """
    total = Decimal(0)
    snapshot: dict[str, str] = {}
    currency: str | None = None
    price_version: str | None = None

    for field, metric, divisor in _METRICS:
        raw = getattr(quantity, field)
        if raw is None:
            continue
        amount_units = Decimal(str(raw)) / divisor
        if amount_units == 0:
            continue
        entry = book.lookup(provider=provider, kind=kind, metric=metric, model=model, at=at)
        if entry is None:
            return PricedAmount(
                priced=False, amount=None, currency=None, price_version=None, snapshot={}
            )
        total += amount_units * entry.unit_price
        snapshot[metric] = str(entry.unit_price)
        currency = entry.currency
        price_version = entry.price_version

    return PricedAmount(
        priced=True,
        amount=quantize_amount(total),
        currency=currency,
        price_version=price_version,
        snapshot=snapshot,
    )
