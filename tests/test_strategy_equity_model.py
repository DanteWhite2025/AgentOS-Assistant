"""净值快照领域模型的构造校验 + 金额解析。"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.strategy.decimals import to_decimal
from app.strategy.equity import EquitySnapshot
from app.strategy.equity_pg_store import ensure_utc

_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _snap(**over):
    base = {
        "strategy_id": "s1", "user_id": "u1", "agent_id": "a1",
        "taken_at": _NOW, "equity": "1000",
    }
    base.update(over)
    return EquitySnapshot(**base)


def test_equity_and_realized_normalized_to_decimal():
    s = _snap(equity=1000.5, realized_pnl="12.5")
    assert isinstance(s.equity, Decimal)
    assert s.realized_pnl == Decimal("12.5")


@pytest.mark.parametrize("field", ["strategy_id", "user_id", "agent_id"])
def test_blank_identity_rejected(field):
    with pytest.raises(ValueError, match=f"{field} 不能为空"):
        _snap(**{field: "  "})


def test_naive_timestamp_rejected():
    """无时区的时间会让窗口比较直接抛 TypeError，必须在构造期就挡掉。"""
    with pytest.raises(ValueError, match="taken_at"):
        _snap(taken_at=_NOW.replace(tzinfo=None))


def test_non_numeric_equity_rejected():
    with pytest.raises(ValueError, match="equity 不是合法数值"):
        _snap(equity="n/a")


def test_bool_equity_rejected():
    with pytest.raises(ValueError, match="不能是布尔值"):
        _snap(equity=True)


def test_bool_realized_pnl_rejected():
    with pytest.raises(ValueError, match="realized_pnl 不能是布尔值"):
        _snap(realized_pnl=False)


def test_to_decimal_passes_decimal_through():
    value = Decimal("3.25")
    assert to_decimal(value, "x") is value


def test_ensure_utc_normalizes_naive_and_keeps_aware():
    """PG 驱动回读 tz-aware，SQLite 回读 naive —— 两种都要能用。"""
    assert ensure_utc(_NOW.replace(tzinfo=None)) == _NOW
    assert ensure_utc(_NOW) is _NOW
