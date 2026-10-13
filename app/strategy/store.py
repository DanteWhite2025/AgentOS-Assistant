"""策略容器存储 —— Protocol + 内存实现（real 模式 PG 落库后续接，配置即切换）。"""

from __future__ import annotations

from typing import Protocol

from app.strategy.container import StrategyContainer


class StrategyStore(Protocol):
    async def create(self, strategy: StrategyContainer) -> None: ...
    async def get(self, strategy_id: str) -> StrategyContainer | None: ...
    async def update(self, strategy: StrategyContainer) -> None: ...
    async def delete(self, strategy_id: str) -> None: ...
    async def list_by_agent(self, user_id: str, agent_id: str) -> list[StrategyContainer]: ...
    async def list_by_user(self, user_id: str) -> list[StrategyContainer]: ...
    async def list_all(self) -> list[StrategyContainer]: ...


class InMemoryStrategyStore:
    """进程内存储；测试/MVP 用。不可变对象直接存引用。"""

    def __init__(self) -> None:
        self._by_id: dict[str, StrategyContainer] = {}

    async def create(self, strategy: StrategyContainer) -> None:
        self._by_id[strategy.strategy_id] = strategy

    async def get(self, strategy_id: str) -> StrategyContainer | None:
        return self._by_id.get(strategy_id)

    async def update(self, strategy: StrategyContainer) -> None:
        self._by_id[strategy.strategy_id] = strategy

    async def delete(self, strategy_id: str) -> None:
        self._by_id.pop(strategy_id, None)

    async def list_by_agent(self, user_id: str, agent_id: str) -> list[StrategyContainer]:
        return [
            s for s in self._by_id.values() if s.user_id == user_id and s.agent_id == agent_id
        ]

    async def list_by_user(self, user_id: str) -> list[StrategyContainer]:
        return [s for s in self._by_id.values() if s.user_id == user_id]

    async def list_all(self) -> list[StrategyContainer]:
        """全量扫描；仅供净值采样器遍历所有归属方使用。"""
        return list(self._by_id.values())
