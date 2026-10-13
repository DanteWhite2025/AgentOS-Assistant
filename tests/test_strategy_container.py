from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.strategy.container import IllegalStateTransition, StrategyContainer, StrategyState

_NOW = datetime(2026, 8, 8, tzinfo=UTC)


def _c(**over):
    base = {
        "strategy_id": "strat_1", "user_id": "u1", "agent_id": "a1", "name": "趋势1",
        "template_id": "tpl_trend", "runtime_env": "paper",
        "allocation": "100", "capital_capacity": "500", "strategy_capacity": "1000",
        "max_leverage": "3", "created_at": _NOW, "updated_at": _NOW,
    }
    base.update(over)
    return StrategyContainer(**base)


def test_valid_container_defaults():
    c = _c()
    assert c.state is StrategyState.ACTIVE
    assert c.version == 1
    assert c.allocation == Decimal(100) and c.strategy_capacity == Decimal(1000)
    assert c.allowed_assets is None and c.expires_at is None


@pytest.mark.parametrize("over, match", [
    ({"allocation": "600"}, "allocation"),          # allocation > capital_capacity
    ({"capital_capacity": "2000"}, "capital"),      # capital_capacity > strategy_capacity
    ({"allocation": "-1"}, "allocation"),           # 负数
    ({"max_leverage": "0"}, "leverage"),            # 杠杆须 > 0
    ({"runtime_env": "prod"}, "runtime_env"),       # 非法枚举
    ({"template_id": "  "}, "template_id"),         # 空
    ({"name": ""}, "name"),                         # 空
    ({"state": "garbage"}, "state"),                # 非法 state 值
    ({"allocation": True}, "布尔值"),                # 金额字段不能是布尔值
    ({"allocation": "not-a-number"}, "不是合法数值"),  # 金额字段非法数值
])
def test_invariants_rejected(over, match):
    with pytest.raises(ValueError, match=match):
        _c(**over)


def test_state_string_normalization():
    """Test that string state values are normalized to StrategyState enum."""
    c = _c(state="ACTIVE")
    assert c.state is StrategyState.ACTIVE
    assert isinstance(c.state, StrategyState)
    # Verify subsequent pause() works with normalized state
    p = c.pause(_NOW)
    assert p.state is StrategyState.PAUSED


def test_pause_resume_roundtrip():
    c = _c()
    p = c.pause(_NOW)
    assert p.state is StrategyState.PAUSED and p.version == 2 and c.state is StrategyState.ACTIVE
    r = p.resume(_NOW)
    assert r.state is StrategyState.ACTIVE and r.version == 3


@pytest.mark.parametrize("start, method", [
    (StrategyState.PAUSED, "pause"),        # 非 ACTIVE 不能 pause
    (StrategyState.ACTIVE, "resume"),       # 非 PAUSED 不能 resume
    (StrategyState.TERMINATED, "terminate"),# 终态不能再 terminate
    (StrategyState.EXPIRED, "terminate"),   # 终态不能再 terminate
    (StrategyState.EXPIRED, "expire"),      # 终态不能再 expire
    (StrategyState.TERMINATED, "expire"),   # 终态不能再 expire
    (StrategyState.TERMINATED, "pause"),    # 终态不能 pause
    (StrategyState.EXPIRED, "pause"),       # 终态不能 pause
    (StrategyState.TERMINATED, "resume"),   # 终态不能 resume
    (StrategyState.EXPIRED, "resume"),      # 终态不能 resume
])
def test_illegal_transitions(start, method):
    c = _c(state=start)
    with pytest.raises(IllegalStateTransition):
        getattr(c, method)(_NOW)


def test_terminate_and_expire_from_active_or_paused():
    assert _c().terminate(_NOW).state is StrategyState.TERMINATED
    assert _c(state=StrategyState.PAUSED).terminate(_NOW).state is StrategyState.TERMINATED
    assert _c().expire(_NOW).state is StrategyState.EXPIRED


def test_resume_blocked_when_expired():
    c = _c(state=StrategyState.PAUSED, expires_at=_NOW - timedelta(hours=1))
    with pytest.raises(IllegalStateTransition):
        c.resume(_NOW)


def test_with_allocation_respects_capacity():
    c = _c()
    assert c.with_allocation("400", _NOW).allocation == Decimal(400)
    with pytest.raises(ValueError, match="allocation"):
        c.with_allocation("600", _NOW)  # > capital_capacity


def test_is_expired():
    assert _c(expires_at=_NOW - timedelta(seconds=1)).is_expired(_NOW) is True
    assert _c(expires_at=None).is_expired(_NOW) is False
