"""回合级超时兜底。

现状是每一层各有各的超时（Tavily 5s、Core 10s + 2 次重试、证据抓取 6s 软超时、
LLM 120s），但**整个回合没有上限**。最坏情况是 4 步循环各自把自己的预算用满，
用户对着输入框等好几分钟——比拿到一句"这次没查到"糟糕得多。

这一层给回合封顶。超时不是报错：已经吐出去的内容照常保留，只是不再等下去。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest

from app.agents.runner import RunResult, TurnDone, TurnEvent, TurnTextDelta
from app.runtime.turn_manager import with_turn_timeout


def _timeout_result(text: str) -> RunResult:
    """直接测 with_turn_timeout 时用的最简超时结果构造器。

    真实调用方（AgentRunner）传的版本会带上已经执行成功的工具——包装层看不到
    那些（它们走 on_tool_end 钩子，不在事件流里），所以必须由调用方提供。
    """

    return RunResult(text=text, executed_tools=(), blockers=("TURN_TIMEOUT",))


async def _drain(events: AsyncGenerator[TurnEvent, None]) -> tuple[list[str], TurnDone]:
    deltas: list[str] = []
    done: TurnDone | None = None
    async for event in events:
        if isinstance(event, TurnTextDelta):
            deltas.append(event.delta)
        elif isinstance(event, TurnDone):
            done = event
    assert done is not None, "超时与否都必须以 TurnDone 收尾"
    return deltas, done


async def _fast_turn() -> AsyncGenerator[TurnEvent, None]:
    yield TurnTextDelta(delta="很快")
    yield TurnTextDelta(delta="就好了")
    yield TurnDone(result=RunResult(text="很快就好了", executed_tools=()))


async def test_fast_turn_passes_through_untouched() -> None:
    """没超时的回合原样透传，不该有任何多余事件。"""
    deltas, done = await _drain(with_turn_timeout(_fast_turn(), timeout_seconds=5, on_timeout=_timeout_result))

    assert deltas == ["很快", "就好了"]
    assert done.result.text == "很快就好了"
    assert done.result.blockers == ()


async def test_timeout_before_any_output_gives_a_plain_explanation() -> None:
    """一个字都没吐出来就超时：给一句人话，别让用户对着空白。"""

    async def _stuck() -> AsyncGenerator[TurnEvent, None]:
        await asyncio.sleep(10)
        yield TurnDone(result=RunResult(text="永远到不了", executed_tools=()))

    deltas, done = await _drain(with_turn_timeout(_stuck(), timeout_seconds=0.05, on_timeout=_timeout_result))

    assert "".join(deltas) == done.result.text, "超时文案也要作为增量吐出来"
    assert done.result.text, "不能给一条空消息"
    assert done.result.blockers == ("TURN_TIMEOUT",)


async def test_timeout_after_partial_output_keeps_what_was_said() -> None:
    """已经说了半句才超时：保留已说的部分，不要再追加一段解释把它冲淡。"""

    async def _slow_after_first() -> AsyncGenerator[TurnEvent, None]:
        yield TurnTextDelta(delta="已经说出口的半句话")
        await asyncio.sleep(10)
        yield TurnDone(result=RunResult(text="永远到不了", executed_tools=()))

    deltas, done = await _drain(
        with_turn_timeout(_slow_after_first(), timeout_seconds=0.05, on_timeout=_timeout_result)
    )

    assert deltas == ["已经说出口的半句话"], "不该在已有内容后面再补一段超时说明"
    assert done.result.text == "已经说出口的半句话"
    assert done.result.blockers == ("TURN_TIMEOUT",)


async def test_timeout_budget_spans_the_whole_turn_not_each_event() -> None:
    """预算是整轮的，不是每个事件各给一份。

    按事件计时的话，一个稳定每 0.9 秒吐一个字的回合可以无限跑下去。
    """

    async def _steady_drip() -> AsyncGenerator[TurnEvent, None]:
        for index in range(20):
            await asyncio.sleep(0.03)
            yield TurnTextDelta(delta=str(index))
        yield TurnDone(result=RunResult(text="全部说完", executed_tools=()))

    deltas, done = await _drain(with_turn_timeout(_steady_drip(), timeout_seconds=0.12, on_timeout=_timeout_result))

    assert len(deltas) < 20, "整轮预算用完就该停，不能被稳定的小间隔拖住"
    assert done.result.blockers == ("TURN_TIMEOUT",)


async def test_timeout_closes_the_underlying_stream() -> None:
    """超时后底层流必须处于关闭状态。

    回合流底下套着 LLM 流（带 span 和 HTTP 连接），不关的话上游还在生成、
    还在计费——这是前面几个 PR 反复踩的同一类问题。

    这条测试守的是**结果**而不是机制：超时发生在 await 内部时，wait_for 取消
    __anext__ 本身就会触发内层的 finally，所以去掉 with_turn_timeout 里那句
    显式 aclose 它也照样通过（实测确认过）。显式 aclose 覆盖的是另一条路径——
    预算恰好在两次 yield 之间耗尽，此时没有待取消的 wait_for——那条路径依赖
    调度时机，很难在测试里稳定构造，所以留作防御而不是被这条测试证明。
    """
    closed = False

    async def _slow_yields() -> AsyncGenerator[TurnEvent, None]:
        nonlocal closed
        try:
            # 每次 yield 之间只睡一点点：预算耗尽时生成器正干净地挂在 yield 上，
            # 走的是 remaining<=0 那条快路径——此时没有待取消的 wait_for，
            # 关闭完全依赖 finally 里那句显式 aclose。
            for index in range(50):
                await asyncio.sleep(0.005)
                yield TurnTextDelta(delta=str(index))
            yield TurnDone(result=RunResult(text="到不了", executed_tools=()))
        finally:
            closed = True

    # 必须持有内层生成器的引用：直接把 _slow_yields() 传进去的话，它出了作用域
    # 就被引用计数回收，asyncgen 终结器会替我们跑掉 finally——测试于是恒真，
    # 去掉显式 aclose 也照样通过。持有引用才能验证"返回时就已经关了"。
    inner = _slow_yields()
    await _drain(
        with_turn_timeout(inner, timeout_seconds=0.05, on_timeout=_timeout_result)
    )

    assert closed, "超时后底层流必须被显式关闭，而不是留给 GC"


async def test_disabled_when_timeout_is_zero() -> None:
    """0 表示不限时——留给排查场景，别让超时本身挡住诊断。"""
    deltas, done = await _drain(with_turn_timeout(_fast_turn(), timeout_seconds=0, on_timeout=_timeout_result))

    assert deltas == ["很快", "就好了"]
    assert done.result.blockers == ()


async def test_cancellation_still_propagates() -> None:
    """用户中断优先于超时——CancelledError 不能被超时逻辑吞掉。"""

    async def _stuck() -> AsyncGenerator[TurnEvent, None]:
        await asyncio.sleep(10)
        yield TurnDone(result=RunResult(text="到不了", executed_tools=()))

    async def _consume() -> None:
        async for _event in with_turn_timeout(_stuck(), timeout_seconds=30, on_timeout=_timeout_result):
            pass

    task = asyncio.create_task(_consume())
    await asyncio.sleep(0.02)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# ---- 端到端：超时是不是真的接到了 runner 上 ----


def _stuck_runner(timeout_seconds: float):
    """构造一个会卡在工具执行里的 runner。"""
    from datetime import UTC, datetime

    from app.agents.llm import LLMResult, LLMToolCall
    from app.agents.runner import AgentProfile, AgentRunner
    from app.audit.store import InMemoryAuditStore
    from app.runtime.session import Scope, Session

    class _StuckExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            await asyncio.sleep(10)
            return {}

    class _ToolThenText:
        async def complete(self, system, messages, tools):
            if any(m.role == "tool" for m in messages):
                return LLMResult(text="到不了")
            return LLMResult(text="", tool_calls=(LLMToolCall("c1", "get_positions", {}),))

    now = datetime(2026, 9, 2, tzinfo=UTC)
    session = Session(
        session_id="s_timeout",
        user_id="u1",
        modality="text",
        scope=Scope(agents=("avatar",), tools=("get_positions",)),
        state="text_active",
        created_at=now,
        last_activity_at=now,
    )
    runner = AgentRunner(
        AgentProfile("avatar", "fake", "你是数字人", ("get_positions",)),
        _StuckExecutor(),
        _ToolThenText(),
        InMemoryAuditStore(),
        turn_timeout_seconds=timeout_seconds,
    )
    return runner, session, now


async def test_runner_applies_the_turn_budget() -> None:
    """卡在工具执行里的回合会被整轮预算掐掉，而不是等工具自己的超时。"""
    runner, session, now = _stuck_runner(0.05)

    started = asyncio.get_running_loop().time()
    _deltas, done = await _drain(
        runner.run_turn_stream(session, "帮我看下持仓", "trace_timeout", now)
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert done.result.blockers == ("TURN_TIMEOUT",)
    assert elapsed < 2, f"应当被预算掐断，实际等了 {elapsed:.2f}s"


async def test_run_turn_aggregates_a_timed_out_turn() -> None:
    """REST 走的 run_turn 也受益——它内部消费同一条流。"""
    runner, session, now = _stuck_runner(0.05)

    result = await runner.run_turn(session, "帮我看下持仓", "trace_timeout_agg", now)

    assert result.blockers == ("TURN_TIMEOUT",)
    assert result.text, "超时也要给用户一句话，不能返回空回复"


async def test_zero_budget_disables_the_limit() -> None:
    """默认构造（不传 turn_timeout_seconds）不限时，行为与改造前一致。

    断言行为而不是读私有属性：只看 _turn_timeout_seconds == 0 的话，即便
    run_turn_stream 把它忘了传给 with_turn_timeout，测试也照样通过。
    """
    runner, session, now = _stuck_runner(0.0)

    # 工具会卡 10 秒，不限时的话这里会真的等下去——用 wait_for 兜住，
    # 超时说明预算确实没生效（这正是期望）。
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            _drain(runner.run_turn_stream(session, "帮我看下持仓", "trace_no_limit", now)),
            timeout=0.3,
        )


async def test_timeout_keeps_tools_that_already_succeeded() -> None:
    """超时不能把已经成功的工具从结果里抹掉。

    真实后果：第一步成功建了交易草案，第二步卡住撞上预算——REST 那条路径只看
    RunResult.executed_tools 来组装 tool_calls（含 trade_draft_id），抹掉的话
    客户端完全不知道草案已经建好了，用户很可能重试，于是建出第二张草案。

    WS 那条路径不受影响（工具结果走 on_tool_end 钩子实时推），但 REST 只有
    这一个来源。
    """
    from datetime import UTC, datetime

    from app.agents.llm import LLMResult, LLMToolCall
    from app.agents.runner import AgentProfile, AgentRunner
    from app.audit.store import InMemoryAuditStore
    from app.runtime.session import Scope, Session

    class _DraftThenStuck:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"trade_draft_id": "td_abc123", "status": "PENDING_USER"}
            await asyncio.sleep(10)
            return {}

    class _TwoToolLLM:
        def __init__(self) -> None:
            self.step = 0

        async def complete(self, system, messages, tools):
            self.step += 1
            if self.step == 1:
                return LLMResult(
                    text="", tool_calls=(LLMToolCall("c1", "create_trade_draft", {}),)
                )
            return LLMResult(text="", tool_calls=(LLMToolCall("c2", "get_positions", {}),))

    now = datetime(2026, 9, 2, tzinfo=UTC)
    session = Session(
        session_id="s_draft",
        user_id="u1",
        modality="text",
        scope=Scope(agents=("avatar",), tools=("create_trade_draft", "get_positions")),
        state="text_active",
        created_at=now,
        last_activity_at=now,
    )
    runner = AgentRunner(
        AgentProfile(
            "avatar", "fake", "你是数字人", ("create_trade_draft", "get_positions")
        ),
        _DraftThenStuck(),
        _TwoToolLLM(),
        InMemoryAuditStore(),
        turn_timeout_seconds=0.1,
    )

    result = await runner.run_turn(
        session, "永续做多 0.1 BTC，限价 90000", "trace_draft_timeout", now
    )

    assert result.blockers[-1] == "TURN_TIMEOUT"
    assert [t.name for t in result.executed_tools] == ["create_trade_draft"], (
        "已经建好的草案不能因为后面超时就从结果里消失"
    )
    assert result.executed_tools[0].trade_draft_id == "td_abc123"


# ---- 配置约束与装配接线 ----


def test_turn_budget_must_exceed_the_llm_breaker() -> None:
    """两个默认值是分别调出来的，很容易在后续调整时漂移到互相打架。

    回合预算小于单步 LLM 熔断时，一次合法的慢推理（opus 的隐藏 reasoning token
    会吃掉大半预算，120s 那个熔断就是为它调的）会先被回合超时砍掉——表现是用户
    永远拿不到那类问题的答案，日志里只有一句 TURN_TIMEOUT。宁可启动就失败。
    """
    import pydantic

    from app.config import Settings

    with pytest.raises(pydantic.ValidationError, match="turn_timeout_seconds"):
        Settings(
            _env_file=None,
            provider_mode="fake",
            turn_timeout_seconds=60,
            llm_timeout_seconds=120,
        )


def test_default_turn_budget_leaves_room_for_one_slow_call() -> None:
    from app.config import Settings

    settings = Settings(_env_file=None, provider_mode="fake")

    assert settings.turn_timeout_seconds > settings.llm_timeout_seconds


def test_zero_budget_skips_the_constraint() -> None:
    """0 表示不限时，不该被"必须大于 LLM 超时"卡住。"""
    from app.config import Settings

    settings = Settings(_env_file=None, provider_mode="fake", turn_timeout_seconds=0)

    assert settings.turn_timeout_seconds == 0


def test_container_wires_the_budget_into_the_runner() -> None:
    """装配接线：配置改了但没传到 runner 是这个项目踩过的老坑。"""
    from app.config import Settings
    from app.container import build_container

    settings = Settings(
        _env_file=None, provider_mode="fake", redis_url="", turn_timeout_seconds=150
    )
    container = build_container(settings)

    assert container.runner._turn_timeout_seconds == 150
