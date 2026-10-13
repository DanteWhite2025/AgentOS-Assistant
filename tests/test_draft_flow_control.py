from datetime import UTC, datetime

import httpx
import pytest

from app.agents.runner import (
    AgentProfile,
    AgentRunner,
    _check_trade_intent_completeness,
)
from app.audit.store import AuditStore
from app.runtime.session import Scope, Session
from app.tools.agentos.trade_flow import TradingReadinessError


class MockLLM:
    def __init__(self, responses):
        self.responses = responses
        self.call_count = 0

    async def complete(self, system_prompt, messages, allowed_tools):
        resp = self.responses[self.call_count]
        self.call_count += 1
        return resp


class MockExecutor:
    def __init__(self, side_effects):
        self.side_effects = side_effects
        self.call_count = 0
        self.executed = []

    async def execute(self, *args, **kwargs):
        self.executed.append((args, kwargs))
        if isinstance(self.side_effects, list):
            if self.call_count < len(self.side_effects):
                effect = self.side_effects[self.call_count]
            else:
                effect = self.side_effects[-1]
        else:
            effect = self.side_effects
        self.call_count += 1

        if isinstance(effect, Exception):
            raise effect
        return effect


class MockAuditStore(AuditStore):
    async def record_tool_call(self, record):
        pass
    async def get_by_trace(self, trace_id):
        return []
    async def get_recent_tool_calls(self, session_id, limit):
        return []

@pytest.fixture
def profile():
    return AgentProfile(
        name="test",
        model="test",
        system_prompt="test",
        tools=("create_trade_draft", "get_market_price"),
    )


@pytest.fixture
def session():
    return Session(
        session_id="sess_123",
        user_id="user_123",
        modality="text",
        state="text_active",
        created_at=datetime.now(UTC),
        last_activity_at=datetime.now(UTC),
        scope=Scope(agents=("avatar",), tools=("create_trade_draft", "get_market_price")),
    )


def test_intent_completeness():
    assert _check_trade_intent_completeness("ETH long 2x 0.1 ETH") is not None
    assert _check_trade_intent_completeness("做多 ETH") is not None
    assert _check_trade_intent_completeness("ETH long 2x 0.1 ETH at 3200") is None
    assert _check_trade_intent_completeness("按当前价格做多 ETH，2倍，0.1 ETH") is None
    assert _check_trade_intent_completeness("ETH long 2x 0.1 ETH @ 3200") is None


@pytest.mark.asyncio
async def test_draft_missing_price_early_exit(profile, session):
    runner = AgentRunner(profile, MockExecutor({}), MockLLM([]), MockAuditStore())
    res = await runner.run_turn(session, "ETH long 2x 0.1 ETH", "trace_1", datetime.now(UTC))
    assert "请告诉我目标价格" in res.text
    assert len(res.executed_tools) == 0


@pytest.mark.asyncio
async def test_draft_success_early_exit(profile, session):
    from app.agents.llm import LLMResult, LLMToolCall

    llm = MockLLM([
        LLMResult(text="", tool_calls=(LLMToolCall("c_1", "create_trade_draft", {"symbol": "ETH"}),)),
        LLMResult(text="交易草稿已创建，正在等待你确认。", tool_calls=())
    ])
    executor = MockExecutor({"trade_draft_id": "draft_123"})
    runner = AgentRunner(profile, executor, llm, MockAuditStore())
    
    res = await runner.run_turn(session, "ETH long 2x 0.1 ETH at 3200", "trace_1", datetime.now(UTC))
    
    assert "交易草稿已创建" in res.text
    assert len(res.executed_tools) == 1
    assert res.executed_tools[0].trade_draft_id == "draft_123"
    assert executor.call_count == 1
    assert llm.call_count == 2


@pytest.mark.asyncio
async def test_core_limit_rejection_early_exit(profile, session):
    from app.agents.llm import LLMResult, LLMToolCall

    llm = MockLLM([LLMResult(text="", tool_calls=(LLMToolCall("c_1", "create_trade_draft", {"symbol": "ETH"}),))])
    executor = MockExecutor(TradingReadinessError("请求金额超过 Core Mandate 或策略剩余额度"))
    runner = AgentRunner(profile, executor, llm, MockAuditStore())
    
    res = await runner.run_turn(session, "ETH long 2x 0.1 ETH at 3200", "trace_1", datetime.now(UTC))
    
    assert "超过 Core 当前允许的单笔交易额度" in res.text
    assert executor.call_count == 1
    assert llm.call_count == 1


@pytest.mark.asyncio
async def test_identical_duplicate_call_deduplication(profile, session):
    from app.agents.llm import LLMResult, LLMToolCall

    call = LLMToolCall("c_1", "get_market_price", {"symbol": "ETH"})
    llm = MockLLM([
        LLMResult(text="", tool_calls=(call, call)), # LLM tries to call it twice in one turn
    ])
    executor = MockExecutor({"price": "3000"})
    runner = AgentRunner(profile, executor, llm, MockAuditStore())
    
    await runner.run_turn(session, "按当前价格做多 ETH，2倍，0.1 ETH", "trace_1", datetime.now(UTC))
    
    assert executor.call_count == 1 # Only executed once


@pytest.mark.asyncio
async def test_network_timeout_early_exit(profile, session):
    from app.agents.llm import LLMResult, LLMToolCall

    llm = MockLLM([LLMResult(text="", tool_calls=(LLMToolCall("c_1", "create_trade_draft", {"symbol": "ETH"}),))])
    executor = MockExecutor(httpx.TimeoutException("timeout"))
    runner = AgentRunner(profile, executor, llm, MockAuditStore())
    
    res = await runner.run_turn(session, "ETH long 2x 0.1 ETH at 3200", "trace_1", datetime.now(UTC))
    
    assert "超时" in res.text
    assert executor.call_count == 1
    assert llm.call_count == 1
