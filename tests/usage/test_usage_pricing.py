from datetime import UTC, datetime
from decimal import Decimal

from app.usage.models import UsageQuantity
from app.usage.pricing import PriceBook, PriceEntry, price_quantity, quantize_amount

AT = datetime(2026, 9, 10, tzinfo=UTC)


def _entry(
    metric: str, price: str, *, model: str | None = "claude-opus-5", **kw: object
) -> PriceEntry:
    defaults: dict[str, object] = {
        "provider": "anthropic",
        "kind": "llm",
        "metric": metric,
        "unit_price": Decimal(price),
        "currency": "USD",
        "effective_from": datetime(2026, 1, 1, tzinfo=UTC),
        "price_version": "v1",
        "model": model,
    }
    defaults.update(kw)
    return PriceEntry(**defaults)  # type: ignore[arg-type]


def test_token_price_is_per_million() -> None:
    """$15/1M input + $75/1M output，1000 in / 200 out。"""
    book = PriceBook([_entry("input_tokens", "15"), _entry("output_tokens", "75")])
    priced = price_quantity(
        UsageQuantity(input_tokens=1000, output_tokens=200),
        book,
        provider="anthropic",
        kind="llm",
        model="claude-opus-5",
        at=AT,
    )
    assert priced.priced is True
    # 1000/1e6*15 = 0.015 ; 200/1e6*75 = 0.015
    assert priced.amount == Decimal("0.03000000")
    assert priced.currency == "USD"
    assert priced.price_version == "v1"
    assert priced.snapshot == {"input_tokens": "15", "output_tokens": "75"}


def test_duration_price_is_per_minute_and_not_rounded_up() -> None:
    """成本核算按真实秒数折算，不做「不足一分钟按一分钟」的向上取整——
    那是配额口径（compute_billed_units），不是成本口径。"""
    book = PriceBook(
        [
            _entry(
                "duration_minute",
                "0.6",
                provider="volc",
                kind="voice",
                model=None,
                currency="CNY",
            )
        ]
    )
    priced = price_quantity(
        UsageQuantity(duration_seconds=Decimal(90)),
        book,
        provider="volc",
        kind="voice",
        model=None,
        at=AT,
    )
    assert priced.amount == Decimal("0.90000000")  # 1.5 分钟 × 0.6
    assert priced.currency == "CNY"


def test_call_price_is_per_call() -> None:
    book = PriceBook([_entry("call", "0.008", provider="tavily", kind="external_api", model=None)])
    priced = price_quantity(
        UsageQuantity(call_count=3),
        book,
        provider="tavily",
        kind="external_api",
        model=None,
        at=AT,
    )
    assert priced.amount == Decimal("0.02400000")


def test_zero_price_entry_is_priced_not_unpriced() -> None:
    """Core 是内部服务，单价 0（spec D2）。0 与「查不到价」必须区分开。"""
    book = PriceBook([_entry("call", "0", provider="core", kind="external_api", model=None)])
    priced = price_quantity(
        UsageQuantity(call_count=5),
        book,
        provider="core",
        kind="external_api",
        model=None,
        at=AT,
    )
    assert priced.priced is True
    assert priced.amount == Decimal("0E-8")


def test_missing_price_yields_unpriced_with_no_amount() -> None:
    """匹配不到单价 → 金额留空，绝不填 0（spec §5.2）。"""
    priced = price_quantity(
        UsageQuantity(input_tokens=100),
        PriceBook([]),
        provider="anthropic",
        kind="llm",
        model="claude-opus-5",
        at=AT,
    )
    assert priced.priced is False
    assert priced.amount is None


def test_partial_price_coverage_is_unpriced() -> None:
    """有 input 价没 output 价时，整条算 unpriced——只算一半会得出偏低的假金额。"""
    book = PriceBook([_entry("input_tokens", "15")])
    priced = price_quantity(
        UsageQuantity(input_tokens=1000, output_tokens=200),
        book,
        provider="anthropic",
        kind="llm",
        model="claude-opus-5",
        at=AT,
    )
    assert priced.priced is False
    assert priced.amount is None


def test_none_quantity_fields_do_not_require_a_price() -> None:
    """cache 两项为 None 时不参与计价，不因缺 cache 价而 unpriced。"""
    book = PriceBook([_entry("input_tokens", "15"), _entry("output_tokens", "75")])
    priced = price_quantity(
        UsageQuantity(input_tokens=1000, output_tokens=200, cache_read_tokens=None),
        book,
        provider="anthropic",
        kind="llm",
        model="claude-opus-5",
        at=AT,
    )
    assert priced.priced is True


def test_zero_quantity_still_needs_no_price_and_costs_nothing() -> None:
    """量为 0 时该 metric 不计费，也不因缺价而 unpriced。"""
    book = PriceBook([_entry("input_tokens", "15")])
    priced = price_quantity(
        UsageQuantity(input_tokens=1000, cache_write_tokens=0),
        book,
        provider="anthropic",
        kind="llm",
        model="claude-opus-5",
        at=AT,
    )
    assert priced.priced is True
    assert priced.amount == Decimal("0.01500000")


def test_effective_window_is_half_open() -> None:
    """[effective_from, effective_to) —— 调价当刻不能两条价都命中。"""
    old = _entry(
        "call",
        "1",
        provider="tavily",
        kind="external_api",
        model=None,
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        effective_to=datetime(2026, 9, 1, tzinfo=UTC),
        price_version="v1",
    )
    new = _entry(
        "call",
        "2",
        provider="tavily",
        kind="external_api",
        model=None,
        effective_from=datetime(2026, 9, 1, tzinfo=UTC),
        price_version="v2",
    )
    book = PriceBook([old, new])
    before = book.lookup(
        provider="tavily",
        kind="external_api",
        metric="call",
        model=None,
        at=datetime(2026, 8, 31, tzinfo=UTC),
    )
    boundary = book.lookup(
        provider="tavily",
        kind="external_api",
        metric="call",
        model=None,
        at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert before is not None and before.price_version == "v1"
    assert boundary is not None and boundary.price_version == "v2"


def test_model_specific_price_beats_wildcard() -> None:
    """model=None 的条目是该 provider 的兜底价；精确匹配优先。"""
    book = PriceBook(
        [
            _entry("input_tokens", "3", model=None, price_version="wild"),
            _entry("input_tokens", "15", model="claude-opus-5", price_version="exact"),
        ]
    )
    hit = book.lookup(
        provider="anthropic", kind="llm", metric="input_tokens", model="claude-opus-5", at=AT
    )
    assert hit is not None and hit.price_version == "exact"
    fallback = book.lookup(
        provider="anthropic", kind="llm", metric="input_tokens", model="claude-haiku-4-5", at=AT
    )
    assert fallback is not None and fallback.price_version == "wild"


def test_quantize_amount_keeps_eight_decimals_half_up() -> None:
    """token 单价量级到 1e-8，精度必须显式定死；
    strategy 侧曾因 Decimal 默认精度静默截断踩过一次。"""
    assert quantize_amount(Decimal("0.000000005")) == Decimal("0.00000001")
    assert quantize_amount(Decimal(1)) == Decimal("1.00000000")


def test_empty_quantity_is_priced_zero() -> None:
    priced = price_quantity(
        UsageQuantity(), PriceBook([]), provider="x", kind="llm", model=None, at=AT
    )
    assert priced.priced is True
    assert priced.amount == Decimal("0E-8")
    assert priced.snapshot == {}
