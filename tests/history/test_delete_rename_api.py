"""删除与重命名历史会话。

删除是真删（历史里有仓位与资产类对话，用户点删除就是期待真的没了），
所以要连运行时会话一起关掉 —— 只删历史的话，下一轮对话会因为
record_turn 是懒创建而把会话行重建出来，表现成「删不掉」。
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


async def _live_session(app, session_id: str, user_id: str = "u1") -> None:
    await app.state.container.session_store.create(
        Session(
            session_id=session_id,
            user_id=user_id,
            modality="text",
            scope=Scope(agents=("avatar",), tools=()),
            state="text_active",
            created_at=BASE,
            last_activity_at=BASE,
        )
    )


# ---------------------------------------------------------------- 删除


async def test_delete_returns_204_and_removes_everything(app_client) -> None:
    app, client = app_client
    await _archive(app, "sess_1", turns=2)

    resp = await client.delete("/v1/agent/conversations/sess_1", headers=_auth())

    assert resp.status_code == 204
    store = app.state.container.conversation_store
    assert await store.get_conversation("sess_1") is None
    items, _ = await store.list_messages("sess_1", limit=10, cursor=None)
    assert items == []


async def test_delete_also_closes_the_runtime_session(app_client) -> None:
    """只删历史不关会话，下一轮 record_turn 会把会话行重建出来——用户会
    发现删掉的历史又回来了。"""
    app, client = app_client
    await _archive(app, "sess_1")
    await _live_session(app, "sess_1")

    await client.delete("/v1/agent/conversations/sess_1", headers=_auth())

    assert await app.state.container.session_store.get("sess_1") is None


async def test_delete_works_when_runtime_session_already_expired(app_client) -> None:
    """绝大多数删除都发生在会话早已过期之后，不能因为 session 不在就报错。"""
    app, client = app_client
    await _archive(app, "sess_1")

    resp = await client.delete("/v1/agent/conversations/sess_1", headers=_auth())
    assert resp.status_code == 204


async def test_delete_leaves_other_conversations_intact(app_client) -> None:
    app, client = app_client
    await _archive(app, "sess_1")
    await _archive(app, "sess_2")

    await client.delete("/v1/agent/conversations/sess_1", headers=_auth())

    body = (await client.get("/v1/agent/conversations", headers=_auth())).json()
    assert [i["conversation_id"] for i in body["items"]] == ["sess_2"]


async def test_deleted_conversation_disappears_from_list(app_client) -> None:
    app, client = app_client
    await _archive(app, "sess_1")

    await client.delete("/v1/agent/conversations/sess_1", headers=_auth())

    body = (await client.get("/v1/agent/conversations", headers=_auth())).json()
    assert body["items"] == []


async def test_deleted_conversation_cannot_be_resumed(app_client) -> None:
    app, client = app_client
    await _archive(app, "sess_1")
    await client.delete("/v1/agent/conversations/sess_1", headers=_auth())

    resp = await client.post(
        "/v1/agent/conversations/sess_1/resume",
        json={"modality": "text"},
        headers=_auth(),
    )
    assert resp.status_code == 404


async def test_deleting_twice_is_404(app_client) -> None:
    app, client = app_client
    await _archive(app, "sess_1")

    assert (
        await client.delete("/v1/agent/conversations/sess_1", headers=_auth())
    ).status_code == 204
    assert (
        await client.delete("/v1/agent/conversations/sess_1", headers=_auth())
    ).status_code == 404


async def test_delete_of_other_users_conversation_is_404(app_client) -> None:
    app, client = app_client
    await _archive(app, "sess_theirs", user_id="u2")

    resp = await client.delete("/v1/agent/conversations/sess_theirs", headers=_auth("u1"))
    assert resp.status_code == 404
    assert resp.json()["code"] == "SESSION_NOT_FOUND"
    # 别人的数据必须还在
    store = app.state.container.conversation_store
    assert await store.get_conversation("sess_theirs") is not None


async def test_delete_of_unknown_conversation_is_404(app_client) -> None:
    _, client = app_client
    resp = await client.delete("/v1/agent/conversations/sess_nope", headers=_auth())
    assert resp.status_code == 404


async def test_delete_requires_authentication(app_client) -> None:
    _, client = app_client
    assert (await client.delete("/v1/agent/conversations/sess_1")).status_code == 401


# ---------------------------------------------------------------- 重命名


async def test_rename_updates_title_and_returns_summary(app_client) -> None:
    app, client = app_client
    await _archive(app, "sess_1")

    resp = await client.patch(
        "/v1/agent/conversations/sess_1",
        json={"title": "我的持仓分析"},
        headers=_auth(),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation_id"] == "sess_1"
    assert body["title"] == "我的持仓分析"
    assert "session_id" not in body


async def test_rename_is_visible_in_list(app_client) -> None:
    app, client = app_client
    await _archive(app, "sess_1")

    await client.patch(
        "/v1/agent/conversations/sess_1", json={"title": "改过的名字"}, headers=_auth()
    )

    body = (await client.get("/v1/agent/conversations", headers=_auth())).json()
    assert body["items"][0]["title"] == "改过的名字"


async def test_rename_trims_surrounding_whitespace(app_client) -> None:
    app, client = app_client
    await _archive(app, "sess_1")

    resp = await client.patch(
        "/v1/agent/conversations/sess_1", json={"title": "  两侧有空格  "}, headers=_auth()
    )
    assert resp.json()["title"] == "两侧有空格"


@pytest.mark.parametrize("title", ["", "   ", "\n\t"])
async def test_rename_rejects_blank_title(app_client, title: str) -> None:
    """空标题会把列表项的主文案清掉，比留着原标题差得多。"""
    app, client = app_client
    await _archive(app, "sess_1")

    resp = await client.patch(
        "/v1/agent/conversations/sess_1", json={"title": title}, headers=_auth()
    )
    assert resp.status_code == 422


async def test_rename_rejects_overlong_title(app_client) -> None:
    """title 列是 String(200)，放过去就是写库失败。"""
    app, client = app_client
    await _archive(app, "sess_1")

    resp = await client.patch(
        "/v1/agent/conversations/sess_1", json={"title": "长" * 201}, headers=_auth()
    )
    assert resp.status_code == 422


async def test_rename_of_other_users_conversation_is_404(app_client) -> None:
    app, client = app_client
    await _archive(app, "sess_theirs", user_id="u2")

    resp = await client.patch(
        "/v1/agent/conversations/sess_theirs", json={"title": "x"}, headers=_auth("u1")
    )
    assert resp.status_code == 404

    store = app.state.container.conversation_store
    convo = await store.get_conversation("sess_theirs")
    assert convo is not None
    assert convo.title == "问0", "改到了别人的会话"


async def test_rename_of_unknown_conversation_is_404(app_client) -> None:
    _, client = app_client
    resp = await client.patch(
        "/v1/agent/conversations/sess_nope", json={"title": "x"}, headers=_auth()
    )
    assert resp.status_code == 404


async def test_rename_requires_authentication(app_client) -> None:
    _, client = app_client
    resp = await client.patch("/v1/agent/conversations/sess_1", json={"title": "x"})
    assert resp.status_code == 401


async def test_rename_survives_a_later_titler_write(app_client) -> None:
    """端到端锁住那个竞态：用户改名后，异步 titler 完成也不能覆盖。"""
    app, client = app_client
    await _archive(app, "sess_1")
    await client.patch(
        "/v1/agent/conversations/sess_1", json={"title": "用户改的名字"}, headers=_auth()
    )

    # 模拟 titler 在 1.5~3 分钟后返回，带着建行时的降级值做守卫
    store = app.state.container.conversation_store
    await store.set_title("sess_1", "模型生成的标题", only_if="问0")

    body = (await client.get("/v1/agent/conversations", headers=_auth())).json()
    assert body["items"][0]["title"] == "用户改的名字"
