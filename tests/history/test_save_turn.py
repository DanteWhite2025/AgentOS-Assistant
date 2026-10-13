"""_save_turn 的双写语义：Redis 上下文窗口 + PG 耐久记录。

两条写入互不拖累是重点 —— 任何一边挂了都不该阻断回合，也不该让另一边
跟着丢数据。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.agents.llm import FakeLLM
from app.agents.runner import AgentProfile, AgentRunner
from app.audit.store import InMemoryAuditStore
from app.history.store import InMemoryConversationStore
from app.runtime.session import Scope, Session
from app.runtime.suggestion_split import MARKER
from app.tools.builtin import build_default_registry
from app.tools.core_client import FakeCoreClient
from app.tools.executor import ToolExecutor

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def _session(session_id: str = "sess_1", user_id: str = "u1") -> Session:
    return Session(
        session_id=session_id,
        user_id=user_id,
        modality="text",
        scope=Scope(agents=("avatar",), tools=()),
        state="text_active",
        created_at=NOW,
        last_activity_at=NOW,
    )


def _runner(session_store=None, conversation_store=None) -> AgentRunner:
    return AgentRunner(
        AgentProfile("avatar", "fake", "你是数字人", ()),
        ToolExecutor(build_default_registry(), FakeCoreClient()),
        FakeLLM(),
        InMemoryAuditStore(),
        session_store=session_store,
        conversation_store=conversation_store,
    )


class _Store:
    """最小 SessionStore 替身。"""

    def __init__(self, session: Session | None) -> None:
        self.session = session
        self.updated: Session | None = None

    async def get(self, session_id: str) -> Session | None:
        return self.session

    async def update(self, session: Session) -> None:
        self.updated = session


class _ExplodingStore(_Store):
    async def update(self, session: Session) -> None:
        raise RuntimeError("redis 挂了")


async def test_turn_is_archived_to_conversation_store() -> None:
    convo = InMemoryConversationStore()
    runner = _runner(_Store(_session()), convo)

    await runner._save_turn(_session(), "BTC 多少钱", "六万二", NOW)

    summary = await convo.get_conversation("sess_1")
    assert summary is not None
    assert summary.user_id == "u1"
    assert summary.message_count == 2
    items, _ = await convo.list_messages("sess_1", limit=10, cursor=None)
    assert [(m.role, m.content) for m in items] == [
        ("user", "BTC 多少钱"),
        ("assistant", "六万二"),
    ]


async def test_archive_happens_even_when_runtime_session_expired() -> None:
    """会话过期只影响上下文窗口，不该让这一轮从耐久记录里消失。

    回合确实发生过。Redis 侧刻意不复活过期会话（TTL 语义），但 PG 是耐久
    记录，凭什么因为运行时对象恰好过期就丢。
    """
    convo = InMemoryConversationStore()
    store = _Store(None)  # get() 返回 None = 会话已过期
    runner = _runner(store, convo)

    await runner._save_turn(_session(), "问", "答", NOW)

    assert await convo.get_conversation("sess_1") is not None
    assert store.updated is None, "过期会话不该被复活"


async def test_archive_survives_session_store_failure() -> None:
    convo = InMemoryConversationStore()
    runner = _runner(_ExplodingStore(_session()), convo)

    await runner._save_turn(_session(), "问", "答", NOW)

    assert await convo.get_conversation("sess_1") is not None


async def test_session_window_survives_conversation_store_failure() -> None:
    class _ExplodingConversations(InMemoryConversationStore):
        async def record_turn(self, *args, **kwargs) -> None:
            raise RuntimeError("pg 挂了")

    store = _Store(_session())
    runner = _runner(store, _ExplodingConversations())

    await runner._save_turn(_session(), "问", "答", NOW)

    assert store.updated is not None
    assert store.updated.turns[-1].user_text == "问"


async def test_suggestion_chips_are_stripped_before_archiving() -> None:
    """追问区是给客户端渲染 chips 的，不是答案正文，不该进历史。"""
    convo = InMemoryConversationStore()
    runner = _runner(_Store(_session()), convo)
    answer = f"六万二。\n{MARKER}\n再看看 ETH\n看看我的持仓\n"

    await runner._save_turn(_session(), "BTC 多少钱", answer, NOW)

    items, _ = await convo.list_messages("sess_1", limit=10, cursor=None)
    assistant = next(m for m in items if m.role == "assistant")
    assert "再看看 ETH" not in assistant.content
    assert "六万二" in assistant.content


async def test_empty_answer_is_not_archived() -> None:
    """空回合（超时且一个字都没吐）进历史，会让模型以为自己上一轮沉默了。"""
    convo = InMemoryConversationStore()
    runner = _runner(_Store(_session()), convo)

    await runner._save_turn(_session(), "问", "   ", NOW)

    assert await convo.get_conversation("sess_1") is None


async def test_runner_without_stores_is_noop() -> None:
    """仓库里大量直接构造 runner 的测试不传 store，不能因此炸。"""
    runner = _runner(None, None)
    await runner._save_turn(_session(), "问", "答", NOW)


# ---------------------------------------------------------------- 标题生成触发


class _RecordingTitler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def generate(self, session_id: str, user_text: str, assistant_text: str) -> None:
        self.calls.append((session_id, user_text, assistant_text))


def _runner_with_titler(convo, titler) -> AgentRunner:
    return AgentRunner(
        AgentProfile("avatar", "fake", "你是数字人", ()),
        ToolExecutor(build_default_registry(), FakeCoreClient()),
        FakeLLM(),
        InMemoryAuditStore(),
        session_store=_Store(_session()),
        conversation_store=convo,
        conversation_titler=titler,
    )


async def test_title_generation_fires_on_first_turn_only() -> None:
    """只在建行那一轮触发。每轮都生成等于把标题成本乘以轮数，还会让标题
    随最后一轮漂移——用户眼里就是标题自己变来变去。"""
    convo = InMemoryConversationStore()
    titler = _RecordingTitler()
    runner = _runner_with_titler(convo, titler)

    await runner._save_turn(_session(), "第一句", "答一", NOW)
    await runner._save_turn(_session(), "第二句", "答二", NOW)
    # 标题是后台任务，_save_turn 返回时它还没跑——这正是要的形状。
    await asyncio.sleep(0)

    assert titler.calls == [("sess_1", "第一句", "答一")]


async def test_title_generation_is_not_awaited_inline() -> None:
    """网关单次 1.5~3 分钟，同步等它等于把回合拖到分钟级。"""
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowTitler:
        async def generate(self, session_id, user_text, assistant_text) -> None:
            started.set()
            await release.wait()

    convo = InMemoryConversationStore()
    runner = _runner_with_titler(convo, _SlowTitler())

    await asyncio.wait_for(runner._save_turn(_session(), "问", "答", NOW), timeout=1)

    # 回合已经返回，标题任务还卡在里面 —— 正是我们要的形状
    await asyncio.wait_for(started.wait(), timeout=1)
    release.set()
    await asyncio.sleep(0)


async def test_turn_survives_titler_explosion() -> None:
    class _BoomTitler:
        async def generate(self, session_id, user_text, assistant_text) -> None:
            raise RuntimeError("titler 炸了")

    convo = InMemoryConversationStore()
    runner = _runner_with_titler(convo, _BoomTitler())

    await runner._save_turn(_session(), "问", "答", NOW)
    await asyncio.sleep(0)

    assert await convo.get_conversation("sess_1") is not None
