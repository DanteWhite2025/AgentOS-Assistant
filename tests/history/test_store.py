"""ConversationStore 的游标、懒创建与分页语义。

同一套用例跑两个实现（内存 / sqlite+aiosqlite），照
tests/news/test_news_store.py 的参数化范式 —— 两边行为若有分歧，
fake 模式下测出来的东西到生产就不作数了。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.history.store import (
    TITLE_MAX_CHARS,
    InMemoryConversationStore,
    decode_cursor,
    encode_cursor,
)

BASE = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


async def _pg_store():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.history.pg_store import PostgresConversationStore

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    store = PostgresConversationStore(engine)
    await store.create_schema()
    return store


@pytest.fixture(params=["memory", "pg"])
async def store(request):
    if request.param == "memory":
        return InMemoryConversationStore()
    return await _pg_store()


# ---------------------------------------------------------------- 游标


def test_cursor_roundtrip() -> None:
    token = encode_cursor(BASE, "sess_abc")
    assert decode_cursor(token) == (BASE, "sess_abc")


def test_cursor_is_opaque() -> None:
    """不能让调用方看出这是时间戳，免得有人拿它当 offset 加减。"""
    token = encode_cursor(BASE, "sess_abc")
    assert "2026" not in token
    assert "sess_abc" not in token


def test_cursor_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        decode_cursor("!!!not-base64!!!")


def test_cursor_rejects_missing_identifier() -> None:
    """只有时间没有 id 的游标会让 keyset 分页退化成不稳定排序。"""
    import base64

    raw = base64.urlsafe_b64encode(BASE.isoformat().encode()).decode().rstrip("=")
    with pytest.raises(ValueError):
        decode_cursor(raw)


# ---------------------------------------------------------------- 懒创建与标题


async def test_first_turn_creates_conversation(store) -> None:
    await store.record_turn("sess_1", "u1", "BTC 现在多少钱", "六万二", at(0))

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.user_id == "u1"
    assert convo.title == "BTC 现在多少钱"
    assert convo.modality == "text"
    assert convo.message_count == 2
    assert convo.created_at == at(0)
    assert convo.last_message_at == at(0)


async def test_title_falls_back_to_truncated_first_message(store) -> None:
    long_text = "帮我详细分析一下比特币最近三个月的走势以及后续可能的支撑位和压力位"
    await store.record_turn("sess_1", "u1", long_text, "好的", at(0))

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.title == long_text[:TITLE_MAX_CHARS]
    assert len(convo.title) == TITLE_MAX_CHARS


async def test_later_turns_do_not_overwrite_title(store) -> None:
    """续写若跟着改标题，第二轮的首句会把 LLM 生成的标题冲掉。"""
    await store.record_turn("sess_1", "u1", "第一句", "答", at(0))
    await store.set_title("sess_1", "BTC行情分析")
    await store.record_turn("sess_1", "u1", "第二句完全不同的话题", "答", at(5))

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.title == "BTC行情分析"


async def test_later_turns_accumulate_count_and_activity(store) -> None:
    await store.record_turn("sess_1", "u1", "一", "答", at(0))
    await store.record_turn("sess_1", "u1", "二", "答", at(5))

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.message_count == 4
    assert convo.last_message_at == at(5)
    assert convo.created_at == at(0)


async def test_get_conversation_missing_returns_none(store) -> None:
    assert await store.get_conversation("sess_nope") is None


async def test_set_title_on_missing_conversation_is_noop(store) -> None:
    """标题生成是异步的，回写时会话可能已被清理——不该炸。"""
    await store.set_title("sess_nope", "标题")
    assert await store.get_conversation("sess_nope") is None


# ---------------------------------------------------------------- 会话列表


async def test_list_conversations_is_newest_first(store) -> None:
    await store.record_turn("sess_old", "u1", "旧", "答", at(0))
    await store.record_turn("sess_new", "u1", "新", "答", at(10))

    items, cursor = await store.list_conversations("u1", limit=10, cursor=None)
    assert [c.session_id for c in items] == ["sess_new", "sess_old"]
    assert cursor is None


async def test_list_conversations_isolates_users(store) -> None:
    await store.record_turn("sess_1", "u1", "我的", "答", at(0))
    await store.record_turn("sess_2", "u2", "别人的", "答", at(1))

    items, _ = await store.list_conversations("u1", limit=10, cursor=None)
    assert [c.session_id for c in items] == ["sess_1"]


async def test_list_conversations_paginates(store) -> None:
    for i in range(5):
        await store.record_turn(f"sess_{i}", "u1", f"问{i}", "答", at(i))

    first, cursor = await store.list_conversations("u1", limit=2, cursor=None)
    assert [c.session_id for c in first] == ["sess_4", "sess_3"]
    assert cursor is not None

    second, cursor2 = await store.list_conversations("u1", limit=2, cursor=cursor)
    assert [c.session_id for c in second] == ["sess_2", "sess_1"]

    third, cursor3 = await store.list_conversations("u1", limit=2, cursor=cursor2)
    assert [c.session_id for c in third] == ["sess_0"]
    assert cursor3 is None


async def test_list_conversations_stable_when_timestamps_tie(store) -> None:
    """同一毫秒的两条会话必须有确定顺序，否则分页会漏条或重复。"""
    await store.record_turn("sess_a", "u1", "a", "答", at(0))
    await store.record_turn("sess_b", "u1", "b", "答", at(0))

    first, cursor = await store.list_conversations("u1", limit=1, cursor=None)
    second, _ = await store.list_conversations("u1", limit=1, cursor=cursor)

    seen = [first[0].session_id, second[0].session_id]
    assert sorted(seen) == ["sess_a", "sess_b"], "分页跨页出现重复或遗漏"


# ---------------------------------------------------------------- 消息回放


async def test_list_messages_is_chronological(store) -> None:
    await store.record_turn("sess_1", "u1", "问一", "答一", at(0))
    await store.record_turn("sess_1", "u1", "问二", "答二", at(5))

    items, cursor = await store.list_messages("sess_1", limit=10, cursor=None)
    assert [(m.role, m.content) for m in items] == [
        ("user", "问一"),
        ("assistant", "答一"),
        ("user", "问二"),
        ("assistant", "答二"),
    ]
    assert cursor is None


async def test_list_messages_paginates_forward(store) -> None:
    for i in range(3):
        await store.record_turn("sess_1", "u1", f"问{i}", f"答{i}", at(i))

    first, cursor = await store.list_messages("sess_1", limit=2, cursor=None)
    assert [m.content for m in first] == ["问0", "答0"]
    assert cursor is not None

    second, _ = await store.list_messages("sess_1", limit=2, cursor=cursor)
    assert [m.content for m in second] == ["问1", "答1"]


async def test_list_messages_of_unknown_conversation_is_empty(store) -> None:
    items, cursor = await store.list_messages("sess_nope", limit=10, cursor=None)
    assert items == []
    assert cursor is None


# ---------------------------------------------------------------- resume 取上下文


async def test_recent_turns_returns_latest_pairs_in_order(store) -> None:
    for i in range(5):
        await store.record_turn("sess_1", "u1", f"问{i}", f"答{i}", at(i))

    turns = await store.recent_turns("sess_1", max_turns=3)
    assert turns == [("问2", "答2"), ("问3", "答3"), ("问4", "答4")]


async def test_recent_turns_when_fewer_than_requested(store) -> None:
    await store.record_turn("sess_1", "u1", "唯一一轮", "答", at(0))

    assert await store.recent_turns("sess_1", max_turns=3) == [("唯一一轮", "答")]


# ---------------------------------------------------------------- 保留期清理


async def test_purge_before_removes_old_conversations(store) -> None:
    await store.record_turn("sess_old", "u1", "旧", "答", at(0))
    await store.record_turn("sess_new", "u1", "新", "答", at(100))

    removed = await store.purge_before(at(50))
    assert removed == 1
    assert await store.get_conversation("sess_old") is None
    assert await store.get_conversation("sess_new") is not None


async def test_purge_before_also_removes_orphan_messages(store) -> None:
    """只删会话行不删消息，等于攒了一堆永远查不到的孤儿数据。"""
    await store.record_turn("sess_old", "u1", "旧", "答", at(0))

    await store.purge_before(at(50))
    items, _ = await store.list_messages("sess_old", limit=10, cursor=None)
    assert items == []


async def test_purge_before_keeps_conversation_still_active(store) -> None:
    """判据是最后活跃时间，不是创建时间——一直在聊的老会话不能被清掉。"""
    await store.record_turn("sess_1", "u1", "很久以前开的", "答", at(0))
    await store.record_turn("sess_1", "u1", "但刚刚还在聊", "答", at(100))

    assert await store.purge_before(at(50)) == 0
    assert await store.get_conversation("sess_1") is not None


# ---------------------------------------------------------------- 删除


async def test_delete_removes_conversation_and_messages(store) -> None:
    await store.record_turn("sess_1", "u1", "问", "答", at(0))

    assert await store.delete_conversation("sess_1") is True
    assert await store.get_conversation("sess_1") is None
    items, _ = await store.list_messages("sess_1", limit=10, cursor=None)
    assert items == [], "只删会话行不删消息，等于攒一堆永远查不到的孤儿数据"


async def test_delete_is_scoped_to_one_conversation(store) -> None:
    await store.record_turn("sess_1", "u1", "问", "答", at(0))
    await store.record_turn("sess_2", "u1", "问", "答", at(1))

    await store.delete_conversation("sess_1")

    assert await store.get_conversation("sess_2") is not None
    items, _ = await store.list_messages("sess_2", limit=10, cursor=None)
    assert len(items) == 2


async def test_delete_missing_conversation_reports_false(store) -> None:
    """路由层靠这个返回值分不出「刚删掉」和「本来没有」——归属校验在它之前
    就已经拦掉不存在的了，这里只是让重复删除不炸。"""
    assert await store.delete_conversation("sess_nope") is False


async def test_delete_twice_is_safe(store) -> None:
    await store.record_turn("sess_1", "u1", "问", "答", at(0))

    assert await store.delete_conversation("sess_1") is True
    assert await store.delete_conversation("sess_1") is False


# ---------------------------------------------------------------- 标题条件更新


async def test_set_title_unconditional_overwrites(store) -> None:
    """用户重命名走这条：无条件生效。"""
    await store.record_turn("sess_1", "u1", "原始首句", "答", at(0))

    await store.set_title("sess_1", "我的持仓分析")

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.title == "我的持仓分析"


async def test_set_title_with_guard_applies_when_unchanged(store) -> None:
    """titler 走这条：标题还是降级值时才允许升级。"""
    await store.record_turn("sess_1", "u1", "原始首句", "答", at(0))

    await store.set_title("sess_1", "BTC行情分析", only_if="原始首句")

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.title == "BTC行情分析"


async def test_set_title_with_guard_skips_when_user_renamed(store) -> None:
    """核心竞态：titler 是异步的（网关 1.5~3 分钟），用户可能在它返回前就
    改了名。守卫不匹配 → 影响 0 行，用户意图胜出。"""
    await store.record_turn("sess_1", "u1", "原始首句", "答", at(0))
    await store.set_title("sess_1", "用户改的名字")

    await store.set_title("sess_1", "模型生成的标题", only_if="原始首句")

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.title == "用户改的名字", "用户改名被异步 titler 覆盖了"


async def test_set_title_with_guard_on_missing_conversation_is_noop(store) -> None:
    await store.set_title("sess_nope", "标题", only_if="任何值")
    assert await store.get_conversation("sess_nope") is None


# ---------------------------------------------------------------- 语音与混合 modality


async def test_voice_turn_records_voice_modality(store) -> None:
    await store.record_turn("sess_1", "u1", "问", "答", at(0), modality="voice")

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.modality == "voice"


async def test_text_then_voice_upgrades_to_both(store) -> None:
    """同一会话里既打字又说话，列表要能看出这是混合会话。"""
    await store.record_turn("sess_1", "u1", "打字问", "答", at(0))
    await store.record_turn("sess_1", "u1", "语音问", "答", at(1), modality="voice")

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.modality == "both"


async def test_voice_then_text_upgrades_to_both(store) -> None:
    await store.record_turn("sess_1", "u1", "语音问", "答", at(0), modality="voice")
    await store.record_turn("sess_1", "u1", "打字问", "答", at(1))

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.modality == "both"


async def test_same_modality_twice_stays_unchanged(store) -> None:
    await store.record_turn("sess_1", "u1", "问一", "答", at(0), modality="voice")
    await store.record_turn("sess_1", "u1", "问二", "答", at(1), modality="voice")

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.modality == "voice"


async def test_both_never_downgrades(store) -> None:
    """升到 both 之后再来任何单一 modality 都不该把它降回去。"""
    await store.record_turn("sess_1", "u1", "打字", "答", at(0))
    await store.record_turn("sess_1", "u1", "语音", "答", at(1), modality="voice")
    await store.record_turn("sess_1", "u1", "又打字", "答", at(2))

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.modality == "both"


# ---------------------------------------------------------------- 空侧轮次


async def test_turn_with_empty_assistant_is_archived_faithfully(store) -> None:
    """用户打断得够早时 AI 一个字没说，那一轮仍要记——用户确实问了。"""
    await store.record_turn("sess_1", "u1", "等一下", "", at(0), modality="voice")

    items, _ = await store.list_messages("sess_1", limit=10, cursor=None)
    assert [(m.role, m.content) for m in items] == [("user", "等一下"), ("assistant", "")]


async def test_empty_assistant_turns_are_not_fed_back_to_the_model(store) -> None:
    """回放要faithful，但灌回上下文不能告诉模型「你上一轮沉默了」——
    一期 _save_turn 就是为这个跳过空回合的。"""
    await store.record_turn("sess_1", "u1", "被打断的问题", "", at(0), modality="voice")
    await store.record_turn("sess_1", "u1", "正常的问题", "正常的回答", at(1), modality="voice")

    assert await store.recent_turns("sess_1", max_turns=3) == [("正常的问题", "正常的回答")]


async def test_blank_first_question_still_gets_a_usable_title(store) -> None:
    """语音里 AI 常先开口，首轮的提问侧可能是空的。title 列非空，
    直接写空串会让列表出现一行没有文字的条目。"""
    await store.record_turn("sess_1", "u1", "", "你好，我在", at(0), modality="voice")

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.title.strip(), "标题不能是空白"
