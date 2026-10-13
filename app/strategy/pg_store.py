"""PostgreSQL 策略容器存储（SQLAlchemy 2.x async Core）。

配置 AGENT_DATABASE_URL 即自动启用，持久化策略定义、容量状态与版本变更。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.strategy.container import StrategyContainer, StrategyState

metadata = sa.MetaData()

strategies_table = sa.Table(
    "strategies",
    metadata,
    sa.Column("strategy_id", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(64), nullable=False, index=True),
    sa.Column("agent_id", sa.String(64), nullable=False, index=True),
    sa.Column("name", sa.String(128), nullable=False),
    sa.Column("template_id", sa.String(64), nullable=False),
    sa.Column("runtime_env", sa.String(32), nullable=False),
    sa.Column("allocation", sa.Numeric(scale=8), nullable=False),
    sa.Column("capital_capacity", sa.Numeric(scale=8), nullable=False),
    sa.Column("strategy_capacity", sa.Numeric(scale=8), nullable=False),
    sa.Column("max_leverage", sa.Numeric(scale=4), nullable=False),
    sa.Column("allowed_assets", sa.JSON, nullable=True),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("take_profit_pct", sa.Numeric(scale=8), nullable=True),
    sa.Column("stop_loss_pct", sa.Numeric(scale=8), nullable=True),
    sa.Column("state", sa.String(32), nullable=False, server_default="ACTIVE"),
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


def _to_decimal(val: Any) -> Decimal:
    """从数据库 row 中提取 Decimal，消除科学计数法干扰。"""
    if val is None:
        return Decimal(0)
    s = f"{val:f}" if isinstance(val, (int, float, Decimal)) else str(val)
    if "E" in s or "e" in s:
        s = f"{Decimal(s):f}"
    return Decimal(s)


def _opt_decimal(val: Any) -> Decimal | None:
    """可空数值列：NULL 保持 None，不落成 0（止盈止损的 0 意味着立刻平仓）。"""
    return None if val is None else _to_decimal(val)


class PostgresStrategyStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_schema(self) -> None:
        """建表（MVP 用；生产改 Alembic 迁移）。"""
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    def _row_to_container(self, row: Any) -> StrategyContainer:
        allowed_assets = (
            tuple(row.allowed_assets) if row.allowed_assets is not None else None
        )
        return StrategyContainer(
            strategy_id=row.strategy_id,
            user_id=row.user_id,
            agent_id=row.agent_id,
            name=row.name,
            template_id=row.template_id,
            runtime_env=row.runtime_env,
            allocation=_to_decimal(row.allocation),
            capital_capacity=_to_decimal(row.capital_capacity),
            strategy_capacity=_to_decimal(row.strategy_capacity),
            max_leverage=_to_decimal(row.max_leverage),
            allowed_assets=allowed_assets,
            expires_at=row.expires_at,
            take_profit_pct=_opt_decimal(row.take_profit_pct),
            stop_loss_pct=_opt_decimal(row.stop_loss_pct),
            state=StrategyState(row.state),
            version=row.version,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def create(self, strategy: StrategyContainer) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(strategies_table).values(
                    strategy_id=strategy.strategy_id,
                    user_id=strategy.user_id,
                    agent_id=strategy.agent_id,
                    name=strategy.name,
                    template_id=strategy.template_id,
                    runtime_env=strategy.runtime_env,
                    allocation=strategy.allocation,
                    capital_capacity=strategy.capital_capacity,
                    strategy_capacity=strategy.strategy_capacity,
                    max_leverage=strategy.max_leverage,
                    allowed_assets=list(strategy.allowed_assets) if strategy.allowed_assets is not None else None,
                    expires_at=strategy.expires_at,
                    take_profit_pct=strategy.take_profit_pct,
                    stop_loss_pct=strategy.stop_loss_pct,
                    state=strategy.state.value,
                    version=strategy.version,
                    created_at=strategy.created_at,
                    updated_at=strategy.updated_at,
                )
            )

    async def get(self, strategy_id: str) -> StrategyContainer | None:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                sa.select(strategies_table).where(strategies_table.c.strategy_id == strategy_id)
            )
            row = result.fetchone()
        if row is None:
            return None
        return self._row_to_container(row)

    async def update(self, strategy: StrategyContainer) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.update(strategies_table)
                .where(strategies_table.c.strategy_id == strategy.strategy_id)
                .values(
                    name=strategy.name,
                    allocation=strategy.allocation,
                    capital_capacity=strategy.capital_capacity,
                    strategy_capacity=strategy.strategy_capacity,
                    max_leverage=strategy.max_leverage,
                    allowed_assets=list(strategy.allowed_assets) if strategy.allowed_assets is not None else None,
                    expires_at=strategy.expires_at,
                    take_profit_pct=strategy.take_profit_pct,
                    stop_loss_pct=strategy.stop_loss_pct,
                    state=strategy.state.value,
                    version=strategy.version,
                    updated_at=strategy.updated_at,
                )
            )

    async def delete(self, strategy_id: str) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.delete(strategies_table).where(strategies_table.c.strategy_id == strategy_id)
            )

    async def list_by_agent(self, user_id: str, agent_id: str) -> list[StrategyContainer]:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                sa.select(strategies_table)
                .where(
                    sa.and_(
                        strategies_table.c.user_id == user_id,
                        strategies_table.c.agent_id == agent_id,
                    )
                )
                .order_by(strategies_table.c.created_at)
            )
            rows = result.fetchall()
        return [self._row_to_container(row) for row in rows]

    async def list_by_user(self, user_id: str) -> list[StrategyContainer]:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                sa.select(strategies_table)
                .where(strategies_table.c.user_id == user_id)
                .order_by(strategies_table.c.created_at)
            )
            rows = result.fetchall()
        return [self._row_to_container(row) for row in rows]

    async def list_all(self) -> list[StrategyContainer]:
        """全量扫描；仅供净值采样器遍历所有归属方使用。"""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                sa.select(strategies_table).order_by(strategies_table.c.created_at)
            )
            rows = result.fetchall()
        return [self._row_to_container(row) for row in rows]
