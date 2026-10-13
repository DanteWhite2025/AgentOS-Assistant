"""净值快照存储 —— Protocol + 内存实现（PG 实现见 equity_pg_store，配置即切换）。

同一 (strategy_id, taken_at) 只保留一条：采样器重试/重复触发不应在曲线上造出重复点。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from app.strategy.equity import EquitySnapshot


class EquityStore(Protocol):
    async def record(self, snapshot: EquitySnapshot) -> None: ...

    async def record_many(self, snapshots: Iterable[EquitySnapshot]) -> None: ...

    async def list_range(
        self,
        user_id: str,
        agent_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        strategy_id: str | None = None,
    ) -> list[EquitySnapshot]: ...


def _matches(
    snapshot: EquitySnapshot,
    user_id: str,
    agent_id: str,
    start: datetime | None,
    end: datetime | None,
    strategy_id: str | None,
) -> bool:
    if snapshot.user_id != user_id or snapshot.agent_id != agent_id:
        return False
    if strategy_id is not None and snapshot.strategy_id != strategy_id:
        return False
    if start is not None and snapshot.taken_at < start:
        return False
    return not (end is not None and snapshot.taken_at > end)


class InMemoryEquityStore:
    """进程内存储；fake 模式与测试用。"""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, datetime], EquitySnapshot] = {}

    async def record(self, snapshot: EquitySnapshot) -> None:
        self._by_key[(snapshot.strategy_id, snapshot.taken_at)] = snapshot

    async def record_many(self, snapshots: Iterable[EquitySnapshot]) -> None:
        for snapshot in snapshots:
            await self.record(snapshot)

    async def list_range(
        self,
        user_id: str,
        agent_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        strategy_id: str | None = None,
    ) -> list[EquitySnapshot]:
        found = [
            s
            for s in self._by_key.values()
            if _matches(s, user_id, agent_id, start, end, strategy_id)
        ]
        return sorted(found, key=lambda s: s.taken_at)
