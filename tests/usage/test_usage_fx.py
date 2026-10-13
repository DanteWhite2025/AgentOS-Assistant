from datetime import UTC, date, datetime
from decimal import Decimal

from app.usage.fx import FxRate, FxTable, convert_to_usd


def test_usd_needs_no_rate() -> None:
    assert convert_to_usd(Decimal("1.5"), "USD", None) == Decimal("1.50000000")


def test_cny_converts_with_rate_snapshot() -> None:
    rate = FxRate(
        rate_date=date(2026, 9, 9), currency="CNY", rate_to_usd=Decimal("0.14"), source="ecb"
    )
    assert convert_to_usd(Decimal("0.9"), "CNY", rate) == Decimal("0.12600000")


def test_missing_rate_yields_none_not_zero() -> None:
    """没汇率就留空，不能当成 0 元（spec §7.2 同一红线）。"""
    assert convert_to_usd(Decimal("0.9"), "CNY", None) is None


def test_rate_for_picks_latest_on_or_before_date() -> None:
    """周末与节假日没有当日汇率，取不晚于该日的最近一条。"""
    table = FxTable(
        [
            FxRate(
                rate_date=date(2026, 9, 4),
                currency="CNY",
                rate_to_usd=Decimal("0.13"),
                source="ecb",
            ),
            FxRate(
                rate_date=date(2026, 9, 8),
                currency="CNY",
                rate_to_usd=Decimal("0.14"),
                source="ecb",
            ),
        ]
    )
    hit = table.rate_for("CNY", datetime(2026, 9, 9, tzinfo=UTC))
    assert hit is not None and hit.rate_to_usd == Decimal("0.14")
    early = table.rate_for("CNY", datetime(2026, 9, 5, tzinfo=UTC))
    assert early is not None and early.rate_to_usd == Decimal("0.13")


def test_rate_for_returns_none_before_first_record() -> None:
    table = FxTable(
        [
            FxRate(
                rate_date=date(2026, 9, 8),
                currency="CNY",
                rate_to_usd=Decimal("0.14"),
                source="ecb",
            )
        ]
    )
    assert table.rate_for("CNY", datetime(2026, 9, 1, tzinfo=UTC)) is None


def test_rate_for_unknown_currency_is_none() -> None:
    assert FxTable([]).rate_for("JPY", datetime(2026, 9, 9, tzinfo=UTC)) is None


def test_fallback_rate_used_when_table_has_no_record() -> None:
    """配置的固定汇率作为兜底（spec §4.2）。"""
    table = FxTable([], fallback={"CNY": Decimal("0.1385")})
    hit = table.rate_for("CNY", datetime(2026, 9, 9, tzinfo=UTC))
    assert hit is not None
    assert hit.rate_to_usd == Decimal("0.1385")
    assert hit.source == "fallback"
