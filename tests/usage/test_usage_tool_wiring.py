"""工具调用（外部 API）用量记账接线。

口径：**调用发出即产生成本**。Tavily 这类按请求计费的供应商，超时与报错同样
扣费，所以 ok / timeout / error 都要记。只有 duplicate 不记——它在调 executor
之前就短路了，没有真实外部调用。

记账收在 _execute_single 而不是 _run_tool 的六个 return 上，与既有的
on_tool_end 钩子同一位置、同一理由（见 runner.py 那段 docstring）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.agents.llm import LLMResult, LLMStreamDone, LLMToolCall, LLMUsage
from app.agents.runner import AgentProfile, AgentRunner
from app.audit.store import InMemoryAuditStore
from app.runtime.session import Scope, Session
from app.usage.fx import FxTable
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
        scope=Scope(agents=("avatar",), tools=("get_positions", "web_search")),
        state="text_active",
        created_at=_now(),
        last_activity_at=_now(),
    )


def _book() -> PriceBook:
    return PriceBook(
        [
            PriceEntry("anthropic", "llm", "input_tokens", Decimal(15), "USD", FROM, "v1"),
            PriceEntry("anthropic", "llm", "output_tokens", Decimal(75), "USD", FROM, "v1"),
            PriceEntry("core", "external_api", "call", Decimal(0), "USD", FROM, "v1"),
            PriceEntry("tavily", "external_api", "call", Decimal("0.008"), "USD", FROM, "v1"),
        ]
    )


class _ScriptedLLM:
    def __init__(self, results: list[LLMResult]) -> None:
        self._results = list(results)

    async def complete(self, system: Any, messages: Any, tools: Any) -> LLMResult:
        raise AssertionError("流式路径不该退回 complete()")

    async def stream(self, system: Any, messages: Any, tools: Any):
        yield LLMStreamDone(result=self._results.pop(0))


class _OkExecutor:
    async def execute(
        self, tool_name: str, user_id: str, args: dict, scope_tools: Any, **kwargs: Any
    ) -> dict:
        return {"source": "FAKE", "positions": []}


class _TimeoutExecutor:
    async def execute(
        self, tool_name: str, user_id: str, args: dict, scope_tools: Any, **kwargs: Any
    ) -> dict:
        raise httpx.TimeoutException("upstream timeout")


def _recorder() -> tuple[UsageRecorder, InMemoryUsageStore]:
    store = InMemoryUsageStore()
    return UsageRecorder(store, _book(), FxTable([])), store


def _runner(results: list[LLMResult], recorder: Any, executor: Any) -> AgentRunner:
    profile = AgentProfile("avatar", "fake", "你是数字人", ("get_positions", "web_search"))
    return AgentRunner(
        profile,
        executor,
        _ScriptedLLM(results),  # type: ignore[arg-type]
        InMemoryAuditStore(),
        usage_recorder=recorder,
    )


def _tool_then_answer(calls: tuple[LLMToolCall, ...]) -> list[LLMResult]:
    return [
        LLMResult(
            text="",
            tool_calls=calls,
            usage=LLMUsage(input_tokens=500, output_tokens=50),
            response_model="claude-opus-5",
        ),
        LLMResult(
            text="答案",
            usage=LLMUsage(input_tokens=800, output_tokens=100),
            response_model="claude-opus-5",
        ),
    ]


async def test_successful_tool_call_is_recorded() -> None:
    recorder, store = _recorder()
    runner = _runner(
        _tool_then_answer((LLMToolCall("c1", "get_positions", {}),)), recorder, _OkExecutor()
    )
    await runner.run_turn(_session(), "查持仓", "trc_1", _now())

    api_rows = [r for r in await store.list_for_user("u1", *WINDOW) if r.kind == "external_api"]
    assert len(api_rows) == 1
    assert api_rows[0].quantity.call_count == 1
    assert api_rows[0].provider == "core"
    assert api_rows[0].model == "get_positions"
    assert api_rows[0].turn_id is not None
    assert api_rows[0].dedup_key.startswith("api:trc_1:get_positions:")


async def test_failed_tool_call_is_still_recorded() -> None:
    """超时同样计费——供应商按请求收钱，不看我们有没有拿到结果。"""
    recorder, store = _recorder()
    runner = _runner(
        _tool_then_answer((LLMToolCall("c1", "get_positions", {}),)), recorder, _TimeoutExecutor()
    )
    await runner.run_turn(_session(), "查持仓", "trc_2", _now())

    api_rows = [r for r in await store.list_for_user("u1", *WINDOW) if r.kind == "external_api"]
    assert len(api_rows) == 1
    assert api_rows[0].quantity.call_count == 1


async def test_tool_usage_shares_turn_id_with_llm_usage() -> None:
    """同一轮的 LLM 成本与工具成本必须能加到一起（S4「每次聊天成本」）。"""
    recorder, store = _recorder()
    runner = _runner(
        _tool_then_answer((LLMToolCall("c1", "get_positions", {}),)), recorder, _OkExecutor()
    )
    await runner.run_turn(_session(), "查持仓", "trc_3", _now())

    rows = await store.list_for_user("u1", *WINDOW)
    kinds = {r.kind for r in rows}
    assert kinds == {"llm", "external_api"}
    assert len({r.turn_id for r in rows}) == 1


async def test_two_calls_of_same_tool_get_distinct_dedup_keys() -> None:
    """同名工具在一轮里被调两次，seq 必须把它们区分开。

    否则第二次会撞上同一个 dedup_key 被当成重复丢掉，成本少算一半。
    """
    recorder, store = _recorder()
    runner = _runner(
        _tool_then_answer(
            (
                LLMToolCall("c1", "get_positions", {}),
                LLMToolCall("c2", "web_search", {"query": "BTC"}),
            )
        ),
        recorder,
        _OkExecutor(),
    )
    await runner.run_turn(_session(), "查持仓和行情", "trc_4", _now())

    api_rows = [r for r in await store.list_for_user("u1", *WINDOW) if r.kind == "external_api"]
    assert len(api_rows) == 2
    assert len({r.dedup_key for r in api_rows}) == 2
    assert {r.provider for r in api_rows} == {"core", "tavily"}


async def test_duplicate_tool_call_is_not_charged() -> None:
    """同一轮里参数完全相同的重复调用在 executor 之前就短路了，没有外部调用，
    不该产生成本。"""
    recorder, store = _recorder()
    runner = _runner(
        _tool_then_answer(
            (
                LLMToolCall("c1", "get_positions", {}),
                LLMToolCall("c2", "get_positions", {}),
            )
        ),
        recorder,
        _OkExecutor(),
    )
    await runner.run_turn(_session(), "查持仓", "trc_5", _now())

    api_rows = [r for r in await store.list_for_user("u1", *WINDOW) if r.kind == "external_api"]
    assert len(api_rows) == 1


async def test_runner_without_recorder_still_executes_tools() -> None:
    """回归：不注入 recorder 时工具链路行为不变。"""
    runner = _runner(
        _tool_then_answer((LLMToolCall("c1", "get_positions", {}),)), None, _OkExecutor()
    )
    result = await runner.run_turn(_session(), "查持仓", "trc_6", _now())
    assert result.text == "答案"
    assert [t.name for t in result.executed_tools] == ["get_positions"]
