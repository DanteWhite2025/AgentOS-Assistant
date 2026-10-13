"""历史对话读接口。

所有权语义必须与 owned_session 一致：不存在与跨用户一律折叠成 404，
不能让调用方通过状态码区分「这条不属于你」和「这条不存在」。
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


def at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


@pytest.fixture
async def app_client():
    app = FastAPI()
    app.state.container = build_container(Settings(redis_url=""))

    # 鉴权失败要变成 401 响应而不是往上抛。main.create_app 装了这个处理器，
    # 最小测试 app 不装的话「未带 token」这条用例测的就不是真实行为。
    @app.exception_handler(UserAuthenticationError)
    async def _auth_error(_request, exc: UserAuthenticationError):
        return error_response(exc.code, exc.message, exc.status_code)

    app.include_router(agent_router, prefix="/v1/agent")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield app, client


def _auth(user_id: str = "u1") -> dict[str, str]:
    """FakeCoreUserAuthenticator 只认 `test-user:<user_id>` 前缀。"""
    return {"Authorization": f"Bearer test-user:{user_id}"}


async def _seed(app, session_id: str, user_id: str, *, minutes: int = 0, turns: int = 1) -> None:
    store = app.state.container.conversation_store
    for i in range(turns):
        await store.record_turn(session_id, user_id, f"问{i}", f"答{i}", at(minutes + i))


# ---------------------------------------------------------------- 列表


async def test_list_returns_conversation_summaries(app_client) -> None:
    app, client = app_client
    await _seed(app, "sess_1", "u1")

    resp = await client.get("/v1/agent/conversations", headers=_auth())

    assert resp.status_code == 200
    body = resp.json()
    assert body["next_cursor"] is None
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["conversation_id"] == "sess_1"
    assert item["title"] == "问0"
    assert item["modality"] == "text"
    assert item["message_count"] == 2


async def test_list_never_exposes_session_id_field(app_client) -> None:
    """对外只认 conversation_id —— 露出 session_id 会让客户端以为能直接连 WS。"""
    app, client = app_client
    await _seed(app, "sess_1", "u1")

    item = (await client.get("/v1/agent/conversations", headers=_auth())).json()["items"][0]
    assert "session_id" not in item


async def test_list_is_scoped_to_caller(app_client) -> None:
    app, client = app_client
    await _seed(app, "sess_mine", "u1")
    await _seed(app, "sess_theirs", "u2")

    body = (await client.get("/v1/agent/conversations", headers=_auth("u1"))).json()
    assert [i["conversation_id"] for i in body["items"]] == ["sess_mine"]


async def test_list_paginates_with_cursor(app_client) -> None:
    app, client = app_client
    for i in range(3):
        await _seed(app, f"sess_{i}", "u1", minutes=i)

    first = (await client.get("/v1/agent/conversations?limit=2", headers=_auth())).json()
    assert [i["conversation_id"] for i in first["items"]] == ["sess_2", "sess_1"]
    assert first["next_cursor"]

    second = (
        await client.get(
            f"/v1/agent/conversations?limit=2&cursor={first['next_cursor']}", headers=_auth()
        )
    ).json()
    assert [i["conversation_id"] for i in second["items"]] == ["sess_0"]
    assert second["next_cursor"] is None


async def test_list_rejects_malformed_cursor(app_client) -> None:
    _, client = app_client
    resp = await client.get("/v1/agent/conversations?cursor=!!!bad!!!", headers=_auth())
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_REQUEST"


async def test_list_requires_authentication(app_client) -> None:
    _, client = app_client
    assert (await client.get("/v1/agent/conversations")).status_code == 401


@pytest.mark.parametrize("limit", [0, 51])
async def test_list_rejects_out_of_range_limit(app_client, limit: int) -> None:
    _, client = app_client
    resp = await client.get(f"/v1/agent/conversations?limit={limit}", headers=_auth())
    assert resp.status_code == 422


# ---------------------------------------------------------------- 回放


async def test_messages_are_returned_chronologically(app_client) -> None:
    app, client = app_client
    await _seed(app, "sess_1", "u1", turns=2)

    body = (await client.get("/v1/agent/conversations/sess_1/messages", headers=_auth())).json()
    assert [(m["role"], m["content"]) for m in body["items"]] == [
        ("user", "问0"),
        ("assistant", "答0"),
        ("user", "问1"),
        ("assistant", "答1"),
    ]


async def test_messages_paginate_forward(app_client) -> None:
    app, client = app_client
    await _seed(app, "sess_1", "u1", turns=3)

    first = (
        await client.get("/v1/agent/conversations/sess_1/messages?limit=2", headers=_auth())
    ).json()
    assert [m["content"] for m in first["items"]] == ["问0", "答0"]

    second = (
        await client.get(
            f"/v1/agent/conversations/sess_1/messages?limit=2&cursor={first['next_cursor']}",
            headers=_auth(),
        )
    ).json()
    assert [m["content"] for m in second["items"]] == ["问1", "答1"]


async def test_messages_of_other_users_conversation_are_404(app_client) -> None:
    """跨用户必须与不存在同码，否则可以拿状态码枚举出别人的 conversation_id。"""
    app, client = app_client
    await _seed(app, "sess_theirs", "u2")

    resp = await client.get("/v1/agent/conversations/sess_theirs/messages", headers=_auth("u1"))
    assert resp.status_code == 404
    assert resp.json()["code"] == "SESSION_NOT_FOUND"


async def test_messages_of_unknown_conversation_are_404(app_client) -> None:
    _, client = app_client
    resp = await client.get("/v1/agent/conversations/sess_nope/messages", headers=_auth())
    assert resp.status_code == 404
    assert resp.json()["code"] == "SESSION_NOT_FOUND"


async def test_messages_reject_malformed_cursor(app_client) -> None:
    app, client = app_client
    await _seed(app, "sess_1", "u1")

    resp = await client.get(
        "/v1/agent/conversations/sess_1/messages?cursor=!!!bad!!!", headers=_auth()
    )
    assert resp.status_code == 400
