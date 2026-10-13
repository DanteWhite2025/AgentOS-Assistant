"""AgentRunner 的流式回合。

`run_turn` 保留聚合语义（消费流再组装 RunResult），现有约 85 处调用和 8 个断言
文件因此零改动；WebSocket 走新的 `run_turn_stream`。

要盯住的是**出口的一致性**：run_turn 有 8 个 return 点，其中两个在任何 LLM 调用
之前就返回固定文案（市场类型待澄清、交易意图不完整）。它们也必须走同一条 yield
通道，否则消费方要处理两种响应形态。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from app.agents.llm import LLMResult, LLMToolCall
from app.agents.runner import (
    AgentProfile,
    AgentRunner,
    RunResult,
    TurnDone,
    TurnTextDelta,
)
from app.audit.store import InMemoryAuditStore
from app.runtime.session import Scope, Session


def _now() -> datetime:
    return datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)


def _session(tools: tuple[str, ...]) -> Session:
    return Session(
        session_id="sess_stream",
        user_id="u1",
        modality="text",
        scope=Scope(agents=("avatar",), tools=tools),
        state="text_active",
        created_at=_now(),
        last_activity_at=_now(),
    )


class _StreamingLLM:
    """按脚本先调工具再出文本，文本以增量形式吐出。"""

    def __init__(self, tool_name: str | None, chunks: tuple[str, ...]) -> None:
        self._tool_name = tool_name
        self._chunks = chunks
        self.stream_calls = 0

    async def complete(self, system, messages, tools) -> LLMResult:
        raise AssertionError("流式路径不该退回 complete()")

    async def stream(self, system, messages, tools):
        self.stream_calls += 1
        from app.agents.llm import LLMStreamDone, LLMTextDelta

        has_tool_result = any(m.role == "tool" for m in messages)
        if self._tool_name and not has_tool_result and self._tool_name in tools:
            yield LLMStreamDone(
                result=LLMResult(
                    text="", tool_calls=(LLMToolCall("c1", self._tool_name, {}),)
                )
            )
            return
        for chunk in self._chunks:
            yield LLMTextDelta(delta=chunk)
        yield LLMStreamDone(result=LLMResult(text="".join(self._chunks)))


class _OkExecutor:
    async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
        return {"positions": []}


class _RaisingExecutor:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
        raise self._exc


def _runner(llm, executor, tools: tuple[str, ...]) -> AgentRunner:
    return AgentRunner(
        AgentProfile("avatar", "fake", "你是数字人", tools), executor, llm, InMemoryAuditStore()
    )


async def _drain(runner, text: str, tools: tuple[str, ...], **kwargs):
    deltas: list[str] = []
    done: TurnDone | None = None
    async for event in runner.run_turn_stream(
        _session(tools), text, "trace_stream", _now(), **kwargs
    ):
        if isinstance(event, TurnTextDelta):
            deltas.append(event.delta)
        elif isinstance(event, TurnDone):
            done = event
    assert done is not None, "流必须以 TurnDone 收尾"
    return deltas, done


async def test_text_streams_incrementally() -> None:
    llm = _StreamingLLM(None, ("你当前", "持有 1.2", " ETH"))
    runner = _runner(llm, _OkExecutor(), ("get_positions",))

    deltas, done = await _drain(runner, "随便聊聊", ("get_positions",))

    assert deltas == ["你当前", "持有 1.2", " ETH"]
    assert done.result.text == "你当前持有 1.2 ETH"


async def test_tool_round_emits_no_text_then_streams_the_answer() -> None:
    """工具轮不产出文本；工具跑完后的那一轮才是用户看到的内容。"""
    llm = _StreamingLLM("get_positions", ("查到了：", "1.2 ETH"))
    runner = _runner(llm, _OkExecutor(), ("get_positions",))

    deltas, done = await _drain(runner, "帮我看下持仓", ("get_positions",))

    assert deltas == ["查到了：", "1.2 ETH"]
    assert llm.stream_calls == 2, "一轮工具 + 一轮出文本"
    assert [t.name for t in done.result.executed_tools] == ["get_positions"]


@pytest.mark.parametrize(
    ("question", "expected_fragment"),
    [
        ("买 100 个 BTC", "现货还是永续合约"),
        ("永续做多 BTC 2 倍杠杆", "目标价格"),
    ],
)
async def test_pre_llm_early_exits_go_through_the_same_channel(
    question: str, expected_fragment: str
) -> None:
    """两个在任何 LLM 调用之前就返回固定文案的早退分支同样要走 yield 通道。

    否则消费方要处理两种响应形态：有的轮次是事件流，有的轮次直接给结果。
    """
    llm = _StreamingLLM(None, ("不该走到这里",))
    runner = _runner(llm, _OkExecutor(), ("create_trade_draft",))

    deltas, done = await _drain(runner, question, ("create_trade_draft",))

    assert llm.stream_calls == 0, "这两条分支根本不该调模型"
    assert expected_fragment in done.result.text
    assert "".join(deltas) == done.result.text, "固定文案也要作为增量吐出来"


async def test_blocked_tool_ends_the_stream_with_its_own_copy() -> None:
    """写工具失败仍然终止本轮，终止事件里带的是给用户看的文案。"""
    llm = _StreamingLLM("create_trade_draft", ("不该走到这里",))
    runner = _runner(
        llm, _RaisingExecutor(httpx.TimeoutException("t")), ("create_trade_draft",)
    )

    deltas, done = await _drain(
        runner, "永续做多 0.1 BTC，限价 90000", ("create_trade_draft",)
    )

    assert "未确认的草稿不会自动成交" in done.result.text
    assert "".join(deltas) == done.result.text


async def test_run_turn_still_returns_an_aggregated_result() -> None:
    """run_turn 保留聚合语义——现有调用方一行都不用改。"""
    llm = _StreamingLLM("get_positions", ("查到了：", "1.2 ETH"))
    runner = _runner(llm, _OkExecutor(), ("get_positions",))

    result = await runner.run_turn(
        _session(("get_positions",)), "帮我看下持仓", "trace_agg", _now()
    )

    assert isinstance(result, RunResult)
    assert result.text == "查到了：1.2 ETH"
    assert [t.name for t in result.executed_tools] == ["get_positions"]


async def test_tool_lifecycle_hooks_still_fire_when_streaming() -> None:
    """工具事件仍走钩子，不混进文本流——WebSocket 侧的两条通道保持分离。"""
    started: list[str] = []
    ended: list[str] = []

    async def _on_start(call_id, name, arguments):
        started.append(name)

    async def _on_end(outcome):
        ended.append(outcome.status)

    llm = _StreamingLLM("get_positions", ("好的",))
    runner = _runner(llm, _OkExecutor(), ("get_positions",))

    await _drain(
        runner,
        "帮我看下持仓",
        ("get_positions",),
        on_tool_start=_on_start,
        on_tool_end=_on_end,
    )

    assert started == ["get_positions"]
    assert ended == ["ok"]


async def test_preamble_before_a_tool_call_is_streamed_but_not_in_final_text() -> None:
    """模型在调工具前先说一句「我查一下」时，那段前言会进 delta 流。

    它不进 RunResult.text（那里只有最后一轮的答案）——消费方要按 delta 累积
    渲染，不能拿 RunResult.text 去覆盖已经显示的内容。这个契约钉在这里，
    WebSocket 侧的 response.text.done 要据此决定发什么。
    """
    from app.agents.llm import LLMStreamDone, LLMTextDelta

    class _PreambleLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, system, messages, tools):
            raise AssertionError("不该走 complete")

        async def stream(self, system, messages, tools):
            self.calls += 1
            if self.calls == 1:
                yield LLMTextDelta(delta="我查一下行情。")
                yield LLMStreamDone(
                    result=LLMResult(
                        text="我查一下行情。",
                        tool_calls=(LLMToolCall("c1", "get_positions", {}),),
                    )
                )
                return
            yield LLMTextDelta(delta="你持有 1.2 ETH")
            yield LLMStreamDone(result=LLMResult(text="你持有 1.2 ETH"))

    runner = _runner(_PreambleLLM(), _OkExecutor(), ("get_positions",))
    deltas, done = await _drain(runner, "帮我看下持仓", ("get_positions",))

    assert deltas == ["我查一下行情。", "你持有 1.2 ETH"], "前言也要吐给用户"
    assert done.result.text == "你持有 1.2 ETH", "最终结果只含最后一轮的答案"


async def test_closing_the_turn_stream_closes_the_llm_stream_too() -> None:
    """关掉外层就要关掉全部内层。

    回归：PR「LLM 层真流式」刚修过一个同类问题——`async for` 提前退出**不会**
    自动关闭被迭代的 async generator，全靠调用方显式 aclose。run_turn_stream
    套了两层生成器（自己 → _stream_llm → provider.stream），任何一层漏了都会
    让底下那层带着 span 和 HTTP 连接挂在原地，等不可靠的 GC 终结。

    这个契约必须在 runner 这一层钉死，不能指望 WebSocket 层自己做对。
    """
    from app.agents.llm import LLMStreamDone, LLMTextDelta

    class _TrackingLLM:
        def __init__(self) -> None:
            self.closed = False

        async def complete(self, system, messages, tools):
            raise AssertionError("不该走 complete")

        async def stream(self, system, messages, tools):
            try:
                yield LLMTextDelta(delta="第一段")
                yield LLMTextDelta(delta="第二段")
                yield LLMStreamDone(result=LLMResult(text="第一段第二段"))
            finally:
                self.closed = True

    llm = _TrackingLLM()
    runner = _runner(llm, _OkExecutor(), ())

    agen = runner.run_turn_stream(_session(()), "随便聊聊", "trace_close", _now())
    async for event in agen:
        if isinstance(event, TurnTextDelta):
            break  # 中途不要了，模拟用户取消
    await agen.aclose()

    assert llm.closed, "关闭 run_turn_stream 必须一路关到 provider 的流"


async def test_fallback_provider_still_produces_a_delta() -> None:
    """降级路径也要吐增量。

    回归：只实现 complete 的 provider 在「这一轮没调工具」时（最常见的收尾轮）
    会走主路径直接 yield TurnDone，全程零 delta——消费方就遇到了两种形态：
    有的轮次有增量、有的只有终止事件。这正是 _finish 的注释要杜绝的情况。
    """

    class _LegacyLLM:
        async def complete(self, system, messages, tools):
            return LLMResult(text="老接口的回答")

    runner = _runner(_LegacyLLM(), _OkExecutor(), ())
    deltas, done = await _drain(runner, "随便聊聊", ())

    assert done.result.text == "老接口的回答", "只实现 complete 的 provider 仍能跑"
    assert "".join(deltas) == done.result.text, "增量拼起来要等于最终文本"


async def test_web_search_unavailable_exit_is_consistent() -> None:
    """_finish 的另一个复用点：联网不可用。与其他出口走同一条通道。"""
    llm = _StreamingLLM(None, ("不该走到这里",))
    # profile 里没有 web_search，但请求要求联网
    runner = _runner(llm, _OkExecutor(), ("get_positions",))

    deltas, done = await _drain(
        runner, "查下最新消息", ("get_positions",), web_search=True
    )

    assert done.result.blockers == ("WEB_SEARCH_NOT_AVAILABLE",)
    assert "".join(deltas) == done.result.text
    assert llm.stream_calls == 0


async def test_stream_announces_thinking_before_any_work() -> None:
    """一轮开头就要有阶段事件——那段等待现在客户端完全看不见。"""
    from app.agents.runner import TurnStage

    llm = _StreamingLLM(None, ("好",))
    runner = _runner(llm, _OkExecutor(), ("get_positions",))

    events = [
        e
        async for e in runner.run_turn_stream(
            _session(("get_positions",)), "你好", "trace_stage", _now()
        )
    ]
    assert isinstance(events[0], TurnStage)
    assert events[0].stage == "thinking"


async def test_stream_announces_composing_after_tools_return() -> None:
    """工具跑完到答案出来之间是最长的一段静默，必须有事件。"""
    from app.agents.runner import TurnStage

    llm = _StreamingLLM("get_positions", ("你持有 1.2 ETH",))
    runner = _runner(llm, _OkExecutor(), ("get_positions",))

    stages = [
        e.stage
        async for e in runner.run_turn_stream(
            _session(("get_positions",)), "帮我看下持仓", "trace_stage2", _now()
        )
        if isinstance(e, TurnStage)
    ]
    assert stages == ["thinking", "composing"]


async def test_no_tool_round_does_not_announce_composing() -> None:
    """没调工具就直接出答案的轮次没有「整理结果」这一步，不该凭空报一个。"""
    from app.agents.runner import TurnStage

    llm = _StreamingLLM(None, ("你好",))
    runner = _runner(llm, _OkExecutor(), ("get_positions",))

    stages = [
        e.stage
        async for e in runner.run_turn_stream(
            _session(("get_positions",)), "你好", "trace_stage3", _now()
        )
        if isinstance(e, TurnStage)
    ]
    assert stages == ["thinking"]


async def test_early_exit_before_llm_still_announces_thinking() -> None:
    """LLM 之前就返回固定文案的那两个出口也走同一条通道，
    否则消费方要处理「没有阶段事件」的第三种形态。"""
    from app.agents.runner import TurnStage

    llm = _StreamingLLM(None, ("不该走到这里",))
    runner = _runner(llm, _OkExecutor(), ())

    events = [
        e
        async for e in runner.run_turn_stream(
            _session(()), "买 0.1", "trace_stage4", _now()
        )
    ]
    assert any(isinstance(e, TurnStage) and e.stage == "thinking" for e in events)
