"""PostgreSQL 净值快照存储（SQLAlchemy 2.x async Core）。

配置 AGENT_DATABASE_URL 即自动启用。写入按 (strategy_id, taken_at) 幂等 upsert，
避免采样重试在曲线上留下重复点。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.strategy.equity import EquitySnapshot

metadata = sa.MetaData()

equity_snapshots_table = sa.Table(
    "strategy_equity_snapshots",
    metadata,
    sa.Column("strategy_id", sa.String(64), primary_key=True),
    sa.Column("taken_at", sa.DateTime(timezone=True), primary_key=True),
    sa.Column("user_id", sa.String(64), nullable=False),
    sa.Column("agent_id", sa.String(64), nullable=False),
    sa.Column("equity", sa.Numeric(scale=8), nullable=False),
    sa.Column("realized_pnl", sa.Numeric(scale=8), nullable=True),
    sa.Index("ix_equity_owner_time", "user_id", "agent_id", "taken_at"),
)


def ensure_utc(value: datetime) -> datetime:
    """回读的时间统一成带时区。

    PG 驱动给的是 tz-aware，SQLite 驱动给的是 naive；不归一化后续窗口比较会直接抛
    TypeError。
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class PostgresEquityStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_schema(self) -> None:
        """建表（MVP 用；生产改 Alembic 迁移）。"""
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    def _row_to_snapshot(self, row: Any) -> EquitySnapshot:
        return EquitySnapshot(
            strategy_id=row.strategy_id,
            user_id=row.user_id,
            agent_id=row.agent_id,
            taken_at=ensure_utc(row.taken_at),
            equity=Decimal(str(row.equity)),
            realized_pnl=None if row.realized_pnl is None else Decimal(str(row.realized_pnl)),
        )

    async def record(self, snapshot: EquitySnapshot) -> None:
        await self.record_many([snapshot])

    async def record_many(self, snapshots: Iterable[EquitySnapshot]) -> None:
        rows = [
            {
                "strategy_id": s.strategy_id,
                "taken_at": s.taken_at,
                "user_id": s.user_id,
                "agent_id": s.agent_id,
                "equity": s.equity,
                "realized_pnl": s.realized_pnl,
            }
            for s in snapshots
        ]
        if not rows:
            return
        async with self._engine.begin() as conn:
            for row in rows:
                # 同键 upsert：先删后插，跨 PG/SQLite 方言一致
                await conn.execute(
                    sa.delete(equity_snapshots_table).where(
                        sa.and_(
                            equity_snapshots_table.c.strategy_id == row["strategy_id"],
                            equity_snapshots_table.c.taken_at == row["taken_at"],
                        )
                    )
                )
            await conn.execute(sa.insert(equity_snapshots_table), rows)

    async def list_range(
        self,
        user_id: str,
        agent_id: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        strategy_id: str | None = None,
    ) -> list[EquitySnapshot]:
        conditions = [
            equity_snapshots_table.c.user_id == user_id,
            equity_snapshots_table.c.agent_id == agent_id,
        ]
        if strategy_id is not None:
            conditions.append(equity_snapshots_table.c.strategy_id == strategy_id)
        if start is not None:
            conditions.append(equity_snapshots_table.c.taken_at >= start)
        if end is not None:
            conditions.append(equity_snapshots_table.c.taken_at <= end)

        async with self._engine.connect() as conn:
            result = await conn.execute(
                sa.select(equity_snapshots_table)
                .where(sa.and_(*conditions))
                .order_by(equity_snapshots_table.c.taken_at)
            )
            rows = result.fetchall()
        return [self._row_to_snapshot(row) for row in rows]
