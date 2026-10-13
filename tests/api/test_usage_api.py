"""GET /internal/usage/* 用量与成本查询接口。

搭建方式照 tests/api/test_news.py：FastAPI() + include_router +
dependency_overrides[get_container]。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_container
from app.api.usage import router
from app.usage.models import UsageQuantity, UsageRecord
from app.usage.store import InMemoryUsageStore

T1 = datetime(2026, 9, 10, tzinfo=UTC)
PERIOD = {"from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"}


class _Ctn:
    def __init__(self, store) -> None:
        self.usage_store = store


@pytest.fixture
async def client_and_store():
    store = InMemoryUsageStore()
    app = FastAPI()
    app.include_router(router, prefix="/internal/usage")
    app.dependency_overrides[get_container] = lambda: _Ctn(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, store


async def _seed(store: InMemoryUsageStore) -> None:
    await store.append(
        UsageRecord(
            dedup_key="llm:t1:0",
            occurred_at=T1,
            recorded_at=T1,
            user_id="u1",
            kind="llm",
            provider="anthropic",
            model="claude-opus-5",
            scene="chat",
            turn_id="t1",
            session_id="s1",
            quantity=UsageQuantity(input_tokens=1000, output_tokens=200),
            price_version="v1",
            unit_price_snapshot={"input_tokens": "15"},
            amount_usd=Decimal(1),
        )
    )
    await store.append(
        UsageRecord(
            dedup_key="voice:s2",
            occurred_at=T1,
            recorded_at=T1,
            user_id="u1",
            kind="voice",
            provider="volc",
            scene="voice",
            session_id="s2",
            quantity=UsageQuantity(duration_seconds=Decimal(60)),
            amount_usd=Decimal(2),
        )
    )
    await store.append(
        UsageRecord(
            dedup_key="llm:t2:0",
            occurred_at=T1,
            recorded_at=T1,
            user_id="u1",
            kind="llm",
            provider="anthropic",
            scene="chat",
            turn_id="t2",
            quantity=UsageQuantity(),
            amount_usd=None,
            status="usage_missing",
        )
    )
    await store.append(
        UsageRecord(
            dedup_key="llm:t3:0",
            occurred_at=T1,
            recorded_at=T1,
            user_id="u2",
            kind="llm",
            provider="anthropic",
            scene="chat",
            turn_id="t3",
            quantity=UsageQuantity(input_tokens=9000),
            amount_usd=Decimal(9),
        )
    )


async def test_user_bill_returns_totals_and_quality(client_and_store) -> None:
    client, store = client_and_store
    await _seed(store)
    resp = await client.get("/internal/usage/users/u1/bill", params=PERIOD)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == "u1"
    assert body["total_amount_usd"] == "3.00000000"
    assert body["by_kind"]["llm"] == "1.00000000"
    assert body["by_kind"]["voice"] == "2.00000000"
    assert body["data_quality"]["usage_missing_count"] == 1
    assert body["data_quality"]["record_count"] == 3
    # 迟到数据会按 occurred_at 写回原账期并重算，账单因此有版本（spec D8）
    assert body["revision"] == 1


async def test_user_bill_reports_quantity_totals(client_and_store) -> None:
    """账单要答「用了多少量」，不只是多少钱。"""
    client, store = client_and_store
    await _seed(store)
    body = (await client.get("/internal/usage/users/u1/bill", params=PERIOD)).json()
    assert body["quantity_totals"]["input_tokens"] == "1000"
    assert body["quantity_totals"]["duration_seconds"] == "60"


async def test_user_bill_requires_period(client_and_store) -> None:
    client, _ = client_and_store
    assert (await client.get("/internal/usage/users/u1/bill")).status_code == 422


async def test_user_bill_of_unknown_user_is_empty_not_404(client_and_store) -> None:
    """没有用量的用户返回零账单——404 会让调用方无法区分「没用」和「查错了」。"""
    client, store = client_and_store
    await _seed(store)
    resp = await client.get("/internal/usage/users/nobody/bill", params=PERIOD)
    assert resp.status_code == 200
    assert resp.json()["total_amount_usd"] == "0.00000000"


async def test_ledger_paginates_and_filters_by_kind(client_and_store) -> None:
    client, store = client_and_store
    await _seed(store)
    params = {**PERIOD, "kind": "llm", "limit": 1}
    resp = await client.get("/internal/usage/users/u1/ledger", params=params)

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "llm"
    # 可复算：单价与版本必须能下钻到（spec G3 / S6）
    assert "unit_price_snapshot" in items[0]
    assert "price_version" in items[0]


async def test_summary_groups_by_whitelisted_dimensions(client_and_store) -> None:
    client, store = client_and_store
    await _seed(store)
    params = {**PERIOD, "group_by": "scene,provider"}
    resp = await client.get("/internal/usage/summary", params=params)

    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    by_scene = {(r["scene"], r["provider"]): r["amount_usd"] for r in rows}
    assert by_scene[("voice", "volc")] == "2.00000000"


async def test_summary_rejects_unknown_group_by_with_400(client_and_store) -> None:
    """group_by 会拼进 SQL，非白名单维度必须在 API 层就挡掉，给 400 而非 500。"""
    client, _ = client_and_store
    params = {**PERIOD, "group_by": "secret_column"}
    resp = await client.get("/internal/usage/summary", params=params)
    assert resp.status_code == 400


async def test_unit_economics_answers_the_three_questions(client_and_store) -> None:
    """甲方问的就是这三个数：每分钟通话、每次聊天、每次分析。"""
    client, store = client_and_store
    await _seed(store)
    resp = await client.get("/internal/usage/unit-economics", params=PERIOD)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    units = body["units"]
    assert set(units) >= {"voice_per_minute", "chat_per_turn", "analysis_per_call"}
    assert {"mean", "p50", "p95", "sample_size"} <= set(units["voice_per_minute"])
    # 60 秒花了 2 USD → 每分钟 2 USD
    assert units["voice_per_minute"]["mean"] == "2.00000000"
    # 单位经济学是跨用户全局的：t1(u1, 1 USD) + t2(u1, usage_missing 0) + t3(u2, 9 USD)
    assert units["chat_per_turn"]["sample_size"] == 3
    assert body["data_quality"]["usage_missing_ratio"] is not None


async def test_unit_economics_with_no_samples_reports_zero_not_error(client_and_store) -> None:
    client, _ = client_and_store
    resp = await client.get("/internal/usage/unit-economics", params=PERIOD)
    assert resp.status_code == 200
    assert resp.json()["units"]["chat_per_turn"]["sample_size"] == 0


async def test_top_users_orders_desc(client_and_store) -> None:
    client, store = client_and_store
    await _seed(store)
    params = {**PERIOD, "limit": 2}
    resp = await client.get("/internal/usage/top-users", params=params)

    assert resp.status_code == 200, resp.text
    rows = resp.json()["rows"]
    assert [r["user_id"] for r in rows] == ["u2", "u1"]
    assert rows[0]["amount_usd"] == "9.00000000"


async def test_amounts_are_serialized_as_strings(client_and_store) -> None:
    """金额走字符串，不走 JSON number——float 化会吃掉 token 级精度。"""
    client, store = client_and_store
    await _seed(store)
    body = (await client.get("/internal/usage/users/u1/bill", params=PERIOD)).json()
    assert isinstance(body["total_amount_usd"], str)


async def test_missing_usage_store_yields_503(client_and_store) -> None:
    """容器未接线时明确报不可用，不要 500。"""
    client, _ = client_and_store

    class _Empty:
        pass

    client._transport.app.dependency_overrides[get_container] = _Empty  # type: ignore[attr-defined]
    resp = await client.get("/internal/usage/users/u1/bill", params=PERIOD)
    assert resp.status_code == 503
