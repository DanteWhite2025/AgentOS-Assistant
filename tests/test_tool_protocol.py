"""发给模型的消息结构 —— 原生 tool 协议。

改造前实测（用 FunctionModel 拦下真实 prompt）：一轮工具调用之后，模型收到的是
**一条 ModelRequest，里面两条并排的 UserPromptPart**：

    ModelRequest:
      SystemPromptPart: '你是数字人'
      UserPromptPart:   'BTC 现在多少钱'
      UserPromptPart:   '[工具结果] {...}'

assistant 回合整个消失了（映射循环只处理 user 和 tool 两种 role），工具结果伪装
成第二条用户消息。落到 OpenAI wire 上是连续两条 role:"user"，模型看不到「是我
自己调的这个工具」，也拿不到 provider 对 tool role 的专门对齐。

这里把正确的形状钉死：ModelRequest / ModelResponse 交替，tool_call_id 配对。
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import ModelResponse, TextPart
from pydantic_ai import models as pai_models
from pydantic_ai.messages import (
    ModelRequest,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.llm import LLMMessage, LLMToolCall, PydanticAILLM

pai_models.ALLOW_MODEL_REQUESTS = False


class _Capture:
    """拦下真正发给模型的消息列表。"""

    # FunctionModel 会读 function.__name__ 做标识，可调用对象得自己补上。
    __name__ = "capture_messages"

    def __init__(self) -> None:
        self.messages: list = []

    def __call__(self, messages, info: AgentInfo) -> ModelResponse:
        self.messages = list(messages)
        return ModelResponse(parts=[TextPart("ok")])

    @property
    def shape(self) -> list[tuple[str, ...]]:
        """每条消息的 part 类型序列，便于整体断言。"""
        return [tuple(type(p).__name__ for p in m.parts) for m in self.messages]

    def parts_of(self, part_type) -> list:
        return [p for m in self.messages for p in m.parts if isinstance(p, part_type)]


async def _complete(capture: _Capture, messages: tuple[LLMMessage, ...]) -> None:
    llm = PydanticAILLM(FunctionModel(capture))
    await llm.complete("你是数字人", messages, ())


async def test_plain_question_is_a_single_request() -> None:
    """没有工具调用时结构不变——一条 ModelRequest，system + user。"""
    capture = _Capture()
    await _complete(capture, (LLMMessage("user", "你好"),))

    assert capture.shape == [("SystemPromptPart", "UserPromptPart")]


async def test_tool_round_becomes_request_response_request() -> None:
    """一轮工具调用展开成三条消息，而不是压进一条 ModelRequest。"""
    capture = _Capture()
    await _complete(
        capture,
        (
            LLMMessage("user", "BTC 现在多少钱"),
            LLMMessage(
                "assistant",
                tool_calls=(LLMToolCall("call_1", "get_market_price", {"symbol": "BTC"}),),
            ),
            LLMMessage(
                "tool",
                content='{"last_price":"92400"}',
                tool_call_id="call_1",
                tool_name="get_market_price",
            ),
        ),
    )

    assert capture.shape == [
        ("SystemPromptPart", "UserPromptPart"),
        ("ToolCallPart",),
        ("ToolReturnPart",),
    ]


async def test_tool_call_and_return_share_the_same_id() -> None:
    """配对靠 tool_call_id 字符串相等。对不上模型就认不出这是它那次调用的结果。"""
    capture = _Capture()
    await _complete(
        capture,
        (
            LLMMessage("user", "BTC 多少钱"),
            LLMMessage("assistant", tool_calls=(LLMToolCall("call_xyz", "get_market_price", {}),)),
            LLMMessage(
                "tool", content="{}", tool_call_id="call_xyz", tool_name="get_market_price"
            ),
        ),
    )

    call = capture.parts_of(ToolCallPart)[0]
    ret = capture.parts_of(ToolReturnPart)[0]
    assert call.tool_call_id == ret.tool_call_id == "call_xyz"
    assert call.tool_name == ret.tool_name == "get_market_price"


async def test_tool_result_never_becomes_a_user_message() -> None:
    """回归：工具结果曾被写成 UserPromptPart('[工具结果] ...')，
    落到 wire 上是连续两条 role:"user"。"""
    capture = _Capture()
    await _complete(
        capture,
        (
            LLMMessage("user", "BTC 多少钱"),
            LLMMessage("assistant", tool_calls=(LLMToolCall("c1", "get_market_price", {}),)),
            LLMMessage("tool", content="92400", tool_call_id="c1", tool_name="get_market_price"),
        ),
    )

    user_parts = capture.parts_of(UserPromptPart)
    assert len(user_parts) == 1, "只有真正的用户提问才是 UserPromptPart"
    assert all("[工具结果]" not in str(p.content) for p in user_parts)


async def test_tool_call_arguments_are_preserved() -> None:
    """参数要原样带给模型——它据此理解自己上一步做了什么。"""
    capture = _Capture()
    args = {"symbol": "BTC", "topic": "news"}
    await _complete(
        capture,
        (
            LLMMessage("user", "查一下"),
            LLMMessage("assistant", tool_calls=(LLMToolCall("c1", "web_search", args),)),
            LLMMessage("tool", content="{}", tool_call_id="c1", tool_name="web_search"),
        ),
    )

    assert capture.parts_of(ToolCallPart)[0].args == args


async def test_parallel_tool_calls_share_one_assistant_turn() -> None:
    """同一轮里并发调用的多个工具属于同一个 assistant 回合，
    对应的结果合并进随后的一条 ModelRequest。"""
    capture = _Capture()
    await _complete(
        capture,
        (
            LLMMessage("user", "看下我的账户"),
            LLMMessage(
                "assistant",
                tool_calls=(
                    LLMToolCall("c1", "get_balances", {}),
                    LLMToolCall("c2", "get_positions", {}),
                ),
            ),
            LLMMessage("tool", content="{}", tool_call_id="c1", tool_name="get_balances"),
            LLMMessage("tool", content="{}", tool_call_id="c2", tool_name="get_positions"),
        ),
    )

    assert capture.shape == [
        ("SystemPromptPart", "UserPromptPart"),
        ("ToolCallPart", "ToolCallPart"),
        ("ToolReturnPart", "ToolReturnPart"),
    ]


async def test_multi_round_tool_calls_stay_alternating() -> None:
    """两轮工具调用：request / response / request / response / request。"""
    capture = _Capture()
    await _complete(
        capture,
        (
            LLMMessage("user", "BTC 为什么跌"),
            LLMMessage("assistant", tool_calls=(LLMToolCall("c1", "web_search", {}),)),
            LLMMessage("tool", content="{}", tool_call_id="c1", tool_name="web_search"),
            LLMMessage("assistant", tool_calls=(LLMToolCall("c2", "get_market_price", {}),)),
            LLMMessage("tool", content="{}", tool_call_id="c2", tool_name="get_market_price"),
        ),
    )

    assert capture.shape == [
        ("SystemPromptPart", "UserPromptPart"),
        ("ToolCallPart",),
        ("ToolReturnPart",),
        ("ToolCallPart",),
        ("ToolReturnPart",),
    ]


async def test_assistant_text_reply_maps_to_text_part() -> None:
    """带文本的 assistant 消息（多轮对话历史）映射成 TextPart 而不是被丢弃。"""
    capture = _Capture()
    await _complete(
        capture,
        (
            LLMMessage("user", "你好"),
            LLMMessage("assistant", content="你好，有什么可以帮你"),
            LLMMessage("user", "查下持仓"),
        ),
    )

    assert capture.shape == [
        ("SystemPromptPart", "UserPromptPart"),
        ("TextPart",),
        ("UserPromptPart",),
    ]


async def test_orphan_tool_result_degrades_instead_of_breaking_the_pairing() -> None:
    """没有配对调用的工具结果（不该发生，但别让它把整个请求搞坏）：
    降级成普通文本，而不是造一个配不上任何调用的 ToolReturnPart。"""
    capture = _Capture()
    await _complete(
        capture,
        (
            LLMMessage("user", "BTC 多少钱"),
            LLMMessage("tool", content="92400", tool_name="get_market_price"),
        ),
    )

    assert capture.parts_of(ToolReturnPart) == []
    assert capture.shape == [("SystemPromptPart", "UserPromptPart", "UserPromptPart")]


async def test_system_prompt_stays_in_the_first_request() -> None:
    capture = _Capture()
    await _complete(
        capture,
        (
            LLMMessage("user", "问题"),
            LLMMessage("assistant", tool_calls=(LLMToolCall("c1", "get_balances", {}),)),
            LLMMessage("tool", content="{}", tool_call_id="c1", tool_name="get_balances"),
        ),
    )

    first = capture.messages[0]
    assert isinstance(first, ModelRequest)
    assert isinstance(first.parts[0], SystemPromptPart)
    assert first.parts[0].content == "你是数字人"
