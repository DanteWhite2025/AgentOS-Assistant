"""Provider Fake 实现测试。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.llm import FakeLLM, LLMMessage
from app.audit.store import AuditRecord, InMemoryAuditStore
from app.tools.core_client import FakeCoreClient
from app.voice.provider import FakeVoiceProvider


def _now() -> datetime:
    return datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)


async def test_fake_core_get_positions() -> None:
    data = await FakeCoreClient().get_resource("positions", "u1", {})
    assert "positions" in data


async def test_fake_core_create_draft() -> None:
    r = await FakeCoreClient().create_trade_draft(
        "u1", {"symbol": "ETH", "side": "long", "qty": "1"}
    )
    assert r["trade_draft_id"].startswith("td_")
    assert r["status"] == "PENDING_CONFIRMATION"


async def test_fake_voice_secret() -> None:
    cred = await FakeVoiceProvider().issue_credentials(
        "sess_1", "u1", ("get_positions",), "gpt-realtime-2.1", _now()
    )
    assert cred.client_secret.startswith("ek_fake_")
    assert cred.model == "gpt-realtime-2.1"
    assert cred.call_url.endswith("/v1/realtime/calls")


async def test_fake_llm_tool_then_text() -> None:
    llm = FakeLLM()
    r1 = await llm.complete("sys", (LLMMessage("user", "帮我看下持仓"),), ("get_positions",))
    assert r1.tool_calls and r1.tool_calls[0].name == "get_positions"

    r2 = await llm.complete(
        "sys",
        (LLMMessage("user", "帮我看下持仓"), LLMMessage("tool", "{持仓:ETH 1.2}")),
        ("get_positions",),
    )
    assert r2.text and not r2.tool_calls


async def test_fake_llm_no_tool_when_not_in_scope() -> None:
    # 命中关键词但工具不在白名单 → 不发 tool_call
    r = await FakeLLM().complete("sys", (LLMMessage("user", "看下持仓"),), ())
    assert not r.tool_calls


async def test_audit_store() -> None:
    store = InMemoryAuditStore()
    await store.record_tool_call(AuditRecord("t1", "sess_1", "get_positions", "h1", "ref1", _now()))
    recs = await store.list_by_session("sess_1")
    assert len(recs) == 1 and recs[0].tool_name == "get_positions"
