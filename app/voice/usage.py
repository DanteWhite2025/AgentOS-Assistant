"""语音通话用量结算。

数据面直连意味着 Runtime 看不到音频流，用量只能由客户端在挂断时上报。因此结算
必须按 ``session_id`` 幂等：网络重试、前后台切换、用户连点挂断都不能重复扣费。

计费口径（暂由 Runtime 自持，待 Core 出正式计费接口后迁移）：
每 ``seconds_per_unit`` 秒计一个时长单元，每 ``tokens_per_unit`` 个 token 计一个
token 单元，均向上取整，两者相加即为本次通话的 billed_units。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import ceil
from typing import Protocol

DEFAULT_SECONDS_PER_UNIT = 60
DEFAULT_TOKENS_PER_UNIT = 1000


def compute_billed_units(
    duration_seconds: float,
    *,
    audio_tokens: int = 0,
    text_tokens: int = 0,
    seconds_per_unit: int = DEFAULT_SECONDS_PER_UNIT,
    tokens_per_unit: int = DEFAULT_TOKENS_PER_UNIT,
) -> Decimal:
    """时长单元 + token 单元，各自向上取整后相加。"""

    if seconds_per_unit <= 0 or tokens_per_unit <= 0:
        raise ValueError("billing unit sizes must be positive")
    duration = max(0.0, duration_seconds)
    tokens = max(0, audio_tokens) + max(0, text_tokens)
    return Decimal(ceil(duration / seconds_per_unit)) + Decimal(ceil(tokens / tokens_per_unit))


@dataclass(frozen=True)
class VoiceUsageRecord:
    """一次通话的用量流水（不可变）。"""

    session_id: str
    user_id: str
    provider: str
    duration_seconds: float
    audio_tokens: int
    text_tokens: int
    billed_units: Decimal
    recorded_at: datetime


@dataclass(frozen=True)
class VoiceUsageSettlement:
    """结算结果。``duplicate=True`` 表示这次上报没有产生新的扣减。"""

    record: VoiceUsageRecord
    duplicate: bool
    total_billed_units: Decimal
    remaining_units: Decimal | None = None


class VoiceUsageStore(Protocol):
    async def settle(self, record: VoiceUsageRecord) -> VoiceUsageSettlement: ...

    async def list_by_user(self, user_id: str) -> list[VoiceUsageRecord]: ...


class InMemoryVoiceUsageStore:
    """进程内用量账本（MVP / 自测用）。

    ``quota_units_per_user`` 为 0 表示不限额，此时 ``remaining_units`` 恒为 None。
    """

    def __init__(self, quota_units_per_user: int = 0) -> None:
        self._quota = max(0, quota_units_per_user)
        self._by_session: dict[str, VoiceUsageRecord] = {}
        self._by_user: dict[str, list[VoiceUsageRecord]] = {}

    def _total(self, user_id: str) -> Decimal:
        return sum(
            (item.billed_units for item in self._by_user.get(user_id, ())), start=Decimal(0)
        )

    def _remaining(self, total: Decimal) -> Decimal | None:
        if self._quota == 0:
            return None
        return max(Decimal(0), Decimal(self._quota) - total)

    async def settle(self, record: VoiceUsageRecord) -> VoiceUsageSettlement:
        existing = self._by_session.get(record.session_id)
        if existing is not None:
            total = self._total(existing.user_id)
            return VoiceUsageSettlement(
                record=existing,
                duplicate=True,
                total_billed_units=total,
                remaining_units=self._remaining(total),
            )
        self._by_session[record.session_id] = record
        self._by_user.setdefault(record.user_id, []).append(record)
        total = self._total(record.user_id)
        return VoiceUsageSettlement(
            record=record,
            duplicate=False,
            total_billed_units=total,
            remaining_units=self._remaining(total),
        )

    async def list_by_user(self, user_id: str) -> list[VoiceUsageRecord]:
        return list(self._by_user.get(user_id, ()))
