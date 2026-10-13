from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.strategy.container import IllegalStateTransition, StrategyState
from app.strategy.manager import (
    OverAllocationError,
    StrategyManager,
    StrategyNotFound,
    derive_mandate,
)
from app.strategy.store import InMemoryStrategyStore

_NOW = datetime(2026, 8, 8, tzinfo=UTC)


def _mgr():
    return StrategyManager(InMemoryStrategyStore())


async def _make(mgr, **over):
    base = {
        "user_id": "u1", "agent_id": "a1", "name": "s", "template_id": "tpl", "runtime_env": "paper",
        "allocation": "100", "capital_capacity": "500", "strategy_capacity": "1000",
        "max_leverage": "3", "allowed_assets": None, "expires_at": None, "now": _NOW,
    }
    base.update(over)
    return await mgr.create(**base)


async def test_create_generates_id_and_persists():
    mgr = _mgr()
    c = await _make(mgr, id_factory=lambda: "strat_fixed")
    assert c.strategy_id == "strat_fixed" and c.state is StrategyState.ACTIVE
    assert (await mgr.get_owned("strat_fixed", "u1", "a1")).strategy_id == "strat_fixed"


async def test_create_rejects_invariant_violation():
    mgr = _mgr()
    with pytest.raises(ValueError):
        await _make(mgr, allocation="600")  # > capital_capacity


async def test_create_aggregate_over_allocation_blocked():
    mgr = _mgr()
    await _make(mgr, allocation="400", id_factory=lambda: "s1")
    with pytest.raises(OverAllocationError):
        await _make(mgr, allocation="400", agent_capital="500", id_factory=lambda: "s2")
    # 400 + 400 > 500（Agent 可用本金）


async def test_create_aggregate_within_agent_capital_allowed():
    mgr = _mgr()
    await _make(mgr, allocation="200", id_factory=lambda: "s1")
    # 200 + 200 = 400 <= 500（Agent 可用本金），应通过并落库
    c = await _make(mgr, allocation="200", agent_capital="500", id_factory=lambda: "s2")
    assert c.strategy_id == "s2" and c.allocation == Decimal(200)


async def test_get_owned_missing_or_cross_agent_raises():
    mgr = _mgr()
    await _make(mgr, id_factory=lambda: "s1")
    with pytest.raises(StrategyNotFound):
        await mgr.get_owned("nope", "u1", "a1")
    with pytest.raises(StrategyNotFound):
        await mgr.get_owned("s1", "u1", "a2")  # 越 agent


async def test_lifecycle_pause_resume_terminate_persist():
    mgr = _mgr()
    await _make(mgr, id_factory=lambda: "s1")
    assert (await mgr.pause("s1", "u1", "a1", _NOW)).state is StrategyState.PAUSED
    assert (await mgr.resume("s1", "u1", "a1", _NOW)).state is StrategyState.ACTIVE
    assert (await mgr.terminate("s1", "u1", "a1", _NOW)).state is StrategyState.TERMINATED


async def test_allocate_updates():
    mgr = _mgr()
    await _make(mgr, id_factory=lambda: "s1")
    assert (await mgr.allocate("s1", "u1", "a1", "250", _NOW)).allocation == Decimal(250)


async def test_allocate_rejects_non_active_strategy():
    mgr = _mgr()
    await _make(mgr, id_factory=lambda: "s1")
    await mgr.pause("s1", "u1", "a1", _NOW)
    with pytest.raises(IllegalStateTransition):
        await mgr.allocate("s1", "u1", "a1", "250", _NOW)


async def test_allocate_with_agent_capital_within_limit():
    mgr = _mgr()
    await _make(mgr, allocation="200", id_factory=lambda: "s1")
    # Allocate s1 from 200 to 300, total with no other strategy = 300 <= 500 agent_capital
    result = await mgr.allocate("s1", "u1", "a1", "300", _NOW, agent_capital="500")
    assert result.allocation == Decimal(300)


async def test_allocate_with_agent_capital_exceeds_limit():
    mgr = _mgr()
    await _make(mgr, allocation="200", id_factory=lambda: "s1")
    await _make(mgr, allocation="300", id_factory=lambda: "s2")
    # s1=200, s2=300, total=500. Try to allocate s1 to 150 + s2=300 = 450 <= 500 (OK)
    result = await mgr.allocate("s1", "u1", "a1", "150", _NOW, agent_capital="500")
    assert result.allocation == Decimal(150)
    # Now try to allocate s1 to 250: 250 + 300 = 550 > 500 (should fail)
    with pytest.raises(OverAllocationError):
        await mgr.allocate("s1", "u1", "a1", "250", _NOW, agent_capital="500")


async def test_allocate_without_agent_capital_only_checks_capital_capacity():
    mgr = _mgr()
    # Create s1 with allocation=100, capital_capacity=500
    await _make(mgr, allocation="100", capital_capacity="500", id_factory=lambda: "s1")
    # Allocate to 400 (within capital_capacity) without agent_capital check
    result = await mgr.allocate("s1", "u1", "a1", "400", _NOW)
    assert result.allocation == Decimal(400)


async def test_ensure_tradable():
    mgr = _mgr()
    active = await _make(mgr, id_factory=lambda: "s1")
    mgr.ensure_tradable(active, _NOW)  # 不抛
    with pytest.raises(IllegalStateTransition):
        mgr.ensure_tradable(active.pause(_NOW), _NOW)
    with pytest.raises(IllegalStateTransition):
        mgr.ensure_tradable(
            await _make(mgr, expires_at=_NOW - timedelta(hours=1), id_factory=lambda: "s2"), _NOW
        )


def test_derive_mandate_maps_fields():
    from app.strategy.container import StrategyContainer

    c = StrategyContainer(
        strategy_id="s", user_id="u", agent_id="a", name="n", template_id="t",
        runtime_env="live", allocation="100", capital_capacity="500", strategy_capacity="500",
        max_leverage="3", allowed_assets=("BTC",), expires_at=_NOW, created_at=_NOW, updated_at=_NOW,
    )
    m = derive_mandate(c)
    assert m.max_leverage == Decimal(3)
    assert m.max_notional == Decimal(100)  # = allocation
    assert m.allowed_assets == ("BTC",)
    assert m.expires_at == _NOW
