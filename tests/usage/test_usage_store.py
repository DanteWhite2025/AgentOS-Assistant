from datetime import UTC, datetime
from decimal import Decimal

from app.usage.models import UsageQuantity, UsageRecord
from app.usage.store import InMemoryUsageStore

T0 = datetime(2026, 9, 5, tzinfo=UTC)
T1 = datetime(2026, 9, 10, tzinfo=UTC)
T2 = datetime(2026, 9, 20, tzinfo=UTC)


def _record(
    key: str,
    *,
    user_id: str = "u1",
    occurred_at: datetime = T1,
    amount_usd: Decimal | None = Decimal("0.01"),
    kind: str = "llm",
    scene: str = "chat",
    provider: str = "anthropic",
    status: str = "ok",
    quantity: UsageQuantity | None = None,
) -> UsageRecord:
    return UsageRecord(
        dedup_key=key,
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        user_id=user_id,
        kind=kind,  # type: ignore[arg-type]
        provider=provider,
        scene=scene,
        quantity=quantity or UsageQuantity(input_tokens=100, output_tokens=10),
        amount_usd=amount_usd,
        status=status,  # type: ignore[arg-type]
    )


async def test_append_is_idempotent_on_dedup_key() -> None:
    """重复上报不产生新流水——语音挂断重试、前后台切换都会走到这里。"""
    store = InMemoryUsageStore()
    assert await store.append(_record("k1")) is True
    assert await store.append(_record("k1")) is False
    bill = await store.bill_for_user("u1", T0, T2)
    assert bill.total_amount_usd == Decimal("0.01")


async def test_bill_sums_only_records_inside_period() -> None:
    store = InMemoryUsageStore()
    await store.append(_record("in", occurred_at=T1))
    await store.append(_record("before", occurred_at=datetime(2026, 8, 1, tzinfo=UTC)))
    await store.append(_record("after", occurred_at=datetime(2026, 10, 1, tzinfo=UTC)))
    bill = await store.bill_for_user("u1", T0, T2)
    assert bill.total_amount_usd == Decimal("0.01")


async def test_bill_period_is_half_open() -> None:
    """[start, end) —— 月度账单相邻两月不能双算同一条。"""
    store = InMemoryUsageStore()
    await store.append(_record("edge", occurred_at=T2))
    assert (await store.bill_for_user("u1", T0, T2)).total_amount_usd == Decimal(0)
    next_day = datetime(2026, 9, 21, tzinfo=UTC)
    assert (await store.bill_for_user("u1", T2, next_day)).total_amount_usd == Decimal("0.01")


async def test_bill_isolates_users() -> None:
    store = InMemoryUsageStore()
    await store.append(_record("a", user_id="u1"))
    await store.append(_record("b", user_id="u2", amount_usd=Decimal(5)))
    assert (await store.bill_for_user("u1", T0, T2)).total_amount_usd == Decimal("0.01")


async def test_bill_counts_data_quality_flags() -> None:
    """usage_missing / unpriced 必须可见，否则拿到的是偏低的假数（spec §7.2）。"""
    store = InMemoryUsageStore()
    await store.append(_record("ok", status="ok"))
    await store.append(_record("miss", status="usage_missing", amount_usd=None))
    await store.append(_record("unpriced", status="unpriced", amount_usd=None))
    bill = await store.bill_for_user("u1", T0, T2)
    assert bill.usage_missing_count == 1
    assert bill.unpriced_count == 1
    assert bill.record_count == 3
    assert bill.total_amount_usd == Decimal("0.01")


async def test_bill_groups_by_kind_and_scene() -> None:
    store = InMemoryUsageStore()
    await store.append(_record("l", kind="llm", scene="chat", amount_usd=Decimal(1)))
    await store.append(
        _record(
            "v",
            kind="voice",
            scene="voice",
            amount_usd=Decimal(2),
            quantity=UsageQuantity(duration_seconds=Decimal(60)),
        )
    )
    bill = await store.bill_for_user("u1", T0, T2)
    assert bill.by_kind == {"llm": Decimal(1), "voice": Decimal(2)}
    assert bill.by_scene == {"chat": Decimal(1), "voice": Decimal(2)}


async def test_bill_sums_quantities_per_kind() -> None:
    """账单要答「用了多少量」，不只是多少钱。"""
    store = InMemoryUsageStore()
    await store.append(_record("a", quantity=UsageQuantity(input_tokens=100, output_tokens=10)))
    await store.append(_record("b", quantity=UsageQuantity(input_tokens=50, output_tokens=5)))
    bill = await store.bill_for_user("u1", T0, T2)
    assert bill.quantity_totals["input_tokens"] == 150
    assert bill.quantity_totals["output_tokens"] == 15


async def test_list_for_user_filters_and_paginates() -> None:
    store = InMemoryUsageStore()
    for i in range(5):
        await store.append(_record(f"k{i}"))
    await store.append(_record("voice", kind="voice", scene="voice"))
    page = await store.list_for_user("u1", T0, T2, limit=2, offset=0)
    assert len(page) == 2
    only_voice = await store.list_for_user("u1", T0, T2, kind="voice")
    assert len(only_voice) == 1


async def test_aggregate_groups_by_requested_dimensions() -> None:
    store = InMemoryUsageStore()
    await store.append(_record("a", scene="chat", provider="anthropic", amount_usd=Decimal(1)))
    await store.append(_record("b", scene="chat", provider="anthropic", amount_usd=Decimal(2)))
    await store.append(_record("c", scene="voice", provider="volc", amount_usd=Decimal(4)))
    rows = await store.aggregate(T0, T2, group_by=("scene", "provider"))
    by_key = {(r.keys["scene"], r.keys["provider"]): r.amount_usd for r in rows}
    assert by_key[("chat", "anthropic")] == Decimal(3)
    assert by_key[("voice", "volc")] == Decimal(4)


async def test_top_users_orders_by_amount_desc() -> None:
    store = InMemoryUsageStore()
    await store.append(_record("a", user_id="u1", amount_usd=Decimal(1)))
    await store.append(_record("b", user_id="u2", amount_usd=Decimal(9)))
    await store.append(_record("c", user_id="u3", amount_usd=Decimal(5)))
    assert await store.top_users(T0, T2, limit=2) == [("u2", Decimal(9)), ("u3", Decimal(5))]
