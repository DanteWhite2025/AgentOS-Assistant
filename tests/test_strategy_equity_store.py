"""净值快照存储 —— 内存实现与 PG 实现（SQLite in-memory 驱动）行为一致。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.strategy.equity import EquitySnapshot
from app.strategy.equity_store import InMemoryEquityStore

_T0 = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _snap(minutes, equity, sid="s1", user="u1", agent="a1", realized=None):
    return EquitySnapshot(
        strategy_id=sid,
        user_id=user,
        agent_id=agent,
        taken_at=_T0 + timedelta(minutes=minutes),
        equity=Decimal(equity),
        realized_pnl=Decimal(realized) if realized is not None else None,
    )


def _pg_store():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.strategy.equity_pg_store import PostgresEquityStore

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    return PostgresEquityStore(engine), engine


async def _make(kind):
    if kind == "memory":
        return InMemoryEquityStore(), None
    store, engine = _pg_store()
    await store.create_schema()
    return store, engine


_KINDS = ["memory", "pg"]


@pytest.mark.parametrize("kind", _KINDS)
async def test_record_and_list_range_sorted(kind):
    store, engine = await _make(kind)
    try:
        await store.record(_snap(20, "1200"))
        await store.record(_snap(0, "1000"))
        await store.record(_snap(10, "1100"))
        got = await store.list_range("u1", "a1")
        assert [s.equity for s in got] == [Decimal(1000), Decimal(1100), Decimal(1200)]
    finally:
        if engine:
            await engine.dispose()


@pytest.mark.parametrize("kind", _KINDS)
async def test_list_range_filters_window(kind):
    store, engine = await _make(kind)
    try:
        await store.record_many([_snap(0, "1000"), _snap(30, "1100"), _snap(90, "1200")])
        got = await store.list_range("u1", "a1", start=_T0 + timedelta(minutes=20))
        assert [s.equity for s in got] == [Decimal(1100), Decimal(1200)]
        got = await store.list_range(
            "u1", "a1", start=_T0 + timedelta(minutes=20), end=_T0 + timedelta(minutes=60)
        )
        assert [s.equity for s in got] == [Decimal(1100)]
    finally:
        if engine:
            await engine.dispose()


@pytest.mark.parametrize("kind", _KINDS)
async def test_list_range_filters_by_strategy(kind):
    store, engine = await _make(kind)
    try:
        await store.record_many([_snap(0, "1000", sid="s1"), _snap(0, "2000", sid="s2")])
        got = await store.list_range("u1", "a1", strategy_id="s2")
        assert [s.equity for s in got] == [Decimal(2000)]
    finally:
        if engine:
            await engine.dispose()


@pytest.mark.parametrize("kind", _KINDS)
async def test_list_range_isolates_other_agents(kind):
    store, engine = await _make(kind)
    try:
        await store.record_many(
            [_snap(0, "1000"), _snap(0, "9999", sid="s9", user="u1", agent="OTHER")]
        )
        assert [s.equity for s in await store.list_range("u1", "a1")] == [Decimal(1000)]
        assert await store.list_range("u1", "nobody") == []
    finally:
        if engine:
            await engine.dispose()


@pytest.mark.parametrize("kind", _KINDS)
async def test_record_same_timestamp_twice_is_idempotent(kind):
    """采样器重复触发不应把同一时刻写成两条，否则曲线出现台阶。"""
    store, engine = await _make(kind)
    try:
        await store.record(_snap(0, "1000"))
        await store.record(_snap(0, "1050"))
        got = await store.list_range("u1", "a1")
        assert [s.equity for s in got] == [Decimal(1050)]
    finally:
        if engine:
            await engine.dispose()


@pytest.mark.parametrize("kind", _KINDS)
async def test_realized_pnl_roundtrip(kind):
    store, engine = await _make(kind)
    try:
        await store.record_many([_snap(0, "1000", realized="12.5"), _snap(10, "1100")])
        got = await store.list_range("u1", "a1")
        assert got[0].realized_pnl == Decimal("12.5")
        assert got[1].realized_pnl is None
    finally:
        if engine:
            await engine.dispose()


@pytest.mark.parametrize("kind", _KINDS)
async def test_record_many_empty_is_noop(kind):
    store, engine = await _make(kind)
    try:
        await store.record_many([])
        assert await store.list_range("u1", "a1") == []
    finally:
        if engine:
            await engine.dispose()
