"""评测集的结构层 —— 每道测题跑一遍，检查发给模型的消息结构合法。

这是 PR「Tool 协议原生化」的验收标准，也是防回归的护栏：结构坏掉（连续两条
user 消息、孤儿工具调用、工具结果伪装成用户输入）在任何模型下都是 bug，与
提示词和模型选择无关，所以可以用 FunctionModel 离线确定性地跑。

质量层（真实模型的回答好不好）在 evaluations/run_baseline.py，需要 key，手动跑。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai import models as pai_models
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from app.agents.llm import PydanticAILLM
from app.agents.runner import AgentProfile, AgentRunner
from app.audit.store import InMemoryAuditStore
from app.runtime.session import Scope, Session
from app.tools.builtin import build_default_registry
from app.tools.core_client import FakeCoreClient
from app.tools.executor import ToolExecutor
from evaluations.cases import CASES, EvalCase
from evaluations.prompt_shape import check, describe

pai_models.ALLOW_MODEL_REQUESTS = False


def _now() -> datetime:
    return datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)


def _session(tools: tuple[str, ...]) -> Session:
    return Session(
        session_id="eval_session",
        user_id="eval_user",
        modality="text",
        scope=Scope(agents=("avatar",), tools=tools),
        state="text_active",
        created_at=_now(),
        last_activity_at=_now(),
    )


class _ScriptedModel:
    """按测题的期望调用工具，拿到结果后出文本。

    刻意不用 FakeLLM：那条路径不经过 PydanticAILLM 的消息映射，而映射正是这里
    要检查的东西。
    """

    __name__ = "scripted_eval_model"

    def __init__(self, case: EvalCase) -> None:
        self._case = case
        self.seen: list[list] = []

    async def stream(self, messages, info: AgentInfo):
        """把同步脚本的响应转成流式形状。

        runner 现在走 provider 的 stream()，FunctionModel 必须同时提供
        stream_function 才支持流式请求。
        """

        response = self(messages, info)
        for index, part in enumerate(response.parts):
            if type(part).__name__ == "TextPart":
                yield part.content
            elif type(part).__name__ == "ToolCallPart":
                # key 是 vendor_part_id，并发调用必须各用各的 index——都用 0 的话
                # 它们会被当成同一个 part 的增量拼接起来，工具名直接连成
                # "get_balancesget_positions"。
                yield {
                    index: DeltaToolCall(
                        name=part.tool_name,
                        json_args="{}",
                        tool_call_id=part.tool_call_id,
                    )
                }

    def __call__(self, messages, info: AgentInfo) -> ModelResponse:
        self.seen.append(list(messages))
        already_called = {
            p.tool_name for m in messages for p in m.parts if type(p).__name__ == "ToolCallPart"
        }
        available = {t.name for t in info.function_tools}
        pending = [
            wanted
            for wanted in self._case.expect_tool_calls
            if wanted not in already_called and wanted in available
        ]
        if not pending:
            return ModelResponse(parts=[TextPart(f"（评测回答）{self._case.question}")])
        # parallel_calls 的测题一次性请求全部工具——runner 只有在 tool_calls 长度
        # 大于 1 时才走 asyncio.gather 的合批分支，一轮一个的话那段逻辑（以及它
        # 产生的「一个 assistant 回合带多个 ToolCallPart」）根本不会被触发。
        wanted_now = pending if self._case.parallel_calls else pending[:1]
        return ModelResponse(
            parts=[
                ToolCallPart(tool_name=name, args={}, tool_call_id=f"call_{name}")
                for name in wanted_now
            ]
        )


async def _run(case: EvalCase) -> _ScriptedModel:
    model = _ScriptedModel(case)
    registry = build_default_registry()
    runner = AgentRunner(
        AgentProfile("avatar", "eval", "你是数字人", case.scope_tools),
        ToolExecutor(registry, FakeCoreClient()),
        PydanticAILLM(
            FunctionModel(model, stream_function=model.stream), registry=registry
        ),
        InMemoryAuditStore(),
    )
    await runner.run_turn(
        _session(case.scope_tools),
        case.question,
        f"trace_eval_{case.name}",
        _now(),
        web_search=case.web_search,
    )
    return model


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
async def test_message_shape_is_valid(case: EvalCase) -> None:
    """每道测题的每一次模型调用，消息结构都必须合法。"""
    model = await _run(case)

    assert model.seen, f"{case.name} 一次模型调用都没发生"
    for round_index, messages in enumerate(model.seen):
        violations = check(messages)
        assert not violations, (
            f"{case.name} 第 {round_index + 1} 轮结构违规：\n"
            + "\n".join(f"  {v}" for v in violations)
            + "\n实际结构：\n"
            + "\n".join(f"  {line}" for line in describe(messages))
        )


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.expect_tool_calls], ids=lambda c: c.name
)
async def test_tool_results_reach_the_model_natively(case: EvalCase) -> None:
    """工具跑完后，结果必须以 ToolReturnPart 到达模型，而不是伪装成用户消息。"""
    model = await _run(case)

    last = model.seen[-1]
    kinds = {type(p).__name__ for m in last for p in m.parts}
    assert "ToolReturnPart" in kinds, (
        f"{case.name} 最后一轮里模型没看到任何原生工具结果，实际结构：\n"
        + "\n".join(f"  {line}" for line in describe(last))
    )
    assert "ToolCallPart" in kinds, f"{case.name} 缺少 assistant 的工具调用回合"


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.expect_no_tool_calls], ids=lambda c: c.name
)
async def test_small_talk_stays_single_turn(case: EvalCase) -> None:
    """闲聊和概念题不该触发工具，消息结构应保持最简的一条 ModelRequest。"""
    model = await _run(case)

    assert len(model.seen) == 1, f"{case.name} 不该发生多轮"
    shape = describe(model.seen[0])
    assert shape == ["ModelRequest(SystemPromptPart, UserPromptPart)"], shape


@pytest.mark.parametrize(
    "case", [c for c in CASES if c.parallel_calls], ids=lambda c: c.name
)
async def test_parallel_calls_really_share_one_assistant_turn(case: EvalCase) -> None:
    """并发测题必须真的走到 runner 的合批路径。

    回归：这道测题原先声称覆盖「同一轮多个调用合成一个 assistant 回合」，但脚本
    模型每次只吐一个 ToolCallPart，runner 的 asyncio.gather 分支从未被触发——
    测题名副其实之前，它验收的是另一件事。
    """
    model = await _run(case)

    last = model.seen[-1]
    tool_call_counts = [
        sum(1 for p in m.parts if type(p).__name__ == "ToolCallPart") for m in last
    ]
    assert max(tool_call_counts, default=0) >= 2, (
        f"{case.name} 没有出现带多个 ToolCallPart 的 assistant 回合，实际结构：\n"
        + "\n".join(f"  {line}" for line in describe(last))
    )


async def test_multi_step_case_produces_multiple_tool_rounds() -> None:
    """多步测题必须真的出现两轮 ToolCall/ToolReturn 交替。

    回归：强制联网分支在进入模型循环之前就把 web_search 塞进了历史，如果测题只
    列 web_search 一个工具，模型脚本一看「已经调过」就直接收尾——这道题会退化成
    和 web_search_news 完全一样的单轮。
    """
    case = next(c for c in CASES if c.name == "multi_step_analysis")
    model = await _run(case)

    last = model.seen[-1]
    rounds = sum(1 for m in last if any(type(p).__name__ == "ToolCallPart" for p in m.parts))
    assert rounds >= 2, (
        "多步测题只产生了 " + str(rounds) + " 轮工具调用，实际结构：\n"
        + "\n".join(f"  {line}" for line in describe(last))
    )


def test_case_suite_keeps_its_declared_coverage() -> None:
    """测题集自身的守卫。

    上面两个针对性测试是参数化的：如果有人把 parallel_calls 关掉，那条测试会
    因为参数列表为空而**静默跳过**，不是失败。这里把「测题集必须包含这两类
    覆盖」这件事本身钉死。
    """
    assert any(c.parallel_calls for c in CASES), "缺少并发调用测题，合批路径会失去覆盖"

    multi = next(c for c in CASES if c.name == "multi_step_analysis")
    assert len(multi.expect_tool_calls) >= 2, (
        "多步测题至少要列两个工具——强制联网分支会先塞一个进历史，"
        "只列一个的话模型脚本直接收尾，这道题会退化成单轮"
    )
