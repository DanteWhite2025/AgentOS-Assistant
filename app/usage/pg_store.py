"""PostgreSQL 用量流水存储（SQLAlchemy 2.x async Core，直插不搞 ORM 关系）。

表结构与索引照 app/history/pg_store.py 的范式：模块级 MetaData + sa.Table。
金额列一律 Numeric(scale=8)、回读一律 Decimal(str(...))，照
app/strategy/equity_pg_store.py 的既有惯例——float 会静默吃掉 token 级金额。

幂等靠 dedup_key 的 UNIQUE 约束 + IntegrityError：
- 不用 PG 专有的 on_conflict_do_nothing（会破坏 sqlite 测试）；
- 也不用 news/pg_store 的 SELECT-then-INSERT（那依赖 RoundLock 单写者，
  用量记账没有这个保证，并发下会插两条 = 重复计费）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.usage.models import UsageQuantity, UsageRecord
from app.usage.store import UsageAggregate, UserBill, build_bill

metadata = sa.MetaData()

usage_ledger = sa.Table(
    "usage_ledger",
    metadata,
    # sqlite 只对 INTEGER PRIMARY KEY 自增，BIGINT 不自增；PG 侧流水量大仍要 BIGINT。
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    sa.Column("dedup_key", sa.String(128), nullable=False, unique=True),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("user_id", sa.String(64), nullable=False),
    sa.Column("session_id", sa.String(64), nullable=True),
    sa.Column("turn_id", sa.String(64), nullable=True),
    sa.Column("agent_id", sa.String(64), nullable=True),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("provider", sa.String(32), nullable=False),
    sa.Column("model", sa.String(64), nullable=True),
    sa.Column("scene", sa.String(32), nullable=False),
    sa.Column("input_tokens", sa.Integer, nullable=True),
    sa.Column("output_tokens", sa.Integer, nullable=True),
    sa.Column("cache_read_tokens", sa.Integer, nullable=True),
    sa.Column("cache_write_tokens", sa.Integer, nullable=True),
    sa.Column("duration_seconds", sa.Numeric(scale=3), nullable=True),
    sa.Column("call_count", sa.Integer, nullable=True),
    sa.Column("price_version", sa.String(32), nullable=True),
    sa.Column("unit_price_snapshot", sa.JSON, nullable=True),
    sa.Column("amount_original", sa.Numeric(scale=8), nullable=True),
    sa.Column("currency_original", sa.String(8), nullable=True),
    sa.Column("fx_rate_snapshot", sa.Numeric(scale=8), nullable=True),
    sa.Column("amount_usd", sa.Numeric(scale=8), nullable=True),
    sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
    # 单用户账期查询是主路径（spec §5.1）
    sa.Index("ix_usage_user_time", "user_id", "occurred_at"),
    sa.Index("ix_usage_time_kind", "occurred_at", "kind"),
    sa.Index("ix_usage_session", "session_id"),
)

def _is_unique_violation(exc: IntegrityError) -> bool:
    """只有唯一约束冲突才算「重复上报」。

    其它 IntegrityError（NOT NULL 违反、长度超限）必须抛出去。把它们一并当成
    重复，会让流水静默丢失且无人知晓——正是 spec §7.2 要防的静默漏数。
    """
    orig = exc.orig
    for attr in ("sqlstate", "pgcode"):
        code = getattr(orig, attr, None)
        if code:
            return str(code) == "23505"  # PG unique_violation
    return "UNIQUE constraint failed" in str(orig)


#: aggregate 的 group_by 会拼进 SQL，只允许这些列。
_GROUPABLE = frozenset({"kind", "provider", "model", "scene", "user_id", "status"})


def ensure_utc(value: datetime) -> datetime:
    """回读的时间统一成带时区（sqlite 会丢 tzinfo）。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


def _to_row(record: UsageRecord) -> dict[str, Any]:
    q = record.quantity
    return {
        "dedup_key": record.dedup_key,
        "occurred_at": record.occurred_at,
        "recorded_at": record.recorded_at,
        "user_id": record.user_id,
        "session_id": record.session_id,
        "turn_id": record.turn_id,
        "agent_id": record.agent_id,
        "kind": record.kind,
        "provider": record.provider,
        "model": record.model,
        "scene": record.scene,
        "input_tokens": q.input_tokens,
        "output_tokens": q.output_tokens,
        "cache_read_tokens": q.cache_read_tokens,
        "cache_write_tokens": q.cache_write_tokens,
        "duration_seconds": q.duration_seconds,
        "call_count": q.call_count,
        "price_version": record.price_version,
        "unit_price_snapshot": record.unit_price_snapshot,
        "amount_original": record.amount_original,
        "currency_original": record.currency_original,
        "fx_rate_snapshot": record.fx_rate_snapshot,
        "amount_usd": record.amount_usd,
        "status": record.status,
    }


def _from_row(row: Any) -> UsageRecord:
    snapshot = row.unit_price_snapshot
    if isinstance(snapshot, str):  # SQLite 把 JSON 列回读成字符串
        snapshot = json.loads(snapshot)
    return UsageRecord(
        dedup_key=row.dedup_key,
        occurred_at=ensure_utc(row.occurred_at),
        recorded_at=ensure_utc(row.recorded_at),
        user_id=row.user_id,
        session_id=row.session_id,
        turn_id=row.turn_id,
        agent_id=row.agent_id,
        kind=row.kind,
        provider=row.provider,
        model=row.model,
        scene=row.scene,
        quantity=UsageQuantity(
            input_tokens=_int_or_none(row.input_tokens),
            output_tokens=_int_or_none(row.output_tokens),
            cache_read_tokens=_int_or_none(row.cache_read_tokens),
            cache_write_tokens=_int_or_none(row.cache_write_tokens),
            duration_seconds=_decimal_or_none(row.duration_seconds),
            call_count=_int_or_none(row.call_count),
        ),
        price_version=row.price_version,
        unit_price_snapshot=snapshot,
        amount_original=_decimal_or_none(row.amount_original),
        currency_original=row.currency_original,
        fx_rate_snapshot=_decimal_or_none(row.fx_rate_snapshot),
        amount_usd=_decimal_or_none(row.amount_usd),
        status=row.status,
    )


class PostgresUsageStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_schema(self) -> None:
        """建表（MVP 用；生产改 Alembic 迁移）。"""
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def append(self, record: UsageRecord) -> bool:
        """写一条流水。返回 False 表示 dedup_key 已存在，本次没有新增。"""
        try:
            async with self._engine.begin() as conn:
                await conn.execute(sa.insert(usage_ledger).values(**_to_row(record)))
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise
            return False
        return True

    def _period(self, start: datetime, end: datetime) -> Any:
        return sa.and_(usage_ledger.c.occurred_at >= start, usage_ledger.c.occurred_at < end)

    async def bill_for_user(self, user_id: str, start: datetime, end: datetime) -> UserBill:
        rows = await self.list_for_user(user_id, start, end, limit=1_000_000)
        return build_bill(user_id, start, end, rows)

    async def list_for_user(
        self,
        user_id: str,
        start: datetime,
        end: datetime,
        *,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[UsageRecord]:
        query = (
            sa.select(usage_ledger)
            .where(usage_ledger.c.user_id == user_id, self._period(start, end))
            .order_by(usage_ledger.c.occurred_at.desc(), usage_ledger.c.id.desc())
            .limit(limit)
            .offset(offset)
        )
        if kind is not None:
            query = query.where(usage_ledger.c.kind == kind)
        async with self._engine.connect() as conn:
            result = await conn.execute(query)
            rows = result.fetchall()
        return [_from_row(row) for row in rows]

    async def list_in_period(
        self, start: datetime, end: datetime, *, limit: int = 100
    ) -> list[UsageRecord]:
        """账期内全部流水，跨用户。单位经济学要按 turn 归并，只能整批拿。"""
        query = (
            sa.select(usage_ledger)
            .where(self._period(start, end))
            .order_by(usage_ledger.c.occurred_at.desc(), usage_ledger.c.id.desc())
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(query)
            rows = result.fetchall()
        return [_from_row(row) for row in rows]

    async def aggregate(
        self, start: datetime, end: datetime, *, group_by: Sequence[str]
    ) -> list[UsageAggregate]:
        unknown = [dim for dim in group_by if dim not in _GROUPABLE]
        if unknown:
            raise ValueError(f"group_by 含不支持的维度: {unknown}")
        columns = [usage_ledger.c[dim] for dim in group_by]
        query = (
            sa.select(
                *columns,
                sa.func.sum(usage_ledger.c.amount_usd).label("amount"),
                sa.func.count().label("cnt"),
            )
            .where(self._period(start, end))
            .group_by(*columns)
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(query)
            rows = result.fetchall()
        return [
            UsageAggregate(
                keys={dim: str(row[i]) for i, dim in enumerate(group_by)},
                amount_usd=_decimal_or_none(row.amount) or Decimal(0),
                record_count=int(row.cnt),
            )
            for row in rows
        ]

    async def top_users(
        self, start: datetime, end: datetime, *, limit: int = 10
    ) -> list[tuple[str, Decimal]]:
        total = sa.func.sum(usage_ledger.c.amount_usd).label("amount")
        query = (
            sa.select(usage_ledger.c.user_id, total)
            .where(self._period(start, end))
            .group_by(usage_ledger.c.user_id)
            .order_by(total.desc())
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(query)
            rows = result.fetchall()
        return [(row.user_id, _decimal_or_none(row.amount) or Decimal(0)) for row in rows]
