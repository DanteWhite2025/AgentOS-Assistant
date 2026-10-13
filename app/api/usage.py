"""内部用量与成本查询。

只对内，不对外暴露。三条原则：
- 金额一律以**字符串**返回。JSON number 会被 float 化，吃掉 token 级精度
  （单价量级到 1e-8）。
- 账期左闭右开 [from, to)，相邻两个月不双算同一条流水。
- 每个响应都带 data_quality。报表不带质量信息就是在给假数：拿不到用量的调用
  被记成 usage_missing、匹配不到单价的记成 unpriced，两者金额都是空的，
  不把它们的条数说出来，看到的总额就是偏低的（spec §7.2）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_container
from app.container import Container
from app.usage.models import UsageRecord
from app.usage.store import UsageAggregate, UserBill

router = APIRouter(tags=["usage"])

Ctn = Annotated[Container, Depends(get_container)]
PeriodStart = Annotated[datetime, Query(alias="from")]
PeriodEnd = Annotated[datetime, Query(alias="to")]

#: 会拼进 SQL 的分组维度白名单。PostgresUsageStore.aggregate 也挡一道，
#: 这里挡是为了给 400 而不是 500。
_GROUPABLE = ("kind", "provider", "model", "scene", "user_id", "status")

_AMOUNT_EXPONENT = Decimal("0.00000001")

#: 单条流水上限。一期用明细实时聚合，量级足够；超了要上物化聚合表（二期）。
_SCAN_LIMIT = 1_000_000


def _amount(value: Decimal | None) -> str:
    """定点字符串。

    必须走 format(..., "f")：str(Decimal("0E-8")) 是 "0E-8"，零金额会变成科学
    计数法，和非零金额的格式不一致。app/api/voice.py 的 _decimal_text 同理。
    """
    return format((value or Decimal(0)).quantize(_AMOUNT_EXPONENT), "f")


def _store(container: Container) -> Any:
    store = getattr(container, "usage_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="用量存储未启用")
    return store


def _percentile(values: list[Decimal], ratio: float) -> str:
    if not values:
        return _amount(None)
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * ratio))
    return _amount(ordered[index])


def _mean(values: list[Decimal]) -> str:
    if not values:
        return _amount(None)
    return _amount(sum(values, start=Decimal(0)) / Decimal(len(values)))


def _unit_block(values: list[Decimal]) -> dict[str, Any]:
    return {
        "mean": _mean(values),
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
        "sample_size": len(values),
    }


def _bill_payload(bill: UserBill) -> dict[str, Any]:
    return {
        "user_id": bill.user_id,
        "period": {"from": bill.start.isoformat(), "to": bill.end.isoformat()},
        # 迟到数据按 occurred_at 写回原账期并重算，账单因此有版本（spec D8）。
        # 一期恒为 1，字段先给出来，调用方从第一天起就别缓存无版本号的账单。
        "revision": 1,
        "total_amount_usd": _amount(bill.total_amount_usd),
        "by_kind": {k: _amount(v) for k, v in bill.by_kind.items()},
        "by_scene": {k: _amount(v) for k, v in bill.by_scene.items()},
        "quantity_totals": {k: str(v) for k, v in bill.quantity_totals.items()},
        "data_quality": {
            "record_count": bill.record_count,
            "usage_missing_count": bill.usage_missing_count,
            "unpriced_count": bill.unpriced_count,
        },
    }


def _record_payload(record: UsageRecord) -> dict[str, Any]:
    q = record.quantity
    return {
        "dedup_key": record.dedup_key,
        "occurred_at": record.occurred_at.isoformat(),
        "kind": record.kind,
        "provider": record.provider,
        "model": record.model,
        "scene": record.scene,
        "session_id": record.session_id,
        "turn_id": record.turn_id,
        "status": record.status,
        "quantity": {
            "input_tokens": q.input_tokens,
            "output_tokens": q.output_tokens,
            "cache_read_tokens": q.cache_read_tokens,
            "cache_write_tokens": q.cache_write_tokens,
            "duration_seconds": None if q.duration_seconds is None else str(q.duration_seconds),
            "call_count": q.call_count,
        },
        # 可复算的三件套：单价版本、单价快照、汇率快照（spec G3）
        "price_version": record.price_version,
        "unit_price_snapshot": record.unit_price_snapshot,
        "amount_original": (
            None if record.amount_original is None else _amount(record.amount_original)
        ),
        "currency_original": record.currency_original,
        "fx_rate_snapshot": (
            None if record.fx_rate_snapshot is None else str(record.fx_rate_snapshot)
        ),
        "amount_usd": None if record.amount_usd is None else _amount(record.amount_usd),
    }


def _aggregate_payload(rows: list[UsageAggregate]) -> list[dict[str, Any]]:
    return [
        {**row.keys, "amount_usd": _amount(row.amount_usd), "record_count": row.record_count}
        for row in rows
    ]


@router.get("/users/{user_id}/bill", summary="单用户账期账单（用了多少量、折算多少钱）")
async def get_user_bill(
    user_id: str,
    container: Ctn,
    from_: PeriodStart,
    to: PeriodEnd,
) -> dict[str, Any]:
    bill = await _store(container).bill_for_user(user_id, from_, to)
    return _bill_payload(bill)


@router.get("/users/{user_id}/ledger", summary="单用户流水明细（可下钻复算）")
async def get_user_ledger(
    user_id: str,
    container: Ctn,
    from_: PeriodStart,
    to: PeriodEnd,
    kind: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    rows = await _store(container).list_for_user(
        user_id, from_, to, kind=kind, limit=limit, offset=offset
    )
    return {
        "items": [_record_payload(r) for r in rows],
        "limit": limit,
        "offset": offset,
    }


@router.get("/summary", summary="按维度聚合的成本汇总")
async def get_summary(
    container: Ctn,
    from_: PeriodStart,
    to: PeriodEnd,
    group_by: Annotated[str, Query()] = "scene",
) -> dict[str, Any]:
    dims = tuple(d.strip() for d in group_by.split(",") if d.strip())
    unknown = [d for d in dims if d not in _GROUPABLE]
    if unknown:
        raise HTTPException(status_code=400, detail=f"不支持的 group_by 维度: {unknown}")
    rows = await _store(container).aggregate(from_, to, group_by=dims)
    return {"group_by": list(dims), "rows": _aggregate_payload(rows)}


@router.get("/unit-economics", summary="单位经济学：每分钟通话/每次聊天/每次分析多少钱")
async def get_unit_economics(
    container: Ctn,
    from_: PeriodStart,
    to: PeriodEnd,
) -> dict[str, Any]:
    """回答甲方的三个问题。

    「一次聊天」按 turn_id 归并——一轮里的多次 LLM 调用与多次工具调用是一次聊天，
    分开算出来的是「一次 LLM 调用多少钱」，不是甲方问的东西（spec §6.2.0）。
    """
    records: list[UsageRecord] = await _store(container).list_in_period(
        from_, to, limit=_SCAN_LIMIT
    )

    voice_per_minute: list[Decimal] = []
    analysis_per_call: list[Decimal] = []
    chat_by_turn: dict[str, Decimal] = {}
    missing = 0

    for record in records:
        if record.status == "usage_missing":
            missing += 1
        amount = record.amount_usd or Decimal(0)
        if record.kind == "voice" and record.quantity.duration_seconds:
            minutes = record.quantity.duration_seconds / Decimal(60)
            if minutes > 0:
                voice_per_minute.append(amount / minutes)
        elif record.scene == "analysis":
            analysis_per_call.append(amount)
        elif record.turn_id is not None:
            chat_by_turn[record.turn_id] = chat_by_turn.get(record.turn_id, Decimal(0)) + amount

    total = len(records)
    return {
        "period": {"from": from_.isoformat(), "to": to.isoformat()},
        "units": {
            "voice_per_minute": _unit_block(voice_per_minute),
            "chat_per_turn": _unit_block(list(chat_by_turn.values())),
            "analysis_per_call": _unit_block(analysis_per_call),
        },
        "data_quality": {
            "record_count": total,
            "usage_missing_count": missing,
            "usage_missing_ratio": (str(round(missing / total, 4)) if total else "0"),
        },
    }


@router.get("/top-users", summary="用量排行：识别高消耗用户")
async def get_top_users(
    container: Ctn,
    from_: PeriodStart,
    to: PeriodEnd,
    limit: Annotated[int, Query(ge=1, le=200)] = 10,
) -> dict[str, Any]:
    rows = await _store(container).top_users(from_, to, limit=limit)
    return {"rows": [{"user_id": uid, "amount_usd": _amount(amount)} for uid, amount in rows]}
