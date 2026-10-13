"""策略容器的止盈/止损参数 —— 构造校验 + 派生到 AgentMandate。"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.strategy.container import StrategyContainer
from app.strategy.manager import derive_mandate

_NOW = datetime(2026, 8, 10, tzinfo=UTC)


def _c(**over):
    base = {
        "strategy_id": "s1", "user_id": "u1", "agent_id": "a1", "name": "s",
        "template_id": "grid_trading_v1", "runtime_env": "paper", "allocation": "1000",
        "capital_capacity": "5000", "strategy_capacity": "10000", "max_leverage": "3",
        "created_at": _NOW, "updated_at": _NOW,
    }
    base.update(over)
    return StrategyContainer(**base)


# --- 构造与校验 -----------------------------------------------------------


def test_tp_sl_default_to_none():
    """不填就是不设止盈止损，不能悄悄兜底成 0（0 会被当成「立刻平仓」）。"""
    c = _c()
    assert c.take_profit_pct is None
    assert c.stop_loss_pct is None


def test_tp_sl_normalized_to_decimal():
    c = _c(take_profit_pct="0.15", stop_loss_pct=0.05)
    assert c.take_profit_pct == Decimal("0.15")
    assert isinstance(c.stop_loss_pct, Decimal)


def test_take_profit_above_one_is_allowed():
    """止盈可以超过 100%（翻倍才平仓是合理配置）。"""
    assert _c(take_profit_pct="2").take_profit_pct == Decimal(2)


@pytest.mark.parametrize("value", ["0", "-0.1"])
def test_take_profit_must_be_positive(value):
    with pytest.raises(ValueError, match="take_profit_pct"):
        _c(take_profit_pct=value)


@pytest.mark.parametrize("value", ["0", "-0.1"])
def test_stop_loss_must_be_positive(value):
    with pytest.raises(ValueError, match="stop_loss_pct"):
        _c(stop_loss_pct=value)


def test_stop_loss_cannot_exceed_one():
    """止损超过 100% 没有意义，本金亏光就到头了。"""
    with pytest.raises(ValueError, match="stop_loss_pct"):
        _c(stop_loss_pct="1.5")


def test_stop_loss_at_exactly_one_is_allowed():
    assert _c(stop_loss_pct="1").stop_loss_pct == Decimal(1)


def test_tp_sl_reject_non_numeric():
    with pytest.raises(ValueError, match="take_profit_pct 不是合法数值"):
        _c(take_profit_pct="n/a")


def test_tp_sl_reject_bool():
    with pytest.raises(ValueError, match="不能是布尔值"):
        _c(stop_loss_pct=True)


def test_tp_sl_survive_lifecycle_transitions():
    """暂停/恢复不该把风控参数弄丢。"""
    c = _c(take_profit_pct="0.2", stop_loss_pct="0.1")
    paused = c.pause(_NOW)
    assert paused.take_profit_pct == Decimal("0.2")
    assert paused.resume(_NOW).stop_loss_pct == Decimal("0.1")


def test_tp_sl_survive_allocation_change():
    c = _c(take_profit_pct="0.2", stop_loss_pct="0.1").with_allocation("2000", _NOW)
    assert c.allocation == Decimal(2000)
    assert (c.take_profit_pct, c.stop_loss_pct) == (Decimal("0.2"), Decimal("0.1"))


# --- 派生 mandate ---------------------------------------------------------


def test_derive_mandate_carries_tp_sl():
    m = derive_mandate(_c(take_profit_pct="0.15", stop_loss_pct="0.05"))
    assert m.take_profit_pct == Decimal("0.15")
    assert m.stop_loss_pct == Decimal("0.05")


def test_derive_mandate_keeps_none_when_unset():
    m = derive_mandate(_c())
    assert m.take_profit_pct is None
    assert m.stop_loss_pct is None


def test_derive_mandate_still_carries_existing_bounds():
    """加字段不能挤掉原有的额度/杠杆/资产边界。"""
    m = derive_mandate(_c(allowed_assets=("BTC",), take_profit_pct="0.1"))
    assert m.max_notional == Decimal(1000)
    assert m.max_leverage == Decimal(3)
    assert m.allowed_assets == ("BTC",)
    assert m.require_confirmation is True
