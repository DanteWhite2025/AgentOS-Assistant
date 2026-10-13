"""Session 上的对话历史与裁剪规则。

历史是修「点追问 chip 答案讲了别人」的地基（见
docs/plans/2026-09-04-text-turn-conversation-history/TECH_DESIGN.md §1）。裁剪
必须两道限制都有：光卡轮数拦不住 3 轮深度分析（每轮 500~800 字）把 prompt 撑大、
首字延迟拉高；光卡字数又会把一整轮切成半句，模型读到的是一句没头没尾的话。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from app.runtime.session import ConversationTurn, Scope, Session


def _now(minute: int = 0) -> datetime:
    return datetime(2026, 9, 4, 12, minute, 0, tzinfo=UTC)


def _session() -> Session:
    return Session(
        session_id="sess_1",
        user_id="u1",
        modality="text",
        scope=Scope(agents=("avatar",), tools=()),
        state="created",
        created_at=_now(),
        last_activity_at=_now(),
    )


def _turn(index: int, assistant_chars: int = 10) -> ConversationTurn:
    return ConversationTurn(
        user_text=f"问题{index}",
        assistant_text="答" * assistant_chars,
        created_at=_now(index),
    )


def test_new_session_has_no_history() -> None:
    assert _session().turns == ()


def test_appending_a_turn_returns_a_new_session() -> None:
    """不可变：原对象不能被改。"""

    original = _session()
    updated = original.with_conversation_turn(_now(1), _turn(1))

    assert original.turns == ()
    assert len(updated.turns) == 1
    assert updated.turns[0].user_text == "问题1"


def test_appending_marks_the_session_text_active() -> None:
    """与既有的 with_text_turn_result 对齐：两者都表示刚发生了一轮文字对话。"""

    updated = _session().with_conversation_turn(_now(1), _turn(1))

    assert updated.state == "text_active"
    assert updated.last_activity_at == _now(1)


def test_history_keeps_only_the_most_recent_turns() -> None:
    session = _session()
    for i in range(1, 6):
        session = session.with_conversation_turn(_now(i), _turn(i))

    assert [t.user_text for t in session.turns] == ["问题3", "问题4", "问题5"]


def test_history_drops_oldest_turns_over_the_char_budget() -> None:
    """三轮没超轮数上限，但合计超了字符预算——从最老的一端丢。"""

    session = _session()
    for i in range(1, 4):
        session = session.with_conversation_turn(_now(i), _turn(i, assistant_chars=2500))

    # 每轮约 2503 字（含 user_text），3 轮 7509 > 6000，最老那轮被丢掉
    assert [t.user_text for t in session.turns] == ["问题2", "问题3"]


def test_the_newest_turn_survives_even_when_it_alone_exceeds_the_budget() -> None:
    """丢掉刚追加的那一轮等于这次写入静默失效——只能截断它的 assistant_text。

    user_text 不截：它是这一轮到底问了什么，截了就没意义了。
    """

    session = _session().with_conversation_turn(
        _now(1), ConversationTurn("问题1", "答" * 9000, _now(1))
    )

    assert len(session.turns) == 1
    kept = session.turns[0]
    assert kept.user_text == "问题1"
    assert len(kept.assistant_text) == 6000 - len("问题1")


def test_oversized_newest_turn_evicts_all_older_history() -> None:
    session = _session().with_conversation_turn(_now(1), _turn(1))
    session = session.with_conversation_turn(
        _now(2), ConversationTurn("问题2", "答" * 9000, _now(2))
    )

    assert [t.user_text for t in session.turns] == ["问题2"]


def test_history_survives_a_redis_dump_load_round_trip() -> None:
    """Session 走 Redis 是整体 JSON set/get。新字段漏进序列化的话，本地内存
    store 测试全绿，staging 上历史永远是空的——而且不报错。
    """

    from app.runtime.redis_session_store import RedisSessionStore

    session = replace(_session(), voice_provider_name="gemini_live").with_conversation_turn(
        _now(1), _turn(1)
    )
    session = session.with_conversation_turn(_now(2), _turn(2))

    restored = RedisSessionStore._load(RedisSessionStore._dump(session))

    assert [t.user_text for t in restored.turns] == ["问题1", "问题2"]
    assert restored.turns[0].assistant_text == "答" * 10
    assert restored.turns[1].created_at == _now(2)
    assert restored.voice_provider_name == "gemini_live"


def test_loading_a_payload_without_turns_yields_empty_history() -> None:
    """已经在 Redis 里的旧会话没有这个键，不能因此崩掉。"""

    import json

    from app.runtime.redis_session_store import RedisSessionStore

    raw = json.loads(RedisSessionStore._dump(_session()))
    raw.pop("turns", None)

    assert RedisSessionStore._load(json.dumps(raw)).turns == ()


def test_loading_an_old_payload_without_voice_provider_uses_default() -> None:
    """部署前已存在的 Redis 会话没有渠道字段，仍须兼容读取。"""

    import json

    from app.runtime.redis_session_store import RedisSessionStore

    raw = json.loads(
        RedisSessionStore._dump(replace(_session(), voice_provider_name="gemini_live"))
    )
    raw.pop("voice_provider_name")

    assert RedisSessionStore._load(json.dumps(raw)).voice_provider_name is None


def test_loading_skips_malformed_history_entries() -> None:
    """照 text_turn_results 的既有防御模式：字段不全的条目跳过，不抛。"""

    import json

    from app.runtime.redis_session_store import RedisSessionStore

    raw = json.loads(RedisSessionStore._dump(_session().with_conversation_turn(_now(1), _turn(1))))
    raw["turns"].append({"user_text": "缺字段"})
    raw["turns"].append("不是对象")

    restored = RedisSessionStore._load(json.dumps(raw))

    assert [t.user_text for t in restored.turns] == ["问题1"]
