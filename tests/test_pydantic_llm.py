"""PydanticAILLM 真实集成测试（用 FunctionModel，无需 API key）。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai import models as pai_models
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from app.agents.llm import LLMMessage, LLMToolCall, PydanticAILLM

# 防止意外发起真实 API 请求；FunctionModel 是本地的，不受影响
pai_models.ALLOW_MODEL_REQUESTS = False


def _tool_then_text(messages, info: AgentInfo) -> ModelResponse:
    """首轮调 get_positions；看到工具结果后给文本回复。

    判据是原生的 ToolReturnPart。改造前工具结果伪装成 UserPromptPart，只能靠
    "[工具结果]" 前缀认——那个前缀现在只剩孤儿降级路径还在用。
    """
    seen_tool = any(
        isinstance(part, ToolReturnPart) for message in messages for part in message.parts
    )
    if seen_tool:
        return ModelResponse(parts=[TextPart("你当前持有 1.2 ETH")])
    return ModelResponse(parts=[ToolCallPart(tool_name="get_positions", args={})])


async def test_pydantic_llm_emits_tool_call() -> None:
    llm = PydanticAILLM(FunctionModel(_tool_then_text))
    result = await llm.complete("你是助手", (LLMMessage("user", "看下持仓"),), ("get_positions",))
    assert result.tool_calls and result.tool_calls[0].name == "get_positions"
    assert not result.text


async def test_pydantic_llm_text_after_tool() -> None:
    llm = PydanticAILLM(FunctionModel(_tool_then_text))
    result = await llm.complete(
        "你是助手",
        (
            LLMMessage("user", "看下持仓"),
            LLMMessage("assistant", tool_calls=(LLMToolCall("c1", "get_positions", {}),)),
            LLMMessage(
                "tool", content="持仓数据", tool_call_id="c1", tool_name="get_positions"
            ),
        ),
        ("get_positions",),
    )
    assert result.text and not result.tool_calls


async def test_pydantic_llm_passes_max_output_tokens_when_configured() -> None:
    """没有 max_tokens 熔断时，模型对某些开放式问题可能无限制地啰嗦下去

    （实测：投研分析场景吐过 1.4 万 output token，耗时 4 分半，比任何合理的客户端
    超时都长）。配置了 max_output_tokens 时必须真的透传给 model_request。
    """
    captured: dict = {}

    def _capture_settings(messages, info: AgentInfo) -> ModelResponse:
        captured["model_settings"] = info.model_settings
        return ModelResponse(parts=[TextPart("ok")])

    llm = PydanticAILLM(FunctionModel(_capture_settings), max_output_tokens=4000)
    await llm.complete("你是助手", (LLMMessage("user", "hi"),), ())

    assert captured["model_settings"] == {"max_tokens": 4000}


async def test_pydantic_llm_omits_max_tokens_when_not_configured() -> None:
    """默认不设置时保持既有行为不变（向后兼容）。"""
    captured: dict = {}

    def _capture_settings(messages, info: AgentInfo) -> ModelResponse:
        captured["model_settings"] = info.model_settings
        return ModelResponse(parts=[TextPart("ok")])

    llm = PydanticAILLM(FunctionModel(_capture_settings))
    await llm.complete("你是助手", (LLMMessage("user", "hi"),), ())

    assert captured["model_settings"] is None


async def _tool_then_text_stream(messages, info: AgentInfo):
    """_tool_then_text 的流式版本，供走 runner 的用例使用。"""
    response = _tool_then_text(messages, info)
    for index, part in enumerate(response.parts):
        if isinstance(part, TextPart):
            yield part.content
        elif isinstance(part, ToolCallPart):
            yield {
                index: DeltaToolCall(
                    name=part.tool_name, json_args="{}", tool_call_id=part.tool_call_id
                )
            }


async def test_pydantic_llm_drives_agent_runner() -> None:
    # 真实 LLM provider 无缝接入现有 AgentRunner，工具执行 + 审计正常
    from app.agents.runner import AgentProfile, AgentRunner
    from app.audit.store import InMemoryAuditStore
    from app.runtime.session import Scope, Session
    from app.tools.builtin import build_default_registry
    from app.tools.core_client import FakeCoreClient
    from app.tools.executor import ToolExecutor

    now = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
    audit = InMemoryAuditStore()
    runner = AgentRunner(
        AgentProfile("default", "fn", "你是助手", ("get_positions",)),
        ToolExecutor(build_default_registry(), FakeCoreClient()),
        PydanticAILLM(
            FunctionModel(_tool_then_text, stream_function=_tool_then_text_stream)
        ),
        audit,
    )
    session = Session(
        "sess_1", "u1", "text", Scope(("avatar",), ("get_positions",)), "text_active", now, now
    )
    result = await runner.run_turn(session, "看下持仓", "trace1", now)
    assert result.executed_tools and result.executed_tools[0].name == "get_positions"
    assert result.text
    assert len(await audit.list_by_session("sess_1")) == 1
