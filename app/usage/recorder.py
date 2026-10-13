"""记账门面。三条业务链路只调这里，不直接碰 store 与 pricing。

**这三个方法永远不抛异常**：记账是旁路，主链路不能因为它挂掉而失败
（spec §6.3）。代价是漏记会变静默，所以 failure_count 必须被监控。

本模块不导入 app.agents.* —— token 数以 int | None 传入，保持 app/usage
对业务包零依赖。
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from app.usage.fx import FxTable, convert_to_usd
from app.usage.models import (
    UsageKind,
    UsageQuantity,
    UsageRecord,
    UsageStatus,
    api_dedup_key,
    llm_dedup_key,
    voice_dedup_key,
)
from app.usage.pricing import PriceBook, price_quantity
from app.usage.store import UsageStore

logger = logging.getLogger(__name__)

#: user_id 缺失时的归属账户。不丢弃这笔成本，让它在报表里可见。
UNKNOWN_USER = "unknown"

_QUANTITY_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "duration_seconds",
    "call_count",
)


def _has_any_quantity(quantity: UsageQuantity) -> bool:
    return any(getattr(quantity, name) is not None for name in _QUANTITY_FIELDS)


class UsageRecorder:
    def __init__(self, store: UsageStore, price_book: PriceBook, fx_table: FxTable) -> None:
        self._store = store
        self._price_book = price_book
        self._fx_table = fx_table
        self._failure_count = 0

    @property
    def failure_count(self) -> int:
        """记账失败次数。为 0 才说明流水是完整的。"""
        return self._failure_count

    async def _append(
        self,
        *,
        dedup_key: str,
        kind: UsageKind,
        provider: str,
        scene: str,
        quantity: UsageQuantity,
        user_id: str,
        occurred_at: datetime,
        session_id: str | None = None,
        turn_id: str | None = None,
        agent_id: str | None = None,
        model: str | None = None,
    ) -> None:
        try:
            record = self._build(
                dedup_key=dedup_key,
                kind=kind,
                provider=provider,
                scene=scene,
                quantity=quantity,
                user_id=user_id,
                occurred_at=occurred_at,
                session_id=session_id,
                turn_id=turn_id,
                agent_id=agent_id,
                model=model,
            )
            await self._store.append(record)
        except Exception:
            self._failure_count += 1
            logger.exception(
                "usage_record_failed kind=%s provider=%s user_id=%s dedup_key=%s",
                kind,
                provider,
                user_id or UNKNOWN_USER,
                dedup_key,
            )

    def _build(
        self,
        *,
        dedup_key: str,
        kind: UsageKind,
        provider: str,
        scene: str,
        quantity: UsageQuantity,
        user_id: str,
        occurred_at: datetime,
        session_id: str | None,
        turn_id: str | None,
        agent_id: str | None,
        model: str | None,
    ) -> UsageRecord:
        status: UsageStatus = "ok"
        resolved_user = user_id.strip() or UNKNOWN_USER
        if resolved_user == UNKNOWN_USER:
            status = "usage_missing"

        amount_original: Decimal | None = None
        currency_original: str | None = None
        amount_usd: Decimal | None = None
        fx_snapshot: Decimal | None = None
        price_version: str | None = None
        snapshot: dict[str, str] | None = None

        if not _has_any_quantity(quantity):
            # 量全缺就不去查价——查了也没有意义，且会误报 unpriced。
            status = "usage_missing"
        else:
            priced = price_quantity(
                quantity,
                self._price_book,
                provider=provider,
                kind=kind,
                model=model,
                at=occurred_at,
            )
            if not priced.priced or priced.amount is None or priced.currency is None:
                if status == "ok":
                    status = "unpriced"
            else:
                amount_original = priced.amount
                currency_original = priced.currency
                price_version = priced.price_version
                snapshot = priced.snapshot
                rate = self._fx_table.rate_for(priced.currency, occurred_at)
                amount_usd = convert_to_usd(priced.amount, priced.currency, rate)
                fx_snapshot = None if rate is None else rate.rate_to_usd
                if amount_usd is None and status == "ok":
                    # 有原币金额但没汇率：原币留档，USD 留空，等汇率补齐后可重算。
                    status = "unpriced"

        return UsageRecord(
            dedup_key=dedup_key,
            occurred_at=occurred_at,
            recorded_at=occurred_at,
            user_id=resolved_user,
            kind=kind,
            provider=provider,
            scene=scene,
            quantity=quantity,
            session_id=session_id,
            turn_id=turn_id,
            agent_id=agent_id,
            model=model,
            price_version=price_version,
            unit_price_snapshot=snapshot,
            amount_original=amount_original,
            currency_original=currency_original,
            fx_rate_snapshot=fx_snapshot,
            amount_usd=amount_usd,
            status=status,
        )

    async def record_llm(
        self,
        *,
        turn_id: str,
        step_index: int,
        user_id: str,
        session_id: str | None,
        agent_id: str | None,
        scene: str,
        provider: str,
        model: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None,
        cache_write_tokens: int | None,
        occurred_at: datetime,
    ) -> None:
        await self._append(
            dedup_key=llm_dedup_key(turn_id, step_index),
            kind="llm",
            provider=provider,
            scene=scene,
            quantity=UsageQuantity(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            ),
            user_id=user_id,
            occurred_at=occurred_at,
            session_id=session_id,
            turn_id=turn_id,
            agent_id=agent_id,
            model=model,
        )

    async def record_voice(
        self,
        *,
        session_id: str,
        user_id: str,
        provider: str,
        duration_seconds: Decimal | None,
        audio_tokens: int | None,
        text_tokens: int | None,
        occurred_at: datetime,
    ) -> None:
        await self._append(
            dedup_key=voice_dedup_key(session_id),
            kind="voice",
            provider=provider,
            scene="voice",
            quantity=UsageQuantity(
                duration_seconds=duration_seconds,
                input_tokens=audio_tokens,
                output_tokens=text_tokens,
            ),
            user_id=user_id,
            occurred_at=occurred_at,
            session_id=session_id,
        )

    async def record_external_api(
        self,
        *,
        trace_id: str,
        tool_name: str,
        seq: int,
        user_id: str,
        session_id: str | None,
        turn_id: str | None,
        scene: str,
        provider: str,
        occurred_at: datetime,
    ) -> None:
        await self._append(
            dedup_key=api_dedup_key(trace_id, tool_name, seq),
            kind="external_api",
            provider=provider,
            scene=scene,
            quantity=UsageQuantity(call_count=1),
            user_id=user_id,
            occurred_at=occurred_at,
            session_id=session_id,
            turn_id=turn_id,
            model=tool_name,
        )
