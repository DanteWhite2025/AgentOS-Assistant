"""端到端验收：一轮真实对话跑完，账单接口能查出这笔钱。

各部分单独通不等于整条链路通。这个测试穿过全部环节：
真实容器装配 → runner 跑一轮（LLM + 工具）→ UsageRecorder 计价 →
usage_store 落流水 → /internal/usage 账单接口读出来。

对应 spec §9 的验收项「三类用量均有流水」与「单用户任意账期账单可查」。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_container
from app.api.usage import router as usage_router
from app.config import get_settings
from app.container import build_container
from app.runtime.session import Scope, Session

NOW = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)
PERIOD = {"from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"}


@pytest.fixture
async def container() -> AsyncIterator[object]:
    ctn = build_container(get_settings())
    try:
        yield ctn
    finally:
        await ctn.aclose()


def _session() -> Session:
    return Session(
        session_id="sess_e2e",
        user_id="u_e2e",
        modality="text",
        scope=Scope(agents=("avatar",), tools=("get_positions",)),
        state="text_active",
        created_at=NOW,
        last_activity_at=NOW,
    )


async def test_one_real_turn_produces_llm_and_tool_usage(container) -> None:
    """跑一轮带工具的对话，LLM 与外部 API 两类流水都要落下来，且归并到同一 turn。"""
    result = await container.runner.run_turn(_session(), "帮我看下持仓", "trc_e2e", NOW)
    assert result.text, "这一轮本身要正常出答案"

    rows = await container.usage_store.list_for_user(
        "u_e2e", datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
    )
    assert rows, "一轮对话必须留下用量流水"
    assert {r.kind for r in rows} == {"llm", "external_api"}
    # 一次聊天 = 一个 turn，两类成本必须能加到一起（spec §6.2.0 / S4）
    assert len({r.turn_id for r in rows}) == 1

    tool_rows = [r for r in rows if r.kind == "external_api"]
    assert tool_rows[0].provider == "core"
    assert tool_rows[0].quantity.call_count == 1

    # FakeLLM 的 usage 恒为 None，所以 LLM 那条应当是 usage_missing 而不是被丢掉——
    # 「拿不到用量」和「没有用量」必须区分，否则成本被系统性低估（spec §7.2）。
    llm_rows = [r for r in rows if r.kind == "llm"]
    assert all(r.status == "usage_missing" for r in llm_rows)
    assert all(r.amount_usd is None for r in llm_rows)


async def test_bill_api_reads_back_the_turn_just_run(container) -> None:
    """同一条流水要能被账单接口查出来——这是甲方最终看到的那个数。"""
    await container.runner.run_turn(_session(), "帮我看下持仓", "trc_e2e_2", NOW)

    app = FastAPI()
    app.include_router(usage_router, prefix="/internal/usage")
    app.dependency_overrides[get_container] = lambda: container
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        bill = (await client.get("/internal/usage/users/u_e2e/bill", params=PERIOD)).json()
        ledger = (await client.get("/internal/usage/users/u_e2e/ledger", params=PERIOD)).json()

    assert bill["user_id"] == "u_e2e"
    assert bill["data_quality"]["record_count"] >= 2
    # Core 单价 0（spec D2）+ FakeLLM 无 usage，所以总额是 0；
    # 关键是 usage_missing 的条数必须露出来，否则这个 0 是个假数。
    assert bill["data_quality"]["usage_missing_count"] >= 1
    assert Decimal(bill["total_amount_usd"]) == 0
    assert len(ledger["items"]) >= 2
    # 明细里必须带得出可复算所需的字段
    assert "price_version" in ledger["items"][0]
    assert "unit_price_snapshot" in ledger["items"][0]


async def test_repeated_turns_do_not_collide_on_dedup_key(container) -> None:
    """连跑两轮，流水不能互相覆盖——dedup_key 要按 turn 区分开。"""
    await container.runner.run_turn(_session(), "看下持仓", "trc_a", NOW)
    await container.runner.run_turn(_session(), "再看一次", "trc_b", NOW)

    rows = await container.usage_store.list_for_user(
        "u_e2e", datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC)
    )
    assert len({r.turn_id for r in rows}) == 2
    assert len({r.dedup_key for r in rows}) == len(rows), "dedup_key 必须两两不同"
