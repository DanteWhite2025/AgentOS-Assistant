"""回合管理 —— 管理一次对话回合的生命周期（用户输入 → 工具循环 → 响应）。

回合循环本身仍在 app/agents/runner.py 的 AgentRunner。这里放的是**跨越整个回合**
的横切关注点，第一个是超时。

为什么需要回合级超时：每一层都各有各的预算（Tavily 5s、Core 10s + 2 次重试、
证据抓取 6s 软超时、LLM 120s），但没有任何一层管得住"整轮加起来多久"。最坏
情况是 4 步循环各自把预算用满，用户对着输入框等好几分钟——比拿到一句"这次没
查到"糟糕得多。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing

from app.agents.runner import RunResult, TurnDone, TurnEvent, TurnTextDelta

logger = logging.getLogger(__name__)

# 超时不是报错。产品口径是少报错，所以这里给的是一句人话，不是错误码；
# 真正的 TURN_TIMEOUT 只进 blockers 供排查。
TURN_TIMEOUT_TEXT = "这次想得有点久，先中断一下。你可以换个说法再问我一次。"


async def with_turn_timeout(
    events: AsyncGenerator[TurnEvent, None],
    *,
    timeout_seconds: float,
    on_timeout: Callable[[str], RunResult],
    trace_id: str = "",
) -> AsyncGenerator[TurnEvent, None]:
    """给一个回合流封顶。

    预算跨越**整轮**而不是每个事件各给一份——按事件计时的话，一个稳定每 0.9 秒
    吐一个字的回合可以无限跑下去。

    超时后的处理分两种，取决于用户已经看到了什么：

    - **一个字都没吐过**：给一句人话解释，否则用户对着空白不知道发生了什么。
    - **已经说了半句**：保留已说的部分就收尾，不再追加解释——那只会把已经读到
      的内容冲淡，而且用户看得到「话说了一半停了」，本身就是信息。

    两种情况都把 ``TURN_TIMEOUT`` 记进 blockers 供排查。

    超时结果由调用方通过 ``on_timeout`` 构造——这一层看不到回合内部已经执行成功
    的工具（那些走的是 on_tool_end 钩子，不在事件流里）。自己拍一个
    ``executed_tools=()`` 会把已经建好的交易草案从结果里抹掉，而 REST 那条路径
    只有这一个来源，客户端会以为什么都没发生然后重试。

    ``timeout_seconds`` 为 0 表示不限时，留给排查场景——别让超时本身挡住诊断。
    """

    if timeout_seconds <= 0:
        # 不限时也要负责关闭：async for 提前退出不会自动关掉被迭代的生成器，
        # 而这条链底下套着 LLM 流（带 span 和 HTTP 连接）。这个早退分支最容易
        # 被忘掉——它看起来只是"原样透传"。
        async with aclosing(events) as passthrough:
            async for event in passthrough:
                yield event
        return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    # 累积已产出的文本。WS 侧自己按 delta 拼，但 run_turn（REST 走的那条）只看
    # TurnDone.result.text——超时时把它置空会让 REST 返回一条空回复，而用户其实
    # 已经收到半句话了。
    produced: list[str] = []
    iterator = events.__aiter__()

    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                for event in _timeout_events(produced, on_timeout, trace_id, timeout_seconds):
                    yield event
                return
            try:
                event = await asyncio.wait_for(iterator.__anext__(), remaining)
            except StopAsyncIteration:
                return
            except TimeoutError:
                if loop.time() < deadline:
                    # 不是我们的预算到了——是回合内部漏出来的真 TimeoutError。
                    # 当成 TURN_TIMEOUT 会把真正的故障原因盖掉。
                    raise
                for event in _timeout_events(produced, on_timeout, trace_id, timeout_seconds):
                    yield event
                return
            if isinstance(event, TurnTextDelta):
                produced.append(event.delta)
            yield event
    finally:
        # 底下套着 LLM 流（带 span 和 HTTP 连接）。不关的话上游还在生成、还在
        # 计费——前面几个 PR 反复踩的就是这一类问题，这里不能再漏一次。
        await events.aclose()


def _timeout_events(
    produced: list[str],
    on_timeout: Callable[[str], RunResult],
    trace_id: str,
    timeout_seconds: float,
) -> list[TurnEvent]:
    logger.warning(
        "turn_timeout trace_id=%s budget_seconds=%s produced_chars=%d",
        trace_id,
        timeout_seconds,
        sum(len(chunk) for chunk in produced),
    )
    if produced:
        # 已经说出口的内容不重复吐一遍 delta（消费方按增量渲染，再补一次会重影），
        # 但结果里要带上——run_turn 聚合出来的 text 只有这一个来源。
        return [TurnDone(result=on_timeout("".join(produced)))]
    return [
        TurnTextDelta(delta=TURN_TIMEOUT_TEXT),
        TurnDone(result=on_timeout(TURN_TIMEOUT_TEXT)),
    ]
