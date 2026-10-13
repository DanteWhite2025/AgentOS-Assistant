"""测试性能优化（工具并发、Minara独立模型）与任务打断（user.interrupt）。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.agents.llm import LLMMessage, LLMResult, LLMToolCall
from app.agents.runner import AgentProfile, AgentRunner
from app.audit.store import InMemoryAuditStore
from app.config import Settings
from app.main import app
from app.runtime.session import Scope, Session
from app.tools.builtin import build_default_registry
from app.tools.core_client import FakeCoreClient
from app.tools.executor import ToolExecutor


def _now() -> datetime:
    return datetime.now(UTC)


def _session(tools: tuple[str, ...]) -> Session:
    now = _now()
    return Session(
        session_id="sess_test",
        user_id="usr_test",
        modality="text",
        scope=Scope(agents=("avatar",), tools=tools),
        state="text_active",
        created_at=now,
        last_activity_at=now,
    )


class _SlowConcurrentToolsLLM:
    """模拟返回多个只读工具调用的 LLM。"""

    def __init__(self) -> None:
        self.turns = 0

    async def complete(
        self, system: str, messages: tuple[LLMMessage, ...], tools: tuple[str, ...]
    ) -> LLMResult:
        if self.turns == 0:
            self.turns += 1
            return LLMResult(
                text="",
                tool_calls=(
                    LLMToolCall("c1", "get_positions", {}),
                    LLMToolCall("c2", "get_balances", {}),
                    LLMToolCall("c3", "market_tickers", {}),
                ),
            )
        return LLMResult(text="3个工具执行完毕")


@pytest.mark.asyncio
async def test_readonly_tools_execute_concurrently() -> None:
    """验证多个只读工具在 AgentRunner 中并发异步执行。"""
    execution_times: list[float] = []

    class MockDelayedExecutor(ToolExecutor):
        async def execute(self, name: str, user_id: str, arguments: dict, scope_tools: list[str], **kwargs) -> dict:
            start = asyncio.get_event_loop().time()
            await asyncio.sleep(0.05)  # 每个工具耗时 50ms
            execution_times.append(asyncio.get_event_loop().time() - start)
            return {"status": "ok", "name": name}

    audit = InMemoryAuditStore()
    executor = MockDelayedExecutor(build_default_registry(), FakeCoreClient())
    profile = AgentProfile(
        "avatar",
        "fake",
        "你是助手",
        ("get_positions", "get_balances", "market_tickers"),
    )
    runner = AgentRunner(profile, executor, _SlowConcurrentToolsLLM(), audit)

    start_all = asyncio.get_event_loop().time()
    result = await runner.run_turn(
        _session(("get_positions", "get_balances", "market_tickers")),
        "查持仓、余额和行情",
        "trace_concurrent",
        _now(),
    )
    total_time = asyncio.get_event_loop().time() - start_all

    assert result.text == "3个工具执行完毕"
    assert len(result.executed_tools) == 3
    # 串行耗时约 150ms+，并发执行总耗时应显著小于 120ms（通常在 60ms 左右）
    assert total_time < 0.12


def test_investment_expert_llm_model_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证投研模型配置项读取正确，且支持标准名与向后兼容别名。"""
    monkeypatch.setenv("AGENT_LLM_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("AGENT_INVESTMENT_EXPERT_LLM_MODEL", "deepseek-v4-flash-max")

    s = Settings()
    assert s.investment_expert_llm_model == "deepseek-v4-flash-max"
    assert s.minara_llm_model == "deepseek-v4-flash-max"
    assert s.llm_model == "claude-opus-4-8"

    # 测试别名 AGENT_FAST_LLM_MODEL
    monkeypatch.delenv("AGENT_INVESTMENT_EXPERT_LLM_MODEL", raising=False)
    monkeypatch.setenv("AGENT_FAST_LLM_MODEL", "claude-3-5-haiku")
    s2 = Settings()
    assert s2.investment_expert_llm_model == "claude-3-5-haiku"


def test_ws_user_interrupt_cancels_active_turn() -> None:
    """验证客户端发送 user.interrupt 时能立即取消运行中的长任务并重置状态为 Idle。"""
    USER_HEADERS = {"Authorization": "Bearer test-user:u1"}

    with TestClient(app) as client:
        body = client.post(
            "/v1/agent/sessions", json={"modality": "text"}, headers=USER_HEADERS
        ).json()
        sid, rt = body["session_id"], body["rt_token"]

        with client.websocket_connect(f"/v1/agent/stream?session_id={sid}&token={rt}") as ws:
            first = ws.receive_json()
            assert first["type"] == "session.state"

            # 发送提问
            ws.send_json({"type": "user.message", "data": {"text": "帮我看下持仓"}})
            # 紧接着发送打断
            ws.send_json({"type": "user.interrupt"})

            events = []
            for _ in range(50):
                evt = ws.receive_json()
                events.append(evt)
                if evt.get("type") == "avatar.event" and evt.get("data", {}).get("value") == "Idle":
                    break

            idle_events = [
                e for e in events
                if e.get("type") == "avatar.event" and e.get("data", {}).get("value") == "Idle"
            ]
            assert len(idle_events) >= 1


@pytest.mark.asyncio
async def test_runner_prevents_repeated_readonly_tool_loops_and_synthesizes() -> None:
    """验证 Runner 在执行只读分析工具后，动态剔除已调用的只读工具，防止大模型反复套娃耗尽步数。"""
    from app.agents.llm import LLMResult, LLMToolCall
    from app.agents.runner import AgentProfile, AgentRunner
    from app.audit.store import InMemoryAuditStore
    from app.tools.builtin import build_default_registry
    from app.tools.executor import ToolExecutor

    class _LoopAttemptLLM:
        def __init__(self) -> None:
            self.step = 0

        async def complete(
            self, system: str, messages: tuple[object, ...], tools: tuple[object, ...]
        ) -> LLMResult:
            self.step += 1
            tool_names = {getattr(t, "name", str(t)) for t in tools}
            if self.step == 1:
                # 第 1 步：调用 market_tickers 工具
                assert "market_tickers" in tool_names
                return LLMResult(
                    text="",
                    tool_calls=(LLMToolCall("c1", "market_tickers", {"symbols": ["BTC", "ETH"]}),),
                )
            else:
                # 第 2 步：market_tickers 已被移除，大模型无法再次套娃调用，直接输出总结
                assert "market_tickers" not in tool_names
                return LLMResult(text="BTC与ETH行情总结完毕", tool_calls=())

    class _MockOkExecutor(ToolExecutor):
        async def execute(self, name: str, user_id: str, arguments: dict, scope_tools: list[str], **kwargs) -> dict:
            return {"status": "ok", "name": name, "tickers": {"BTC": 65000, "ETH": 3500}}

    registry = build_default_registry()
    profile = AgentProfile("test", "gpt-5.6-terra", "sys", registry.names())
    executor = _MockOkExecutor(registry, FakeCoreClient())
    runner = AgentRunner(profile, executor, _LoopAttemptLLM(), InMemoryAuditStore(), max_steps=4)  # type: ignore

    res = await runner.run_turn(
        _session(("market_tickers",)),
        "查下BTC和ETH",
        "trc_loop_test",
        _now(),
    )
    assert res.text == "BTC与ETH行情总结完毕"
    assert len(res.executed_tools) == 1


