import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.usage.models import UsageQuantity, UsageRecord
from app.usage.pg_store import PostgresUsageStore

T0 = datetime(2026, 9, 1, tzinfo=UTC)
T1 = datetime(2026, 9, 10, tzinfo=UTC)
T2 = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
async def store() -> AsyncIterator[PostgresUsageStore]:
    """每个测试一套独立的内存库。

    必须 dispose：aiosqlite 的连接是后台线程，不关会在事件循环关闭后回调，
    表现为别的测试上冒出 PytestUnhandledThreadExceptionWarning。
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    s = PostgresUsageStore(engine)
    await s.create_schema()
    try:
        yield s
    finally:
        await engine.dispose()


def _record(key: str, **kw: object) -> UsageRecord:
    defaults: dict[str, object] = {
        "dedup_key": key,
        "occurred_at": T1,
        "recorded_at": T1,
        "user_id": "u1",
        "kind": "llm",
        "provider": "anthropic",
        "scene": "chat",
        "quantity": UsageQuantity(input_tokens=1000, output_tokens=200),
        "amount_usd": Decimal("0.03"),
    }
    defaults.update(kw)
    return UsageRecord(**defaults)  # type: ignore[arg-type]


async def test_append_then_read_back_roundtrip(store: PostgresUsageStore) -> None:
    await store.append(
        _record(
            "k1",
            model="claude-opus-5",
            turn_id="trc_1",
            session_id="sess_1",
            agent_id="agent_1",
            price_version="v1",
            unit_price_snapshot={"input_tokens": "15"},
            amount_original=Decimal("0.03"),
            currency_original="USD",
        )
    )
    rows = await store.list_for_user("u1", T0, T2)
    assert len(rows) == 1
    row = rows[0]
    assert row.dedup_key == "k1"
    assert row.model == "claude-opus-5"
    assert row.turn_id == "trc_1"
    assert row.quantity.input_tokens == 1000
    assert row.quantity.output_tokens == 200
    assert row.unit_price_snapshot == {"input_tokens": "15"}
    assert row.amount_usd == Decimal("0.03")
    assert row.occurred_at.tzinfo is not None


async def test_amount_keeps_eight_decimal_precision(store: PostgresUsageStore) -> None:
    """回读必须还是 Decimal 且不丢精度——float 化会静默吃掉 token 级金额。"""
    await store.append(_record("tiny", amount_usd=Decimal("0.00000123")))
    row = (await store.list_for_user("u1", T0, T2))[0]
    assert isinstance(row.amount_usd, Decimal)
    assert row.amount_usd == Decimal("0.00000123")


async def test_duration_seconds_roundtrip_as_decimal(store: PostgresUsageStore) -> None:
    await store.append(
        _record(
            "v",
            kind="voice",
            scene="voice",
            provider="volc",
            quantity=UsageQuantity(duration_seconds=Decimal("61.500")),
        )
    )
    row = (await store.list_for_user("u1", T0, T2))[0]
    assert row.quantity.duration_seconds == Decimal("61.500")


async def test_duplicate_dedup_key_returns_false_and_inserts_once(
    store: PostgresUsageStore,
) -> None:
    assert await store.append(_record("dup")) is True
    assert await store.append(_record("dup")) is False
    assert len(await store.list_for_user("u1", T0, T2)) == 1


async def test_concurrent_duplicate_appends_insert_only_one(tmp_path: Path) -> None:
    """并发重复上报只能进一条。SELECT-then-INSERT 在这个用例下会漏成两条。

    必须用**文件型** sqlite：内存型 + StaticPool 下所有连接共用同一个物理连接，
    5 个 engine.begin() 实际落在同一个 sqlite 事务里，冲突的那几个 rollback 会把
    已成功的插入一起回滚——那是测试环境的产物，不是 PG 的行为。
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/usage.db")
    concurrent_store = PostgresUsageStore(engine)
    await concurrent_store.create_schema()
    try:
        results = await asyncio.gather(
            *[concurrent_store.append(_record("race")) for _ in range(5)],
            return_exceptions=True,
        )
        assert not [r for r in results if isinstance(r, Exception)]
        assert results.count(True) == 1
        assert len(await concurrent_store.list_for_user("u1", T0, T2)) == 1
    finally:
        await engine.dispose()


async def test_nullable_fields_survive_roundtrip(store: PostgresUsageStore) -> None:
    """usage_missing 的流水金额与量纲全空，回读不得变成 0。"""
    await store.append(
        _record("miss", status="usage_missing", amount_usd=None, quantity=UsageQuantity())
    )
    row = (await store.list_for_user("u1", T0, T2))[0]
    assert row.status == "usage_missing"
    assert row.amount_usd is None
    assert row.quantity.input_tokens is None
    assert row.unit_price_snapshot is None


async def test_bill_matches_in_memory_semantics(store: PostgresUsageStore) -> None:
    await store.append(_record("a", amount_usd=Decimal(1)))
    await store.append(_record("b", kind="voice", scene="voice", amount_usd=Decimal(2)))
    await store.append(_record("c", status="usage_missing", amount_usd=None))
    bill = await store.bill_for_user("u1", T0, T2)
    assert bill.total_amount_usd == Decimal(3)
    assert bill.by_kind == {"llm": Decimal(1), "voice": Decimal(2)}
    assert bill.usage_missing_count == 1
    assert bill.record_count == 3


async def test_bill_period_is_half_open(store: PostgresUsageStore) -> None:
    await store.append(_record("edge", occurred_at=T2))
    assert (await store.bill_for_user("u1", T0, T2)).record_count == 0


async def test_aggregate_and_top_users(store: PostgresUsageStore) -> None:
    await store.append(_record("a", user_id="u1", amount_usd=Decimal(1)))
    await store.append(_record("b", user_id="u2", amount_usd=Decimal(9), scene="voice"))
    rows = await store.aggregate(T0, T2, group_by=("scene",))
    assert {r.keys["scene"]: r.amount_usd for r in rows} == {
        "chat": Decimal(1),
        "voice": Decimal(9),
    }
    assert await store.top_users(T0, T2, limit=1) == [("u2", Decimal(9))]


async def test_aggregate_rejects_unknown_group_by(store: PostgresUsageStore) -> None:
    """group_by 直接拼进 SQL，必须白名单校验，不能信外部字符串。"""
    with pytest.raises(ValueError, match="group_by"):
        await store.aggregate(T0, T2, group_by=("user_id; DROP TABLE usage_ledger",))


async def test_non_unique_integrity_error_is_raised_not_swallowed(
    store: PostgresUsageStore,
) -> None:
    """只有唯一冲突才算「重复上报」。

    NOT NULL 违反这类完整性错误必须抛出去：一并当成重复会让流水静默丢失，
    而调用方还以为写成功了（spec §7.2 的静默漏数）。
    """
    with pytest.raises(IntegrityError):
        await store.append(_record("bad", user_id=None))
