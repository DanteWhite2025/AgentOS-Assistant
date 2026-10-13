"""LLM 层流式输出。

改造前 ws.py 把完整答案切成 8 字一段假装流式（循环里连 sleep 都没有），首字延迟
等于整轮耗时。这一层负责把真实的 token 增量吐出来；接到 WebSocket 是后续 PR。

三个必须钉死的点，都是流式特有的坑：
1. span 要包住整个流——只包住"建立请求"那一下的话，span 在第一个 token 之前就
   关了，之后 record_llm_usage 是静默 no-op，用量全丢。
2. usage 只有流跑完才准确（provider 逐 chunk 累加，最终值在最后一个 usage-only
   chunk 里），提前 break 会缺最后一块。
3. 工具调用的参数是逐块的残缺 JSON，中途不能解析。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import models as pai_models
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from app.agents.llm import (
    FakeLLM,
    LLMMessage,
    LLMStreamDone,
    LLMTextDelta,
    PydanticAILLM,
)

pai_models.ALLOW_MODEL_REQUESTS = False


async def _text_stream(messages, info: AgentInfo):
    for chunk in ("比特", "币今天", "下跌 3%"):
        yield chunk


async def _tool_stream(messages, info: AgentInfo):
    # 参数逐块到达，中途是残缺 JSON
    yield {0: DeltaToolCall(name="get_market_price", json_args='{"sym', tool_call_id="c1")}
    yield {0: DeltaToolCall(json_args='bol":"BTC"}')}


_DEFAULT_MESSAGES = (LLMMessage("user", "BTC 怎么样"),)


async def _collect(llm, messages=_DEFAULT_MESSAGES, tools=()):
    deltas: list[str] = []
    done: LLMStreamDone | None = None
    async for event in llm.stream("你是数字人", messages, tools):
        if isinstance(event, LLMTextDelta):
            deltas.append(event.delta)
        elif isinstance(event, LLMStreamDone):
            done = event
    assert done is not None, "流必须以 LLMStreamDone 收尾"
    return deltas, done


async def test_text_arrives_as_increments() -> None:
    """首块内容在 PartStartEvent 里，只处理 delta 会丢掉开头几个字。"""
    llm = PydanticAILLM(FunctionModel(stream_function=_text_stream))
    deltas, done = await _collect(llm)

    assert deltas == ["比特", "币今天", "下跌 3%"]
    assert done.result.text == "比特币今天下跌 3%"


async def test_done_carries_the_full_result() -> None:
    llm = PydanticAILLM(FunctionModel(stream_function=_text_stream))
    _deltas, done = await _collect(llm)

    assert done.result.text
    assert done.result.tool_calls == ()
    assert done.result.usage is not None, "usage 要在流跑完后取，不能丢"
    assert done.result.usage.output_tokens > 0


async def test_tool_call_emits_no_text_and_parses_args_at_the_end() -> None:
    """工具调用轮不该吐文本增量；参数只在流结束后解析（中途是残缺 JSON）。"""
    llm = PydanticAILLM(FunctionModel(stream_function=_tool_stream))
    deltas, done = await _collect(llm, tools=("get_market_price",))

    assert deltas == []
    assert len(done.result.tool_calls) == 1
    call = done.result.tool_calls[0]
    assert call.name == "get_market_price"
    assert call.call_id == "c1"
    assert call.arguments == {"symbol": "BTC"}, "残缺 JSON 必须拼完整再解析"


async def test_stream_and_complete_agree_on_the_same_input() -> None:
    """流式与非流式对同一输入应给出一致的结果，否则两条路径会悄悄分叉。"""
    llm = PydanticAILLM(FunctionModel(stream_function=_tool_stream, function=_sync_tool))
    _deltas, done = await _collect(llm, tools=("get_market_price",))
    completed = await llm.complete(
        "你是数字人", (LLMMessage("user", "BTC 怎么样"),), ("get_market_price",)
    )

    assert done.result.tool_calls[0].name == completed.tool_calls[0].name
    assert done.result.tool_calls[0].arguments == completed.tool_calls[0].arguments


def _sync_tool(messages, info: AgentInfo):
    from pydantic_ai import ModelResponse, ToolCallPart

    return ModelResponse(
        parts=[
            ToolCallPart(
                tool_name="get_market_price", args={"symbol": "BTC"}, tool_call_id="c1"
            )
        ]
    )


async def test_native_tool_history_still_applies_when_streaming() -> None:
    """流式走的是同一套消息映射——原生 tool 协议不能只在非流式路径生效。"""
    seen: list = []

    async def _capture(messages, info: AgentInfo):
        seen.append(list(messages))
        yield "好的"

    llm = PydanticAILLM(FunctionModel(stream_function=_capture))
    from app.agents.llm import LLMToolCall

    await _collect(
        llm,
        messages=(
            LLMMessage("user", "BTC 多少钱"),
            LLMMessage("assistant", tool_calls=(LLMToolCall("c1", "get_market_price", {}),)),
            LLMMessage(
                "tool", content="92400", tool_call_id="c1", tool_name="get_market_price"
            ),
        ),
    )

    shape = [tuple(type(p).__name__ for p in m.parts) for m in seen[0]]
    assert shape == [
        ("SystemPromptPart", "UserPromptPart"),
        ("ToolCallPart",),
        ("ToolReturnPart",),
    ]


async def test_cancelling_mid_stream_propagates() -> None:
    """消费方取消时 CancelledError 必须照常传播，不能被吞掉。"""
    started = asyncio.Event()

    async def _slow_stream(messages, info: AgentInfo):
        yield "开头"
        started.set()
        await asyncio.sleep(10)
        yield "永远到不了"

    llm = PydanticAILLM(FunctionModel(stream_function=_slow_stream))

    async def _consume() -> None:
        async for _event in llm.stream("你是数字人", (LLMMessage("user", "q"),), ()):
            pass

    task = asyncio.create_task(_consume())
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_fake_llm_also_streams() -> None:
    """FakeLLM 是默认 provider，两条路径都要能走，否则本地自测覆盖不到流式。"""
    deltas, done = await _collect(FakeLLM(), (LLMMessage("user", "你好"),))

    assert "".join(deltas) == done.result.text
    assert done.result.text


async def test_early_break_still_records_partial_usage(caplog) -> None:
    """在收到终止事件之前就 break（用户中途取消），已经烧掉的 token 仍要记账。

    这条路径必须 aclose——生成器停在某个 delta 上，用量记录还没跑到。
    """

    async def _four_chunks(messages, info: AgentInfo):
        for chunk in ("第一段", "第二段", "第三段", "第四段"):
            yield chunk

    llm = PydanticAILLM(FunctionModel(stream_function=_four_chunks))
    agen = llm.stream("你是数字人", (LLMMessage("user", "q"),), ())

    received = []
    with caplog.at_level(logging.INFO, logger="app.agents.llm"):
        async for event in agen:
            if isinstance(event, LLMTextDelta):
                received.append(event.delta)
                if len(received) == 2:
                    break
        await agen.aclose()

    assert received == ["第一段", "第二段"], "中断点之前的内容要照常吐出"
    usage_lines = [r.message for r in caplog.records if r.message.startswith("llm_usage")]
    assert usage_lines, "中断路径必须留下用量记录，否则这部分成本完全不可见"
    # 只生成了两段就断了，用量应当反映实际生成量而不是完整的四段
    assert "output=2" in usage_lines[0], usage_lines[0]


async def test_stream_is_not_a_coroutine() -> None:
    """stream() 是 async generator 函数，直接返回迭代器。

    写成 `await llm.stream(...)` 会拿到生成器对象本身而不是结果——把这个契约
    钉死，免得调用方按 complete 的习惯去 await 它。
    """
    llm = PydanticAILLM(FunctionModel(stream_function=_text_stream))
    agen = llm.stream("你是数字人", (LLMMessage("user", "q"),), ())

    assert hasattr(agen, "__aiter__")
    assert hasattr(agen, "aclose")
    await agen.aclose()


async def test_span_closes_before_the_consumer_sees_done() -> None:
    """收到 LLMStreamDone 就 break，是最自然的消费方式——那时 span 必须已经关掉。

    回归：终止事件原先 yield 在 `with llm_span(...)` 内部，消费方 break 后生成器
    停在那个 yield 上，span 一直不关。清理只能等 GC，而 GC 跑在**另一个
    contextvars.Context** 里，detach 会失败（OTel 内部吞掉一个 ValueError），
    结果是后续同一个 task 里开的无关 span 被错误地挂成这个悬空 span 的子节点，
    整棵 trace 树就此错乱。

    改成把终止事件 yield 到 with 块之外，span 关闭就与消费方的行为无关了。
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    original = otel_trace.get_tracer_provider()
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    try:
        llm = PydanticAILLM(FunctionModel(stream_function=_text_stream))
        async for event in llm.stream("你是数字人", (LLMMessage("user", "q"),), ()):
            if isinstance(event, LLMStreamDone):
                break  # 刻意不 aclose——这正是要防的消费方式

        finished = [s for s in exporter.get_finished_spans() if s.name.startswith("chat ")]
        assert finished, "消费方拿到终止事件时 span 应该已经结束，而不是悬在 yield 上"
        assert finished[0].attributes.get("gen_ai.usage.output_tokens", 0) > 0, (
            "用量属性要在 span 关闭前挂上"
        )
    finally:
        otel_trace._TRACER_PROVIDER = original  # type: ignore[attr-defined]


async def _stream_with_empty_chunks(messages, info: AgentInfo):
    """provider 发空块是常见行为（心跳、角色声明、思考占位）。"""
    for chunk in ("", "", "真正的", "", "第一个字"):
        yield chunk


async def test_empty_chunks_are_not_emitted_as_deltas() -> None:
    """空增量不该往上吐。

    PartStartEvent 那个分支本来就判了 content 非空，PartDeltaEvent 这边漏了；
    漏出去的后果是下游把「首字到了」判早——数字人提前切 Speaking，客户端
    掐表算出的「已思考 N 秒」也会偏小。
    """
    llm = PydanticAILLM(FunctionModel(stream_function=_stream_with_empty_chunks))
    deltas, done = await _collect(llm)

    assert deltas == ["真正的", "第一个字"]
    assert done.result.text == "真正的第一个字", "过滤空块不能改变最终文本"
