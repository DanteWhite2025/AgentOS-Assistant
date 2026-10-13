"""语音字幕归档进历史。

数据源是客户端上报（POST /sessions/{id}/history）——服务端此前拿到正文只
抽时间戳就丢掉（provider 侧的回调解析器也刻意不保留正文）。二期 B 把这条
丢掉的正文接进历史库。

归档失败绝不能影响 202：这个端点的本职是上报语音证据时间戳，语音链路的
可观测性不该被历史库的抖动拖下水。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.agent import router as agent_router
from app.api.errors import error_response
from app.auth.core_user import UserAuthenticationError
from app.config import Settings
from app.container import build_container
from app.runtime.session import Scope, Session

BASE = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def at(seconds: int) -> str:
    return (BASE + timedelta(seconds=seconds)).isoformat()


@pytest.fixture
async def app_client():
    app = FastAPI()
    app.state.container = build_container(Settings(redis_url=""))

    @app.exception_handler(UserAuthenticationError)
    async def _auth_error(_request, exc: UserAuthenticationError):
        return error_response(exc.code, exc.message, exc.status_code)

    app.include_router(agent_router, prefix="/v1/agent")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield app, client


def _auth(user_id: str = "u1") -> dict[str, str]:
    return {"Authorization": f"Bearer test-user:{user_id}"}


async def _voice_session(app, session_id: str = "sess_1", user_id: str = "u1") -> None:
    await app.state.container.session_store.create(
        Session(
            session_id=session_id,
            user_id=user_id,
            modality="voice",
            scope=Scope(agents=("avatar",), tools=()),
            state="voice_ready",
            created_at=BASE,
            last_activity_at=BASE,
        )
    )


async def _report(client, turns, session_id: str = "sess_1", user_id: str = "u1"):
    return await client.post(
        f"/v1/agent/sessions/{session_id}/history",
        json={"source": "voice", "turns": turns},
        headers=_auth(user_id),
    )


# ---------------------------------------------------------------- 归档


async def test_reported_transcript_lands_in_history(app_client) -> None:
    app, client = app_client
    await _voice_session(app)

    resp = await _report(
        client,
        [
            {"role": "user", "text": "BTC 多少钱", "started_at": at(0)},
            {"role": "assistant", "text": "六万二", "started_at": at(2)},
        ],
    )

    assert resp.status_code == 202
    store = app.state.container.conversation_store
    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.modality == "voice"
    items, _ = await store.list_messages("sess_1", limit=10, cursor=None)
    assert [(m.role, m.content) for m in items] == [
        ("user", "BTC 多少钱"),
        ("assistant", "六万二"),
    ]


async def test_archived_voice_conversation_shows_in_list(app_client) -> None:
    app, client = app_client
    await _voice_session(app)
    await _report(
        client,
        [
            {"role": "user", "text": "查下持仓", "started_at": at(0)},
            {"role": "assistant", "text": "你有 2 个仓位", "started_at": at(1)},
        ],
    )

    body = (await client.get("/v1/agent/conversations", headers=_auth())).json()
    assert len(body["items"]) == 1
    assert body["items"][0]["conversation_id"] == "sess_1"
    assert body["items"][0]["modality"] == "voice"
    assert body["items"][0]["title"] == "查下持仓"


async def test_turn_timestamps_come_from_the_report(app_client) -> None:
    """全用服务端 now 会让一整批字幕挤在同一时刻，正序分页只能靠自增 id
    兜底，跨批次顺序会乱。"""
    app, client = app_client
    await _voice_session(app)
    await _report(
        client,
        [
            {"role": "user", "text": "问一", "started_at": at(0)},
            {"role": "assistant", "text": "答一", "started_at": at(5)},
            {"role": "user", "text": "问二", "started_at": at(60)},
            {"role": "assistant", "text": "答二", "started_at": at(65)},
        ],
    )

    store = app.state.container.conversation_store
    items, _ = await store.list_messages("sess_1", limit=10, cursor=None)
    assert items[0].created_at == BASE
    assert items[2].created_at == BASE + timedelta(seconds=60)


async def test_resending_the_same_batch_does_not_duplicate(app_client) -> None:
    """App 是异步批量上报，网络重试会重发同一批。重复历史是用户可见的
    数据损坏，不能靠「客户端保证不重传」。"""
    app, client = app_client
    await _voice_session(app)
    turns = [
        {"role": "user", "text": "问", "started_at": at(0)},
        {"role": "assistant", "text": "答", "started_at": at(2)},
    ]

    await _report(client, turns)
    await _report(client, turns)

    store = app.state.container.conversation_store
    items, _ = await store.list_messages("sess_1", limit=20, cursor=None)
    assert len(items) == 2, f"重传导致重复入库：{[m.content for m in items]}"
    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.message_count == 2


async def test_a_genuinely_new_batch_is_appended(app_client) -> None:
    """去重不能把后续真实轮次也挡掉。"""
    app, client = app_client
    await _voice_session(app)

    await _report(
        client,
        [
            {"role": "user", "text": "问一", "started_at": at(0)},
            {"role": "assistant", "text": "答一", "started_at": at(2)},
        ],
    )
    await _report(
        client,
        [
            {"role": "user", "text": "问二", "started_at": at(60)},
            {"role": "assistant", "text": "答二", "started_at": at(62)},
        ],
    )

    store = app.state.container.conversation_store
    items, _ = await store.list_messages("sess_1", limit=20, cursor=None)
    assert [m.content for m in items] == ["问一", "答一", "问二", "答二"]


async def test_interrupted_answer_is_archived_with_what_was_said(app_client) -> None:
    app, client = app_client
    await _voice_session(app)

    await _report(
        client,
        [
            {"role": "user", "text": "BTC 怎么样", "started_at": at(0)},
            {"role": "assistant", "text": "六万二，今天", "started_at": at(2)},
        ],
    )

    store = app.state.container.conversation_store
    items, _ = await store.list_messages("sess_1", limit=10, cursor=None)
    assert items[1].content == "六万二，今天"


async def test_blank_only_report_archives_nothing(app_client) -> None:
    """ASR 吐空串时不该建出一条空历史。"""
    app, client = app_client
    await _voice_session(app)

    resp = await _report(
        client, [{"role": "user", "text": "   "}, {"role": "assistant", "text": ""}]
    )

    assert resp.status_code == 202
    store = app.state.container.conversation_store
    assert await store.get_conversation("sess_1") is None


async def test_archive_failure_does_not_break_the_report(app_client) -> None:
    """这个端点的本职是上报语音证据时间戳，历史库抖动不该拖垮它。"""
    app, client = app_client
    await _voice_session(app)

    async def _boom(*args, **kwargs):
        raise RuntimeError("pg 挂了")

    app.state.container.conversation_store.record_turn = _boom  # type: ignore[method-assign]

    resp = await _report(
        client,
        [
            {"role": "user", "text": "问", "started_at": at(0)},
            {"role": "assistant", "text": "答", "started_at": at(2)},
        ],
    )
    assert resp.status_code == 202


async def test_voice_after_text_marks_conversation_as_both(app_client) -> None:
    app, client = app_client
    await _voice_session(app)
    store = app.state.container.conversation_store
    await store.record_turn("sess_1", "u1", "先打字问的", "答", BASE)

    await _report(
        client,
        [
            {"role": "user", "text": "再语音问", "started_at": at(60)},
            {"role": "assistant", "text": "答", "started_at": at(62)},
        ],
    )

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.modality == "both"


async def test_archive_requires_owning_the_session(app_client) -> None:
    app, client = app_client
    await _voice_session(app, user_id="u2")

    resp = await _report(client, [{"role": "user", "text": "问"}], user_id="u1")
    assert resp.status_code == 404


# ---------------------------------------------------------------- 语音历史续聊


async def test_voice_history_resumes_as_text(app_client) -> None:
    """按定案口径降级成文本聊（对标主流 App：点进历史默认打字续聊）。"""
    app, client = app_client
    await _voice_session(app)
    await _report(
        client,
        [
            {"role": "user", "text": "语音问的", "started_at": at(0)},
            {"role": "assistant", "text": "语音答的", "started_at": at(2)},
        ],
    )
    await app.state.container.session_store.delete("sess_1")

    resp = await client.post(
        "/v1/agent/conversations/sess_1/resume",
        json={"modality": "text"},
        headers=_auth(),
    )

    assert resp.status_code == 200
    session = await app.state.container.session_store.get("sess_1")
    assert session is not None
    assert session.modality == "text"
    assert [(t.user_text, t.assistant_text) for t in session.turns] == [
        ("语音问的", "语音答的")
    ]


async def test_resume_skips_turns_whose_answer_was_interrupted(app_client) -> None:
    """空答案不进上下文，否则模型会以为自己上一轮沉默了。"""
    app, client = app_client
    await _voice_session(app)
    await _report(client, [{"role": "user", "text": "被打断的问题", "started_at": at(0)}])
    await _report(
        client,
        [
            {"role": "user", "text": "正常问题", "started_at": at(60)},
            {"role": "assistant", "text": "正常回答", "started_at": at(62)},
        ],
    )
    await app.state.container.session_store.delete("sess_1")

    await client.post(
        "/v1/agent/conversations/sess_1/resume",
        json={"modality": "text"},
        headers=_auth(),
    )

    session = await app.state.container.session_store.get("sess_1")
    assert session is not None
    assert [t.user_text for t in session.turns] == ["正常问题"]
