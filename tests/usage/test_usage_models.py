from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.usage.models import (
    UsageQuantity,
    UsageRecord,
    api_dedup_key,
    llm_dedup_key,
    voice_dedup_key,
)


def test_dedup_keys_are_stable_and_namespaced() -> None:
    """三类 kind 的 dedup_key 口径一次定死，跨版本不得改（spec §5.1）。"""
    assert llm_dedup_key("trc_abc", 0) == "llm:trc_abc:0"
    assert llm_dedup_key("trc_abc", 2) == "llm:trc_abc:2"
    assert voice_dedup_key("sess_x") == "voice:sess_x"
    assert api_dedup_key("trc_abc", "get_market", 1) == "api:trc_abc:get_market:1"


def test_dedup_keys_do_not_collide_across_kinds() -> None:
    keys = {llm_dedup_key("x", 1), voice_dedup_key("x"), api_dedup_key("x", "t", 1)}
    assert len(keys) == 3


def test_usage_record_is_immutable() -> None:
    """流水只追加不更新，用 frozen dataclass 从类型层面挡住原地改。"""
    record = UsageRecord(
        dedup_key="voice:sess_x",
        occurred_at=datetime(2026, 9, 10, tzinfo=UTC),
        recorded_at=datetime(2026, 9, 10, tzinfo=UTC),
        user_id="u1",
        kind="voice",
        provider="volc",
        scene="voice",
        quantity=UsageQuantity(duration_seconds=Decimal("61.5")),
    )
    with pytest.raises(FrozenInstanceError):
        record.user_id = "u2"  # type: ignore[misc]


def test_usage_quantity_defaults_are_all_none() -> None:
    """量纲字段默认 None 而非 0——0 表示「真的是零」，None 表示「没这个量纲」。"""
    q = UsageQuantity()
    assert q.input_tokens is None
    assert q.output_tokens is None
    assert q.cache_read_tokens is None
    assert q.cache_write_tokens is None
    assert q.duration_seconds is None
    assert q.call_count is None


def test_usage_record_optional_fields_default_to_none_and_status_ok() -> None:
    record = UsageRecord(
        dedup_key="llm:trc:0",
        occurred_at=datetime(2026, 9, 10, tzinfo=UTC),
        recorded_at=datetime(2026, 9, 10, tzinfo=UTC),
        user_id="u1",
        kind="llm",
        provider="anthropic",
        scene="chat",
        quantity=UsageQuantity(input_tokens=100, output_tokens=20),
    )
    assert record.status == "ok"
    assert record.amount_usd is None
    assert record.turn_id is None
    assert record.unit_price_snapshot is None
