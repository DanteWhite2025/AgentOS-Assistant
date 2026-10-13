"""历史对话保留期清理。

清理循环最危险的失效模式是「静默失效」：某一轮硬失败被 except 吞成
「等下一轮」，而下一轮同样失败，于是保留期永远不生效、表无界增长。
所以这里既测正常删除，也测失败后循环是否还活着。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.history.purger import ConversationPurger
from app.history.store import InMemoryConversationStore

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _AlwaysLock:
    async def acquire(self, ttl_seconds: int) -> bool:
        return True


class _NeverLock:
    def __init__(self) -> None:
        self.ttls: list[int] = []

    async def acquire(self, ttl_seconds: int) -> bool:
        self.ttls.append(ttl_seconds)
        return False


async def _store_with(*ages_in_days: float) -> InMemoryConversationStore:
    store = InMemoryConversationStore()
    for i, age in enumerate(ages_in_days):
        await store.record_turn(f"sess_{i}", "u1", "问", "答", NOW - timedelta(days=age))
    return store


def _purger(store, lock=None, *, retention_days: int = 30) -> ConversationPurger:
    return ConversationPurger(
        store,
        lock or _AlwaysLock(),
        retention_days=retention_days,
        now=_Clock(),
    )


# ---------------------------------------------------------------- 单轮清理


async def test_purges_only_conversations_past_retention() -> None:
    store = await _store_with(45, 10)  # 一条 45 天前，一条 10 天前

    removed = await _purger(store).purge_once()

    assert removed == 1
    assert await store.get_conversation("sess_0") is None
    assert await store.get_conversation("sess_1") is not None


async def test_cutoff_uses_retention_days() -> None:
    store = await _store_with(8)

    assert await _purger(store, retention_days=30).purge_once() == 0
    assert await _purger(store, retention_days=7).purge_once() == 1


async def test_nothing_to_purge_is_not_an_error() -> None:
    """小时级间隔下绝大多数轮次都无事可做，这是设计而非异常。"""
    store = await _store_with(1)
    assert await _purger(store).purge_once() == 0


async def test_skips_when_lock_not_acquired() -> None:
    """多副本下只有拿到轮次锁的那个实例执行，其余跳过。"""
    store = await _store_with(45)
    lock = _NeverLock()

    assert await _purger(store, lock).purge_once() == 0
    assert await store.get_conversation("sess_0") is not None, "没拿到锁却删了数据"


async def test_lock_ttl_is_derived_from_interval() -> None:
    store = await _store_with()
    lock = _NeverLock()
    purger = ConversationPurger(
        store, lock, retention_days=30, now=_Clock(), interval_seconds=3600
    )

    await purger.purge_once()

    assert lock.ttls == [3570], "锁 TTL 应为 interval - 30s"


# ---------------------------------------------------------------- 循环韧性


async def test_loop_survives_a_failing_round() -> None:
    """单轮失败绝不能中断循环——那等于保留期从此静默失效。"""
    calls = {"n": 0}

    class _Flaky(InMemoryConversationStore):
        async def purge_before(self, cutoff):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("pg 抖了")
            return 0

    purger = ConversationPurger(_Flaky(), _AlwaysLock(), retention_days=30, now=_Clock())
    task = asyncio.create_task(purger.run_forever(0.001))
    try:
        while calls["n"] < 2:
            await asyncio.sleep(0.001)
    finally:
        task.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await task

    assert calls["n"] >= 2, "第一轮失败后循环没有继续"


async def test_run_forever_is_cancellable() -> None:
    """容器关停要能干净地停下来，不能把 CancelledError 当普通失败咽掉。"""
    purger = _purger(await _store_with())
    task = asyncio.create_task(purger.run_forever(3600))
    await asyncio.sleep(0)
    task.cancel()

    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task.done()
