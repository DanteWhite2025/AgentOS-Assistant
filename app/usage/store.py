"""用量流水存储接口与内存实现。

账期一律左闭右开 [start, end)：相邻两个月的账单不能双算同一条流水。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.usage.models import UsageRecord

#: 参与「用了多少量」汇总的量纲字段。
_QUANTITY_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "duration_seconds",
    "call_count",
)


@dataclass(frozen=True)
class UsageAggregate:
    keys: dict[str, str]
    amount_usd: Decimal
    record_count: int


@dataclass(frozen=True)
class UserBill:
    """单用户账期账单。"""

    user_id: str
    start: datetime
    end: datetime
    total_amount_usd: Decimal
    record_count: int
    usage_missing_count: int
    unpriced_count: int
    by_kind: dict[str, Decimal] = field(default_factory=dict)
    by_scene: dict[str, Decimal] = field(default_factory=dict)
    quantity_totals: dict[str, Decimal] = field(default_factory=dict)


class UsageStore(Protocol):
    async def append(self, record: UsageRecord) -> bool: ...

    async def bill_for_user(self, user_id: str, start: datetime, end: datetime) -> UserBill: ...

    async def list_for_user(
        self,
        user_id: str,
        start: datetime,
        end: datetime,
        *,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[UsageRecord]: ...

    async def aggregate(
        self, start: datetime, end: datetime, *, group_by: Sequence[str]
    ) -> list[UsageAggregate]: ...

    async def list_in_period(
        self, start: datetime, end: datetime, *, limit: int = 100
    ) -> list[UsageRecord]: ...

    async def top_users(
        self, start: datetime, end: datetime, *, limit: int = 10
    ) -> list[tuple[str, Decimal]]: ...


def build_bill(
    user_id: str, start: datetime, end: datetime, records: Sequence[UsageRecord]
) -> UserBill:
    """把一批流水聚成账单。PG 实现回读后复用同一段逻辑，保证两种存储口径一致。"""
    total = Decimal(0)
    by_kind: dict[str, Decimal] = {}
    by_scene: dict[str, Decimal] = {}
    quantity_totals: dict[str, Decimal] = {}
    usage_missing = 0
    unpriced = 0

    for record in records:
        if record.status == "usage_missing":
            usage_missing += 1
        elif record.status == "unpriced":
            unpriced += 1
        amount = record.amount_usd or Decimal(0)
        total += amount
        by_kind[record.kind] = by_kind.get(record.kind, Decimal(0)) + amount
        by_scene[record.scene] = by_scene.get(record.scene, Decimal(0)) + amount
        for name in _QUANTITY_FIELDS:
            value = getattr(record.quantity, name)
            if value is None:
                continue
            quantity_totals[name] = quantity_totals.get(name, Decimal(0)) + Decimal(str(value))

    return UserBill(
        user_id=user_id,
        start=start,
        end=end,
        total_amount_usd=total,
        record_count=len(records),
        usage_missing_count=usage_missing,
        unpriced_count=unpriced,
        by_kind=by_kind,
        by_scene=by_scene,
        quantity_totals=quantity_totals,
    )


class InMemoryUsageStore:
    """进程内流水账本（自测与 fake 模式用）。"""

    def __init__(self) -> None:
        self._records: list[UsageRecord] = []
        self._keys: set[str] = set()

    def _in_period(self, record: UsageRecord, start: datetime, end: datetime) -> bool:
        return start <= record.occurred_at < end

    async def append(self, record: UsageRecord) -> bool:
        if record.dedup_key in self._keys:
            return False
        self._keys.add(record.dedup_key)
        self._records.append(record)
        return True

    async def bill_for_user(self, user_id: str, start: datetime, end: datetime) -> UserBill:
        rows = [
            r for r in self._records if r.user_id == user_id and self._in_period(r, start, end)
        ]
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
        rows = [
            r
            for r in self._records
            if r.user_id == user_id
            and self._in_period(r, start, end)
            and (kind is None or r.kind == kind)
        ]
        rows.sort(key=lambda r: r.occurred_at, reverse=True)
        return rows[offset : offset + limit]

    async def aggregate(
        self, start: datetime, end: datetime, *, group_by: Sequence[str]
    ) -> list[UsageAggregate]:
        buckets: dict[tuple[str, ...], list[UsageRecord]] = {}
        for record in self._records:
            if not self._in_period(record, start, end):
                continue
            key = tuple(str(getattr(record, dim)) for dim in group_by)
            buckets.setdefault(key, []).append(record)
        return [
            UsageAggregate(
                keys=dict(zip(group_by, key, strict=True)),
                amount_usd=sum((r.amount_usd or Decimal(0) for r in rows), start=Decimal(0)),
                record_count=len(rows),
            )
            for key, rows in buckets.items()
        ]

    async def list_in_period(
        self, start: datetime, end: datetime, *, limit: int = 100
    ) -> list[UsageRecord]:
        """账期内全部流水，跨用户。单位经济学要按 turn 归并，只能整批拿。"""
        rows = [r for r in self._records if self._in_period(r, start, end)]
        rows.sort(key=lambda r: r.occurred_at, reverse=True)
        return rows[:limit]

    async def top_users(
        self, start: datetime, end: datetime, *, limit: int = 10
    ) -> list[tuple[str, Decimal]]:
        totals: dict[str, Decimal] = {}
        for record in self._records:
            if not self._in_period(record, start, end):
                continue
            totals[record.user_id] = totals.get(record.user_id, Decimal(0)) + (
                record.amount_usd or Decimal(0)
            )
        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[:limit]
