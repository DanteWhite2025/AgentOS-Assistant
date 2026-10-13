"""用量流水领域模型。

流水**只追加**：更正靠写一条反向冲抵记录，不 UPDATE 历史行（spec §5.1）。
量纲保持原始单位（tokens / seconds / count），不存 billed_units——把时长单元与
token 单元相加会丢掉定价所需的结构（spec D10）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

UsageKind = Literal["llm", "voice", "external_api"]
UsageStatus = Literal["ok", "usage_missing", "duplicate", "unpriced"]


def llm_dedup_key(turn_id: str, step_index: int) -> str:
    """一轮内每步 LLM 调用一条流水。口径定死，改动会造成存量重复计费。"""
    return f"llm:{turn_id}:{step_index}"


def voice_dedup_key(session_id: str) -> str:
    """沿用语音既有的「按 session 幂等」口径（app/voice/usage.py 模块注释）。"""
    return f"voice:{session_id}"


def api_dedup_key(trace_id: str, tool_name: str, seq: int) -> str:
    """同一 trace 内同名工具可能被调多次，靠 seq 区分。"""
    return f"api:{trace_id}:{tool_name}:{seq}"


@dataclass(frozen=True)
class UsageQuantity:
    """原始量纲。

    默认 None 而非 0：None 表示「这类用量不适用」，0 表示「真的是零」。
    两者在报表里意义不同，不能混。
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    duration_seconds: Decimal | None = None
    call_count: int | None = None


@dataclass(frozen=True)
class UsageRecord:
    """一条用量流水。"""

    dedup_key: str
    occurred_at: datetime
    recorded_at: datetime
    user_id: str
    kind: UsageKind
    provider: str
    scene: str
    quantity: UsageQuantity
    session_id: str | None = None
    turn_id: str | None = None
    agent_id: str | None = None
    model: str | None = None
    price_version: str | None = None
    unit_price_snapshot: dict[str, Any] | None = None
    amount_original: Decimal | None = None
    currency_original: str | None = None
    fx_rate_snapshot: Decimal | None = None
    amount_usd: Decimal | None = None
    status: UsageStatus = "ok"
