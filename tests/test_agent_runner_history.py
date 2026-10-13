"""回合把历史带给模型。

真机事故（docs/plans/2026-09-04-text-turn-conversation-history/TECH_DESIGN.md §1）：
问完 Leopold Aschenbrenner 的基金，点追问 chip「他一季度具体做空了哪些股票？」，
答案讲的是 Michael Burry。根因就是这里——每轮只把当前一句喂给模型，"他"没有
指代对象。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.llm import LLMMessage, LLMResult
from app.agents.runner import AgentProfile, AgentRunner, _history_messages
from app.audit.store import InMemoryAuditStore
from app.runtime.session import ConversationTurn, Scope, Session
from app.tools.builtin import build_default_registry
from app.tools.core_client import FakeCoreClient
from app.tools.executor import ToolExecutor


def _now() -> datetime:
    return datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


class _RecordingLLM:
    """记下每次收到的 messages，用来断言历史真的进了 prompt。"""

    def __init__(self) -> None:
        self.seen: list[tuple[LLMMessage, ...]] = []

    async def complete(
        self, system: str, messages: tuple[LLMMessage, ...], tools: tuple[str, ...]
    ) -> LLMResult:
        self.seen.append(messages)
        return LLMResult(text="好的")


def _session(turns: tuple[ConversationTurn, ...] = ()) -> Session:
    return Session(
        session_id="sess_1",
        user_id="u1",
        modality="text",
        scope=Scope(agents=("avatar",), tools=()),
        state="text_active",
        created_at=_now(),
        last_activity_at=_now(),
        turns=turns,
    )


def _runner(llm: object) -> AgentRunner:
    return AgentRunner(
        AgentProfile("avatar", "fake", "你是数字人", ()),
        ToolExecutor(build_default_registry(), FakeCoreClient()),
        llm,
        InMemoryAuditStore(),
    )


def test_history_messages_alternates_user_and_assistant() -> None:
    turns = (
        ConversationTurn("leopold的基金现在怎么样了", "Leopold Aschenbrenner 的基金…", _now()),
    )

    assert [(m.role, m.content) for m in _history_messages(turns)] == [
        ("user", "leopold的基金现在怎么样了"),
        ("assistant", "Leopold Aschenbrenner 的基金…"),
    ]


def test_history_messages_carries_no_tool_calls() -> None:
    """只带纯文本（TECH_DESIGN.md §2）：过期行情留在上下文里会被当成当前数据。"""

    turns = (ConversationTurn("问", "答", _now()),)

    assert all(m.tool_calls == () and m.tool_call_id == "" for m in _history_messages(turns))


def test_empty_history_produces_no_messages() -> None:
    assert _history_messages(()) == []


async def test_turn_prepends_history_before_the_current_question() -> None:
    llm = _RecordingLLM()
    turns = (
        ConversationTurn("leopold的基金现在怎么样了", "Leopold Aschenbrenner 的基金…", _now()),
    )

    await _runner(llm).run_turn(_session(turns), "他一季度具体做空了哪些股票？", "trace1", _now())

    assert [(m.role, m.content) for m in llm.seen[0]] == [
        ("user", "leopold的基金现在怎么样了"),
        ("assistant", "Leopold Aschenbrenner 的基金…"),
        ("user", "他一季度具体做空了哪些股票？"),
    ]


async def test_turn_without_history_sends_only_the_current_question() -> None:
    """向后兼容：没有历史时的行为与改造前完全一致。"""

    llm = _RecordingLLM()

    await _runner(llm).run_turn(_session(), "BTC 现在多少", "trace1", _now())

    assert [(m.role, m.content) for m in llm.seen[0]] == [("user", "BTC 现在多少")]


class _FixedLLM:
    """吐一句固定答案，用来验证写入的内容与时机。"""

    def __init__(self, text: str = "答案正文") -> None:
        self._text = text

    async def complete(
        self, system: str, messages: tuple[LLMMessage, ...], tools: tuple[str, ...]
    ) -> LLMResult:
        return LLMResult(text=self._text)


def _store_runner(llm: object, store: object) -> AgentRunner:
    return AgentRunner(
        AgentProfile("avatar", "fake", "你是数字人", ()),
        ToolExecutor(build_default_registry(), FakeCoreClient()),
        llm,
        InMemoryAuditStore(),
        session_store=store,
    )


async def test_turn_persists_itself_into_history() -> None:
    from app.runtime.session_store import InMemorySessionStore

    store = InMemorySessionStore()
    session = _session()
    await store.create(session)

    await _store_runner(_FixedLLM(), store).run_turn(session, "BTC 现在多少", "trace1", _now())

    saved = await store.get("sess_1")
    assert saved is not None
    assert [(t.user_text, t.assistant_text) for t in saved.turns] == [("BTC 现在多少", "答案正文")]


async def test_second_turn_sees_the_first_one() -> None:
    """端到端：这就是事故链路——第二轮必须看得到第一轮。"""

    from app.runtime.session_store import InMemorySessionStore

    store = InMemorySessionStore()
    session = _session()
    await store.create(session)
    llm = _RecordingLLM()
    runner = _store_runner(llm, store)

    await runner.run_turn(session, "leopold的基金现在怎么样了", "trace1", _now())
    reloaded = await store.get("sess_1")
    assert reloaded is not None
    await runner.run_turn(reloaded, "他一季度具体做空了哪些股票？", "trace2", _now())

    assert [(m.role, m.content) for m in llm.seen[1]] == [
        ("user", "leopold的基金现在怎么样了"),
        ("assistant", "好的"),
        ("user", "他一季度具体做空了哪些股票？"),
    ]


async def test_persisted_answer_has_the_followup_section_stripped() -> None:
    """追问区不能进历史（TECH_DESIGN.md §3.1）。

    留着的话下一轮模型会把上一轮的三条追问当正文读，甚至照着模仿。
    """
    from app.runtime.session_store import InMemorySessionStore
    from app.runtime.suggestion_split import MARKER

    store = InMemorySessionStore()
    session = _session()
    await store.create(session)
    answer = f"正文结论。\n\n{MARKER}\n1. 追问一\n2. 追问二\n3. 追问三"

    await _store_runner(_FixedLLM(answer), store).run_turn(session, "问一句", "trace1", _now())

    saved = await store.get("sess_1")
    assert saved is not None
    assert saved.turns[0].assistant_text == "正文结论。"


async def test_empty_answer_is_not_persisted() -> None:
    """空回合进历史会让模型以为自己上一轮沉默了。"""

    from app.runtime.session_store import InMemorySessionStore

    store = InMemorySessionStore()
    session = _session()
    await store.create(session)

    await _store_runner(_FixedLLM(""), store).run_turn(session, "问一句", "trace1", _now())

    saved = await store.get("sess_1")
    assert saved is not None
    assert saved.turns == ()


async def test_runner_without_a_store_behaves_exactly_as_before() -> None:
    """向后兼容：仓库里大量测试直接构造 runner，不传 store。"""

    result = await _runner(_RecordingLLM()).run_turn(_session(), "问一句", "trace1", _now())

    assert result.text == "好的"


async def test_persist_failure_does_not_break_the_turn() -> None:
    """被 LLM 调用的路径上永不抛异常——历史写失败就丢这一轮历史，不能崩掉回合。"""

    class _BrokenStore:
        async def get(self, session_id: str) -> Session | None:
            raise RuntimeError("redis down")

        async def update(self, session: Session) -> None:  # pragma: no cover - 到不了
            raise AssertionError("get 就炸了，不该走到这里")

    result = await _store_runner(_FixedLLM(), _BrokenStore()).run_turn(
        _session(), "问一句", "trace1", _now()
    )

    assert result.text == "答案正文"


async def test_a_vanished_session_is_not_recreated() -> None:
    """会话已过期就跳过写入，不要凭手里的旧对象把它写回去。"""

    from app.runtime.session_store import InMemorySessionStore

    store = InMemorySessionStore()  # 故意不 create

    await _store_runner(_FixedLLM(), store).run_turn(_session(), "问一句", "trace1", _now())

    assert await store.get("sess_1") is None


async def test_a_half_finished_answer_still_lands_in_history() -> None:
    """回合超时但吐了半句：用户看得到那半句，下一轮必须知道说过什么。"""

    import asyncio
    from collections.abc import AsyncIterator

    from app.agents.llm import LLMStreamEvent, LLMTextDelta
    from app.runtime.session_store import InMemorySessionStore

    class _HangingLLM:
        async def complete(
            self, system: str, messages: tuple[LLMMessage, ...], tools: tuple[str, ...]
        ) -> LLMResult:  # pragma: no cover - 走的是 stream
            await asyncio.sleep(10)
            raise AssertionError("不该跑到这里")

        async def stream(
            self, system: str, messages: tuple[LLMMessage, ...], tools: tuple[str, ...]
        ) -> AsyncIterator[LLMStreamEvent]:
            yield LLMTextDelta(delta="已经说了半句")
            await asyncio.sleep(10)

    store = InMemorySessionStore()
    session = _session()
    await store.create(session)
    runner = AgentRunner(
        AgentProfile("avatar", "fake", "你是数字人", ()),
        ToolExecutor(build_default_registry(), FakeCoreClient()),
        _HangingLLM(),
        InMemoryAuditStore(),
        turn_timeout_seconds=0.05,
        session_store=store,
    )

    result = await runner.run_turn(session, "问一句", "trace1", _now())

    assert "TURN_TIMEOUT" in result.blockers
    saved = await store.get("sess_1")
    assert saved is not None
    assert saved.turns[0].assistant_text == "已经说了半句"


async def test_forced_web_search_anchors_a_dangling_query() -> None:
    """带指代的检索词要拼上一轮主题，否则证据包讲的是别人的事。

    事故的下半：Tavily 拿到「他一季度具体做空了哪些股票」，返回的全是
    Michael Burry 的 13F，而提示词要求"必须基于本轮搜索结果回答"。
    """

    captured: list[dict] = []

    class _SearchExecutor:
        async def execute(
            self, tool_name: str, user_id: str, args: dict, scope_tools: tuple[str, ...], **kwargs
        ) -> dict:
            captured.append(args)
            return {"source": "TAVILY", "results": [{"title": "t", "content": "c"}]}

    session = Session(
        session_id="sess_1",
        user_id="u1",
        modality="text",
        scope=Scope(agents=("avatar",), tools=("web_search",)),
        state="text_active",
        created_at=_now(),
        last_activity_at=_now(),
        turns=(ConversationTurn("leopold的基金现在怎么样了", "……", _now()),),
    )
    runner = AgentRunner(
        AgentProfile("avatar", "fake", "你是数字人", ("web_search",)),
        _SearchExecutor(),
        _RecordingLLM(),
        InMemoryAuditStore(),
    )

    await runner.run_turn(
        session, "他一季度具体做空了哪些股票？", "trace1", _now(), web_search=True
    )

    assert captured
    assert captured[0]["query"] == "leopold的基金现在怎么样了 他一季度具体做空了哪些股票？"


async def test_forced_web_search_leaves_a_self_contained_query_alone() -> None:
    captured: list[dict] = []

    class _SearchExecutor:
        async def execute(
            self, tool_name: str, user_id: str, args: dict, scope_tools: tuple[str, ...], **kwargs
        ) -> dict:
            captured.append(args)
            return {"source": "TAVILY", "results": [{"title": "t", "content": "c"}]}

    session = Session(
        session_id="sess_1",
        user_id="u1",
        modality="text",
        scope=Scope(agents=("avatar",), tools=("web_search",)),
        state="text_active",
        created_at=_now(),
        last_activity_at=_now(),
        turns=(ConversationTurn("leopold的基金现在怎么样了", "……", _now()),),
    )
    runner = AgentRunner(
        AgentProfile("avatar", "fake", "你是数字人", ("web_search",)),
        _SearchExecutor(),
        _RecordingLLM(),
        InMemoryAuditStore(),
    )

    await runner.run_turn(session, "英伟达最新财报怎么样", "trace1", _now(), web_search=True)

    assert captured
    assert captured[0]["query"] == "英伟达最新财报怎么样"
