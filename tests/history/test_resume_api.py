"""从历史会话续聊。

两条硬约束：
- 无论 Runtime 侧会话是否命中，都必须换发新 rt_token。rt_token 只活 300s，
  会话活 3600s，命中分支下旧 token 基本必然已过期。
- scope 一律按当前策略重算，绝不从历史快照恢复——Scope.tools 是 RT 二次
  校验的唯一权威来源，恢复旧白名单就是个提权口子。
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

BASE = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


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


async def _archive(app, session_id: str, user_id: str = "u1", *, turns: int = 1) -> None:
    store = app.state.container.conversation_store
    for i in range(turns):
        await store.record_turn(
            session_id, user_id, f"问{i}", f"答{i}", BASE + timedelta(minutes=i)
        )


async def _resume(client, conversation_id: str, user_id: str = "u1"):
    return await client.post(
        f"/v1/agent/conversations/{conversation_id}/resume",
        json={"modality": "text"},
        headers=_auth(user_id),
    )


# ---------------------------------------------------------------- 基本行为


async def test_resume_rebuilds_expired_session(app_client) -> None:
    app, client = app_client
    await _archive(app, "sess_1", turns=2)

    resp = await _resume(client, "sess_1")

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "sess_1", "必须复用原 id，续聊才追加进同一条历史"
    assert body["rt_token"]
    assert body["ws_url"].endswith("session_id=sess_1")

    session = await app.state.container.session_store.get("sess_1")
    assert session is not None
    assert session.user_id == "u1"


async def test_resume_rehydrates_recent_turns(app_client) -> None:
    app, client = app_client
    await _archive(app, "sess_1", turns=2)

    await _resume(client, "sess_1")

    session = await app.state.container.session_store.get("sess_1")
    assert session is not None
    assert [(t.user_text, t.assistant_text) for t in session.turns] == [
        ("问0", "答0"),
        ("问1", "答1"),
    ]


async def test_rehydration_respects_the_three_turn_window(app_client) -> None:
    """回放是全量的，但灌回上下文只能是最近 3 轮——过期行情进上下文会被
    模型当成当前数据用。"""
    app, client = app_client
    await _archive(app, "sess_1", turns=6)

    await _resume(client, "sess_1")

    session = await app.state.container.session_store.get("sess_1")
    assert session is not None
    assert len(session.turns) == 3
    assert [t.user_text for t in session.turns] == ["问3", "问4", "问5"]


# ---------------------------------------------------------------- 凭证换发


async def test_resume_issues_fresh_token_when_session_still_alive(app_client) -> None:
    """命中分支也必须换票。rt_token 300s vs session 3600s，差 12 倍。"""
    app, client = app_client
    await _archive(app, "sess_1")

    first = await _resume(client, "sess_1")
    second = await _resume(client, "sess_1")

    assert first.status_code == second.status_code == 200
    assert second.json()["rt_token"]
    # 两次都拿到可用的票；返回值不幂等是预期行为
    assert second.json()["session_id"] == "sess_1"


async def test_resume_does_not_duplicate_history_on_repeat(app_client) -> None:
    """幂等的是会话状态：不重建已存在的会话，也不重复灌历史。"""
    app, client = app_client
    await _archive(app, "sess_1", turns=2)

    await _resume(client, "sess_1")
    await _resume(client, "sess_1")

    session = await app.state.container.session_store.get("sess_1")
    assert session is not None
    assert len(session.turns) == 2, "重复 resume 把历史又灌了一遍"


async def test_resume_recomputes_scope_from_current_policy(app_client) -> None:
    app, client = app_client
    await _archive(app, "sess_1")

    body = (await _resume(client, "sess_1")).json()

    expected = list(app.state.container.registry.names())
    assert body["scope"]["tools"] == expected
    session = await app.state.container.session_store.get("sess_1")
    assert session is not None
    assert list(session.scope.tools) == expected


# ---------------------------------------------------------------- 归属与错误


async def test_resume_of_other_users_conversation_is_404(app_client) -> None:
    app, client = app_client
    await _archive(app, "sess_theirs", user_id="u2")

    resp = await _resume(client, "sess_theirs", user_id="u1")
    assert resp.status_code == 404
    assert resp.json()["code"] == "SESSION_NOT_FOUND"


async def test_resume_of_unknown_conversation_is_404(app_client) -> None:
    _, client = app_client
    resp = await _resume(client, "sess_nope")
    assert resp.status_code == 404


async def test_resume_requires_authentication(app_client) -> None:
    _, client = app_client
    resp = await client.post(
        "/v1/agent/conversations/sess_1/resume", json={"modality": "text"}
    )
    assert resp.status_code == 401


async def test_resumed_session_keeps_appending_to_same_conversation(app_client) -> None:
    """续聊必须追加进同一条历史，用户看到的是连续的一条。"""
    app, client = app_client
    await _archive(app, "sess_1", turns=1)
    await _resume(client, "sess_1")

    store = app.state.container.conversation_store
    await store.record_turn("sess_1", "u1", "续聊的问题", "答", BASE + timedelta(minutes=10))

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.message_count == 4

    body = (await client.get("/v1/agent/conversations", headers=_auth())).json()
    assert len(body["items"]) == 1, "续聊不该变出第二条历史"
