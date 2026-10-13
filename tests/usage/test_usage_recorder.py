from datetime import UTC, date, datetime
from decimal import Decimal

from app.usage.fx import FxRate, FxTable
from app.usage.models import UsageRecord
from app.usage.pricing import PriceBook, PriceEntry
from app.usage.recorder import UsageRecorder
from app.usage.store import InMemoryUsageStore

NOW = datetime(2026, 9, 10, tzinfo=UTC)
END = datetime(2026, 9, 11, tzinfo=UTC)
FROM = datetime(2026, 1, 1, tzinfo=UTC)


def _llm_book() -> PriceBook:
    return PriceBook(
        [
            PriceEntry("anthropic", "llm", "input_tokens", Decimal(15), "USD", FROM, "v1"),
            PriceEntry("anthropic", "llm", "output_tokens", Decimal(75), "USD", FROM, "v1"),
        ]
    )


def _volc_book() -> PriceBook:
    return PriceBook(
        [PriceEntry("volc", "voice", "duration_minute", Decimal("0.6"), "CNY", FROM, "v1")]
    )


def _cny_table() -> FxTable:
    return FxTable(
        [
            FxRate(
                rate_date=date(2026, 9, 9),
                currency="CNY",
                rate_to_usd=Decimal("0.14"),
                source="ecb",
            )
        ]
    )


async def test_record_llm_prices_and_stores() -> None:
    store = InMemoryUsageStore()
    recorder = UsageRecorder(store, _llm_book(), FxTable([]))
    await recorder.record_llm(
        turn_id="turn_1",
        step_index=0,
        user_id="u1",
        session_id="s1",
        agent_id="a1",
        scene="chat",
        provider="anthropic",
        model="claude-opus-5",
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=0,
        cache_write_tokens=0,
        occurred_at=NOW,
    )
    rows = await store.list_for_user("u1", NOW, END)
    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].amount_usd == Decimal("0.03000000")
    assert rows[0].turn_id == "turn_1"
    assert rows[0].dedup_key == "llm:turn_1:0"
    assert rows[0].price_version == "v1"


async def test_record_llm_with_no_usage_is_usage_missing_not_zero() -> None:
    """拿不到 token 数时照样落流水，标 usage_missing、金额留空。
    漏掉这条会让 LLM 成本被系统性低估（spec §7.2）。"""
    store = InMemoryUsageStore()
    recorder = UsageRecorder(store, _llm_book(), FxTable([]))
    await recorder.record_llm(
        turn_id="turn_1",
        step_index=0,
        user_id="u1",
        session_id="s1",
        agent_id=None,
        scene="chat",
        provider="anthropic",
        model="claude-opus-5",
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        occurred_at=NOW,
    )
    row = (await store.list_for_user("u1", NOW, END))[0]
    assert row.status == "usage_missing"
    assert row.amount_usd is None
    assert row.amount_original is None


async def test_record_llm_without_price_is_unpriced() -> None:
    store = InMemoryUsageStore()
    recorder = UsageRecorder(store, PriceBook([]), FxTable([]))
    await recorder.record_llm(
        turn_id="t",
        step_index=0,
        user_id="u1",
        session_id=None,
        agent_id=None,
        scene="chat",
        provider="anthropic",
        model="unknown-model",
        input_tokens=10,
        output_tokens=1,
        cache_read_tokens=None,
        cache_write_tokens=None,
        occurred_at=NOW,
    )
    row = (await store.list_for_user("u1", NOW, END))[0]
    assert row.status == "unpriced"
    assert row.amount_usd is None


async def test_record_voice_converts_cny_to_usd_with_snapshot() -> None:
    store = InMemoryUsageStore()
    recorder = UsageRecorder(store, _volc_book(), _cny_table())
    await recorder.record_voice(
        session_id="s1",
        user_id="u1",
        provider="volc",
        duration_seconds=Decimal(90),
        audio_tokens=None,
        text_tokens=None,
        occurred_at=NOW,
    )
    row = (await store.list_for_user("u1", NOW, END))[0]
    assert row.amount_original == Decimal("0.90000000")
    assert row.currency_original == "CNY"
    assert row.fx_rate_snapshot == Decimal("0.14")
    assert row.amount_usd == Decimal("0.12600000")
    assert row.dedup_key == "voice:s1"
    assert row.kind == "voice"


async def test_record_voice_without_fx_rate_is_unpriced() -> None:
    """有原币金额但没汇率时不能瞎折算，也不能当 0（spec §4.2）。"""
    store = InMemoryUsageStore()
    recorder = UsageRecorder(store, _volc_book(), FxTable([]))
    await recorder.record_voice(
        session_id="s1",
        user_id="u1",
        provider="volc",
        duration_seconds=Decimal(90),
        audio_tokens=None,
        text_tokens=None,
        occurred_at=NOW,
    )
    row = (await store.list_for_user("u1", NOW, END))[0]
    assert row.status == "unpriced"
    assert row.amount_usd is None
    # 原币金额仍留档，便于事后补算
    assert row.amount_original == Decimal("0.90000000")


async def test_record_external_api_counts_one_call() -> None:
    store = InMemoryUsageStore()
    book = PriceBook(
        [PriceEntry("tavily", "external_api", "call", Decimal("0.008"), "USD", FROM, "v1")]
    )
    recorder = UsageRecorder(store, book, FxTable([]))
    await recorder.record_external_api(
        trace_id="trc_1",
        tool_name="web_search",
        seq=0,
        user_id="u1",
        session_id="s1",
        turn_id="turn_1",
        scene="chat",
        provider="tavily",
        occurred_at=NOW,
    )
    row = (await store.list_for_user("u1", NOW, END))[0]
    assert row.quantity.call_count == 1
    assert row.amount_usd == Decimal("0.00800000")
    assert row.dedup_key == "api:trc_1:web_search:0"


async def test_store_failure_never_propagates_but_is_counted() -> None:
    """记账挂了绝不能把对话或通话带崩（spec §6.3）。"""

    class BoomStore:
        async def append(self, record: UsageRecord) -> bool:
            raise RuntimeError("db down")

    recorder = UsageRecorder(BoomStore(), _llm_book(), FxTable([]))  # type: ignore[arg-type]
    await recorder.record_llm(
        turn_id="t",
        step_index=0,
        user_id="u1",
        session_id=None,
        agent_id=None,
        scene="chat",
        provider="anthropic",
        model="claude-opus-5",
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=None,
        cache_write_tokens=None,
        occurred_at=NOW,
    )
    assert recorder.failure_count == 1


async def test_missing_user_id_is_recorded_as_unknown_user() -> None:
    """user_id 缺失时不能丢掉这笔成本，落到 unknown 用户名下并标 usage_missing，
    这样报表里看得见（spec §11 风险表）。"""
    store = InMemoryUsageStore()
    recorder = UsageRecorder(store, _llm_book(), FxTable([]))
    await recorder.record_llm(
        turn_id="t",
        step_index=0,
        user_id="",
        session_id=None,
        agent_id=None,
        scene="chat",
        provider="anthropic",
        model="claude-opus-5",
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=None,
        cache_write_tokens=None,
        occurred_at=NOW,
    )
    rows = await store.list_for_user("unknown", NOW, END)
    assert len(rows) == 1
    assert rows[0].status == "usage_missing"


async def test_duplicate_record_does_not_raise() -> None:
    store = InMemoryUsageStore()
    recorder = UsageRecorder(store, _volc_book(), _cny_table())
    for _ in range(2):
        await recorder.record_voice(
            session_id="s1",
            user_id="u1",
            provider="volc",
            duration_seconds=Decimal(60),
            audio_tokens=None,
            text_tokens=None,
            occurred_at=NOW,
        )
    assert recorder.failure_count == 0
    assert len(await store.list_for_user("u1", NOW, END)) == 1
