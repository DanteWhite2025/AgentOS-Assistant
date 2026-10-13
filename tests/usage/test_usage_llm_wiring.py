"""LLM 用量记账接线。

三件事必须锁住：
1. 一轮内多步 LLM 调用都记账，且同属一个 turn_id（S4「每次聊天成本」靠它归并）；
2. usage 为 None 时落 usage_missing，不静默漏掉；
3. recorder 为 None 时 runner 行为与接入前完全一致。

Session / AgentRunner / 脚本化 LLM 一律照 test_agent_runner.py 与
test_runner_streaming.py 的既有搭建方式。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.agents.llm import LLMResult, LLMStreamDone, LLMToolCall, LLMUsage
from app.agents.runner import AgentProfile, AgentRunner
from app.audit.store import InMemoryAuditStore
from app.runtime.session import Scope, Session
from app.usage.fx import FxTable
from app.usage.models import UsageRecord
from app.usage.pricing import PriceBook, PriceEntry
from app.usage.recorder import UsageRecorder
from app.usage.store import InMemoryUsageStore

FROM = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW = (datetime(2026, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))


def _now() -> datetime:
    return datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def _session() -> Session:
    return Session(
        session_id="sess_1",
        user_id="u1",
        modality="text",
        scope=Scope(agents=("avatar",), tools=("get_positions",)),
        state="text_active",
        created_at=_now(),
        last_activity_at=_now(),
    )


def _book() -> PriceBook:
    return PriceBook(
        [
            PriceEntry("anthropic", "llm", "input_tokens", Decimal(15), "USD", FROM, "v1"),
            PriceEntry("anthropic", "llm", "output_tokens", Decimal(75), "USD", FROM, "v1"),
        ]
    )


class _ScriptedLLM:
    """按序吐出预设的 LLMResult，每次 stream 消费一个。"""

    def __init__(self, results: list[LLMResult]) -> None:
        self._results = list(results)
        self.calls = 0

    async def complete(self, system: Any, messages: Any, tools: Any) -> LLMResult:
        raise AssertionError("流式路径不该退回 complete()")

    async def stream(self, system: Any, messages: Any, tools: Any):
        self.calls += 1
        yield LLMStreamDone(result=self._results.pop(0))


class _OkExecutor:
    async def execute(
        self, tool_name: str, user_id: str, args: dict, scope_tools: Any, **kwargs: Any
    ) -> dict:
        return {"source": "FAKE", "positions": []}


def _runner(results: list[LLMResult], recorder: Any = None) -> AgentRunner:
    profile = AgentProfile("avatar", "fake", "你是数字人", ("get_positions",))
    return AgentRunner(
        profile,
        _OkExecutor(),  # type: ignore[arg-type]
        _ScriptedLLM(results),  # type: ignore[arg-type]
        InMemoryAuditStore(),
        usage_recorder=recorder,
    )


def _recorder() -> tuple[UsageRecorder, InMemoryUsageStore]:
    store = InMemoryUsageStore()
    return UsageRecorder(store, _book(), FxTable([])), store


def test_llm_result_carries_response_model_with_safe_default() -> None:
    """新字段必须有默认值，否则现存所有 LLMResult 构造点都会炸。"""
    assert LLMResult(text="hi").response_model is None
    assert LLMResult(text="hi", response_model="claude-opus-5").response_model == "claude-opus-5"


async def test_single_turn_llm_usage_is_recorded() -> None:
    recorder, store = _recorder()
    runner = _runner(
        [
            LLMResult(
                text="答案",
                usage=LLMUsage(input_tokens=1000, output_tokens=200),
                response_model="claude-opus-5",
            )
        ],
        recorder,
    )
    await runner.run_turn(_session(), "问题", "trc_1", _now())

    rows = await store.list_for_user("u1", *WINDOW)
    assert len(rows) == 1
    assert rows[0].kind == "llm"
    assert rows[0].scene == "chat"
    assert rows[0].model == "claude-opus-5"
    assert rows[0].amount_usd == Decimal("0.03000000")
    assert rows[0].turn_id is not None
    assert rows[0].session_id == "sess_1"


async def test_multi_step_turn_shares_one_turn_id() -> None:
    """一轮里模型先调工具、再作答 = 两次 LLM 调用，必须归并到同一个 turn_id，
    否则「一次聊天多少钱」算出来的是「一次 LLM 调用多少钱」。"""
    recorder, store = _recorder()
    runner = _runner(
        [
            LLMResult(
                text="",
                tool_calls=(LLMToolCall("c1", "get_positions", {}),),
                usage=LLMUsage(input_tokens=500, output_tokens=50),
                response_model="claude-opus-5",
            ),
            LLMResult(
                text="答案",
                usage=LLMUsage(input_tokens=800, output_tokens=100),
                response_model="claude-opus-5",
            ),
        ],
        recorder,
    )
    await runner.run_turn(_session(), "查一下持仓", "trc_2", _now())

    llm_rows = [r for r in await store.list_for_user("u1", *WINDOW) if r.kind == "llm"]
    assert len(llm_rows) == 2
    turn_ids = {r.turn_id for r in llm_rows}
    assert len(turn_ids) == 1
    turn_id = turn_ids.pop()
    assert {r.dedup_key for r in llm_rows} == {f"llm:{turn_id}:0", f"llm:{turn_id}:1"}


async def test_turn_ids_differ_across_turns() -> None:
    recorder, store = _recorder()
    runner = _runner(
        [
            LLMResult(text="a", usage=LLMUsage(input_tokens=10, output_tokens=1)),
            LLMResult(text="b", usage=LLMUsage(input_tokens=10, output_tokens=1)),
        ],
        recorder,
    )
    await runner.run_turn(_session(), "一", "trc_a", _now())
    await runner.run_turn(_session(), "二", "trc_b", _now())

    rows = await store.list_for_user("u1", *WINDOW)
    assert len(rows) == 2
    assert len({r.turn_id for r in rows}) == 2


async def test_missing_usage_is_recorded_as_usage_missing() -> None:
    """FakeLLM 的 usage 恒为 None（app/agents/llm.py 的红线）。这类调用照样要留痕，
    否则 LLM 成本会被系统性低估。"""
    recorder, store = _recorder()
    runner = _runner([LLMResult(text="答案", usage=None)], recorder)
    await runner.run_turn(_session(), "问题", "trc_3", _now())

    rows = await store.list_for_user("u1", *WINDOW)
    assert len(rows) == 1
    assert rows[0].status == "usage_missing"
    assert rows[0].amount_usd is None


async def test_runner_without_recorder_behaves_identically() -> None:
    """回归：不注入 recorder 时不得有任何行为变化。"""
    runner = _runner([LLMResult(text="答案")])
    result = await runner.run_turn(_session(), "问题", "trc_4", _now())
    assert result.text == "答案"


async def test_turn_survives_recorder_failure() -> None:
    """记账挂了不能把对话带崩（spec §6.3）。"""

    class BoomStore:
        async def append(self, record: UsageRecord) -> bool:
            raise RuntimeError("db down")

    recorder = UsageRecorder(BoomStore(), _book(), FxTable([]))  # type: ignore[arg-type]
    runner = _runner([LLMResult(text="答案")], recorder)
    result = await runner.run_turn(_session(), "问题", "trc_5", _now())
    assert result.text == "答案"
    assert recorder.failure_count == 1
