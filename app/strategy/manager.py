"""策略容器编排 —— 创建/查询/生命周期/额度 + 派生 mandate + 执行前预检。

红线：user_id/agent_id 由调用方（token/会话）派生；账户由 Agent 维度发现，策略不建 Core 账户。
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.policies.mandate import AgentMandate
from app.strategy.container import IllegalStateTransition, StrategyContainer, StrategyState
from app.strategy.store import StrategyStore


class StrategyNotFound(Exception):
    """策略不存在或不属于该 user/agent（对应端点 404）。"""


class OverAllocationError(Exception):
    """该 Agent 下 ACTIVE 策略额度之和超过 Agent 可用本金（对应端点 409）。"""


def _new_id() -> str:
    return "strat_" + secrets.token_hex(12)


def derive_mandate(container: StrategyContainer) -> AgentMandate:
    """从策略配置派生 AgentMandate，用于每单 runtime 预检（Core 仍权威）。"""
    return AgentMandate(
        max_notional=container.allocation,
        max_leverage=container.max_leverage,
        allowed_assets=container.allowed_assets,
        allowed_markets=None,
        expires_at=container.expires_at,
        require_confirmation=True,
        take_profit_pct=container.take_profit_pct,
        stop_loss_pct=container.stop_loss_pct,
    )


class StrategyManager:
    def __init__(self, store: StrategyStore) -> None:
        self._store = store

    async def create(
        self,
        *,
        user_id: str,
        agent_id: str,
        name: str,
        template_id: str,
        runtime_env: str,
        allocation: Any,
        capital_capacity: Any,
        strategy_capacity: Any,
        max_leverage: Any,
        allowed_assets: tuple[str, ...] | None,
        expires_at: datetime | None,
        now: datetime,
        take_profit_pct: Any = None,
        stop_loss_pct: Any = None,
        agent_capital: Any = None,
        id_factory: Callable[[], str] | None = None,
    ) -> StrategyContainer:
        container = StrategyContainer(
            strategy_id=(id_factory or _new_id)(),
            user_id=user_id,
            agent_id=agent_id,
            name=name,
            template_id=template_id,
            runtime_env=runtime_env,
            allocation=allocation,
            capital_capacity=capital_capacity,
            strategy_capacity=strategy_capacity,
            max_leverage=max_leverage,
            allowed_assets=allowed_assets,
            expires_at=expires_at,
            take_profit_pct=take_profit_pct,
            stop_loss_pct=stop_loss_pct,
            created_at=now,
            updated_at=now,
        )
        if agent_capital is not None:
            cap = Decimal(str(agent_capital))
            existing = await self._store.list_by_agent(user_id, agent_id)
            active_sum = sum(
                (s.allocation for s in existing if s.state is StrategyState.ACTIVE), Decimal(0)
            )
            if active_sum + container.allocation > cap:
                raise OverAllocationError(
                    f"额度之和 {active_sum + container.allocation} 超过 Agent 可用本金 {cap}"
                )
        await self._store.create(container)
        return container

    async def get_owned(
        self, strategy_id: str, user_id: str, agent_id: str
    ) -> StrategyContainer:
        s = await self._store.get(strategy_id)
        if s is None or s.user_id != user_id or s.agent_id != agent_id:
            raise StrategyNotFound(strategy_id)
        return s

    async def list(self, user_id: str, agent_id: str) -> list[StrategyContainer]:
        return await self._store.list_by_agent(user_id, agent_id)

    async def _transition(
        self, strategy_id: str, user_id: str, agent_id: str, method: str, now: datetime
    ) -> StrategyContainer:
        s = await self.get_owned(strategy_id, user_id, agent_id)
        updated = getattr(s, method)(now)
        await self._store.update(updated)
        return updated

    async def pause(self, sid: str, user_id: str, agent_id: str, now: datetime) -> StrategyContainer:
        return await self._transition(sid, user_id, agent_id, "pause", now)

    async def resume(self, sid: str, user_id: str, agent_id: str, now: datetime) -> StrategyContainer:
        return await self._transition(sid, user_id, agent_id, "resume", now)

    async def terminate(self, sid: str, user_id: str, agent_id: str, now: datetime) -> StrategyContainer:
        return await self._transition(sid, user_id, agent_id, "terminate", now)

    async def allocate(
        self, sid: str, user_id: str, agent_id: str, value: Any, now: datetime,
        agent_capital: Any = None,
    ) -> StrategyContainer:
        s = await self.get_owned(sid, user_id, agent_id)
        if s.state is not StrategyState.ACTIVE:
            raise IllegalStateTransition(f"仅 ACTIVE 策略可调整额度，当前 {s.state}")
        updated = s.with_allocation(value, now)
        if agent_capital is not None:
            cap = Decimal(str(agent_capital))
            existing = await self._store.list_by_agent(user_id, agent_id)
            # Sum all ACTIVE strategies except current one's old allocation, plus new allocation
            active_sum = sum(
                (st.allocation for st in existing if st.state is StrategyState.ACTIVE and st.strategy_id != sid),
                Decimal(0),
            ) + updated.allocation
            if active_sum > cap:
                raise OverAllocationError(
                    f"额度之和 {active_sum} 超过 Agent 可用本金 {cap}"
                )
        await self._store.update(updated)
        return updated

    def ensure_tradable(self, container: StrategyContainer, now: datetime) -> None:
        if container.state is not StrategyState.ACTIVE:
            raise IllegalStateTransition(f"策略非 ACTIVE（当前 {container.state}），不可交易")
        if container.is_expired(now):
            raise IllegalStateTransition("策略已过期，不可交易")
