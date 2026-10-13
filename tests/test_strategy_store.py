from datetime import UTC, datetime

from app.strategy.container import StrategyContainer
from app.strategy.store import InMemoryStrategyStore

_NOW = datetime(2026, 8, 8, tzinfo=UTC)


def _c(sid, user="u1", agent="a1"):
    return StrategyContainer(
        strategy_id=sid, user_id=user, agent_id=agent, name="s", template_id="tpl",
        runtime_env="paper", allocation="1", capital_capacity="10", strategy_capacity="10",
        max_leverage="2", created_at=_NOW, updated_at=_NOW,
    )


async def test_create_get_update_delete():
    store = InMemoryStrategyStore()
    await store.create(_c("strat_1"))
    got = await store.get("strat_1")
    assert got is not None and got.strategy_id == "strat_1"
    await store.update(got.pause(_NOW))
    assert (await store.get("strat_1")).state.value == "PAUSED"
    await store.delete("strat_1")
    assert await store.get("strat_1") is None


async def test_get_missing_returns_none():
    assert await InMemoryStrategyStore().get("nope") is None


async def test_list_by_agent_and_user_isolation():
    store = InMemoryStrategyStore()
    await store.create(_c("s1", user="u1", agent="a1"))
    await store.create(_c("s2", user="u1", agent="a1"))
    await store.create(_c("s3", user="u1", agent="a2"))  # 同用户，另一 agent
    await store.create(_c("s4", user="u2", agent="a1"))  # 另一用户
    by_agent = await store.list_by_agent("u1", "a1")
    assert {s.strategy_id for s in by_agent} == {"s1", "s2"}
    by_user = await store.list_by_user("u1")
    assert {s.strategy_id for s in by_user} == {"s1", "s2", "s3"}
