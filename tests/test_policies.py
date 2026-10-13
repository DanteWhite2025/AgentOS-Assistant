"""风控（Agent Mandate）测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.main import app
from app.policies.engine import PolicyEngine, PolicyViolationError
from app.policies.mandate import AgentMandate, default_mandate
from app.tools.builtin import build_default_registry
from app.tools.core_client import FakeCoreClient
from app.tools.executor import ToolExecutor


def _now() -> datetime:
    return datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


def test_within_limits_allowed() -> None:
    decision = PolicyEngine(default_mandate()).check_trade_draft(
        {"symbol": "ETH", "qty": "1", "price": "3000"}, _now()
    )
    assert decision.allowed and decision.requires_confirmation


def test_notional_exceeds() -> None:
    # 默认 max_notional=10000；1×30000 超额
    decision = PolicyEngine(default_mandate()).check_trade_draft(
        {"symbol": "ETH", "qty": "10", "price": "3000"}, _now()
    )
    assert not decision.allowed and "额度" in decision.reason


def test_leverage_exceeds() -> None:
    decision = PolicyEngine(default_mandate()).check_trade_draft(
        {"symbol": "ETH", "qty": "1", "price": "100", "leverage": "10"}, _now()
    )
    assert not decision.allowed and "杠杆" in decision.reason


def test_asset_not_allowed() -> None:
    decision = PolicyEngine(AgentMandate(allowed_assets=("BTC",))).check_trade_draft(
        {"symbol": "ETH", "qty": "1", "price": "1"}, _now()
    )
    assert not decision.allowed and "资产" in decision.reason


def test_mandate_expired() -> None:
    decision = PolicyEngine(AgentMandate(expires_at=_now() - timedelta(seconds=1))).check_trade_draft(
        {"symbol": "ETH"}, _now()
    )
    assert not decision.allowed and "过期" in decision.reason


async def test_executor_blocks_over_limit() -> None:
    executor = ToolExecutor(build_default_registry(), FakeCoreClient(), PolicyEngine(default_mandate()))
    with pytest.raises(PolicyViolationError):
        await executor.execute(
            "create_trade_draft", "u1", {"symbol": "ETH", "qty": "10", "price": "3000"}, ["create_trade_draft"]
        )


async def test_executor_allows_within_limit() -> None:
    executor = ToolExecutor(build_default_registry(), FakeCoreClient(), PolicyEngine(default_mandate()))
    out = await executor.execute(
        "create_trade_draft", "u1", {"symbol": "ETH", "qty": "1", "price": "3000"}, ["create_trade_draft"]
    )
    assert out["trade_draft_id"].startswith("td_")


async def test_relay_policy_violation_403() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        body = (
            await client.post(
                "/v1/agent/sessions",
                json={"modality": "text"},
                headers={"Authorization": "Bearer test-user:u1"},
            )
        ).json()
        sid, rt = body["session_id"], body["rt_token"]
        resp = await client.post(
            f"/v1/agent/sessions/{sid}/tool-calls",
            json={
                "call_id": "c1",
                "tool_name": "create_trade_draft",
                "arguments": {"symbol": "ETH", "qty": "100", "price": "3000"},
            },
            headers={"Authorization": f"Bearer {rt}"},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "POLICY_VIOLATION"


async def test_rejected_tool_call_is_audited() -> None:
    # 红线：被风控拒绝的 tool call 也要留痕（status=rejected）
    from app.agents.llm import LLMResult, LLMToolCall
    from app.agents.runner import AgentProfile, AgentRunner
    from app.audit.store import InMemoryAuditStore
    from app.runtime.session import Scope, Session

    class _DraftLLM:
        async def complete(self, system, messages, tools):
            if any(m.role == "tool" for m in messages):
                return LLMResult(text="已尝试下单")
            return LLMResult(
                text="",
                tool_calls=(
                    LLMToolCall("c1", "create_trade_draft", {"symbol": "ETH", "qty": "100", "price": "3000"}),
                ),
            )

    audit = InMemoryAuditStore()
    runner = AgentRunner(
        AgentProfile("default", "fn", "sys", ("create_trade_draft",)),
        ToolExecutor(build_default_registry(), FakeCoreClient(), PolicyEngine(default_mandate())),
        _DraftLLM(),
        audit,
    )
    session = Session(
        "sess_1", "u1", "text", Scope(("strategy",), ("create_trade_draft",)), "text_active", _now(), _now()
    )
    result = await runner.run_turn(session, "帮我下单 100 个 ETH", "trace1", _now())

    records = await audit.list_by_session("sess_1")
    assert len(records) == 1
    assert records[0].status == "rejected"
    assert not result.executed_tools  # 被拒，未执行
