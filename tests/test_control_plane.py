"""控制面端到端测试（in-process ASGI，Fake provider）。"""

from __future__ import annotations

import httpx

from app.main import app

U1 = {"Authorization": "Bearer test-user:u1"}
U2 = {"Authorization": "Bearer test-user:u2"}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_full_session_flow() -> None:
    async with _client() as client:
        # 创建会话（both → 含 voice 凭证）
        r = await client.post("/v1/agent/sessions", json={"modality": "both"}, headers=U1)
        assert r.status_code == 201, r.text
        body = r.json()
        sid = body["session_id"]
        assert body["rt_token"]
        assert body["voice"]["provider"] == "fake"
        assert body["voice"]["client_secret"].startswith("ek_")
        assert "get_positions" in body["scope"]["tools"]
        assert body["ws_url"].endswith(f"session_id={sid}")

        # 查会话
        r = await client.get(f"/v1/agent/sessions/{sid}", headers=U1)
        assert r.status_code == 200
        assert r.json()["state"] == "created"

        # 客户端进房成功后显式启动 AI 任务，重复调用保持幂等。
        for _ in range(2):
            r = await client.post(f"/v1/agent/sessions/{sid}/voice/start", headers=U1)
            assert r.status_code == 200, r.text
            assert r.json()["state"] in {"task_accepted", "voice_ready"}

        # 文本单轮：问持仓 → 触发工具 → 有回复
        r = await client.post(
            "/v1/agent/messages",
            json={"session_id": sid, "text": "帮我看下持仓"},
            headers=U1,
        )
        assert r.status_code == 200, r.text
        msg = r.json()
        assert msg["text"]
        assert msg["tool_calls"]  # 至少执行了一个工具

        # 历史含审计记录
        r = await client.get(f"/v1/agent/sessions/{sid}/history", headers=U1)
        assert r.status_code == 200
        assert len(r.json()["items"]) >= 1

        # 关闭 → 再查 应该保留 tombstone
        r = await client.delete(f"/v1/agent/sessions/{sid}", headers=U1)
        assert r.status_code == 204
        r = await client.get(f"/v1/agent/sessions/{sid}", headers=U1)
        assert r.status_code == 200
        assert r.json()["state"] == "voice_stopped"


async def test_tool_relay_auth_and_whitelist() -> None:
    async with _client() as client:
        body = (
            await client.post("/v1/agent/sessions", json={"modality": "text"}, headers=U1)
        ).json()
        sid, rt = body["session_id"], body["rt_token"]

        # 无 rt_token → 401
        r = await client.post(
            f"/v1/agent/sessions/{sid}/tool-calls",
            json={"call_id": "c1", "tool_name": "get_positions", "arguments": {}},
        )
        assert r.status_code == 401

        # 合法 token + 白名单工具 → 200
        r = await client.post(
            f"/v1/agent/sessions/{sid}/tool-calls",
            json={"call_id": "c1", "tool_name": "get_positions", "arguments": {}},
            headers={"Authorization": f"Bearer {rt}"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok"

        # 伪造未注册工具 → 403 TOOL_NOT_IN_SCOPE
        r = await client.post(
            f"/v1/agent/sessions/{sid}/tool-calls",
            json={"call_id": "c2", "tool_name": "evil_tool", "arguments": {}},
            headers={"Authorization": f"Bearer {rt}"},
        )
        assert r.status_code == 403
        assert r.json()["code"] == "TOOL_NOT_IN_SCOPE"


async def test_create_trade_draft_via_relay() -> None:
    async with _client() as client:
        body = (
            await client.post("/v1/agent/sessions", json={"modality": "text"}, headers=U1)
        ).json()
        sid, rt = body["session_id"], body["rt_token"]
        r = await client.post(
            f"/v1/agent/sessions/{sid}/tool-calls",
            json={
                "call_id": "c1",
                "tool_name": "create_trade_draft",
                "arguments": {"symbol": "ETH", "side": "long", "qty": "1"},
            },
            headers={"Authorization": f"Bearer {rt}"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["trade_draft_id"].startswith("td_")


async def test_missing_session_404() -> None:
    async with _client() as client:
        r = await client.get("/v1/agent/sessions/does_not_exist", headers=U1)
        assert r.status_code == 404
        assert r.json()["code"] == "SESSION_NOT_FOUND"


async def test_anonymous_session_create_is_401() -> None:
    async with _client() as client:
        response = await client.post("/v1/agent/sessions", json={"modality": "voice"})
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"


async def test_x_user_id_is_ignored_and_core_identity_owns_session() -> None:
    async with _client() as client:
        response = await client.post(
            "/v1/agent/sessions",
            json={"modality": "text"},
            headers={**U1, "X-User-Id": "spoofed"},
        )
        assert response.status_code == 201
        sid = response.json()["session_id"]
        assert (await app.state.container.session_store.get(sid)).user_id == "u1"


async def test_cross_user_session_read_and_voice_refresh_are_hidden() -> None:
    async with _client() as client:
        created = await client.post("/v1/agent/sessions", json={"modality": "voice"}, headers=U1)
        sid = created.json()["session_id"]

        read = await client.get(f"/v1/agent/sessions/{sid}", headers=U2)
        refresh = await client.post(
            f"/v1/agent/sessions/{sid}/token",
            json={"want": ["voice"]},
            headers=U2,
        )

    assert read.status_code == 404
    assert refresh.status_code == 404
    assert read.json()["code"] == refresh.json()["code"] == "SESSION_NOT_FOUND"


async def test_invalid_core_user_token_is_401() -> None:
    async with _client() as client:
        response = await client.post(
            "/v1/agent/sessions",
            json={"modality": "text"},
            headers={"Authorization": "Bearer not-a-test-user-token"},
        )
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"


async def test_rt_token_cannot_be_used_for_another_session_or_user() -> None:
    async with _client() as client:
        first = (
            await client.post("/v1/agent/sessions", json={"modality": "text"}, headers=U1)
        ).json()
        second = (
            await client.post("/v1/agent/sessions", json={"modality": "text"}, headers=U1)
        ).json()
        mismatch = await client.post(
            f"/v1/agent/sessions/{second['session_id']}/tool-calls",
            json={"call_id": "c-mismatch", "tool_name": "get_positions", "arguments": {}},
            headers={"Authorization": f"Bearer {first['rt_token']}"},
        )

        session = await app.state.container.session_store.get(first["session_id"])
        wrong_user_token, _ = app.state.container.token_service.issue_rt_token(
            first["session_id"], "u2", session.scope.tools, session.created_at
        )
        wrong_user = await client.post(
            f"/v1/agent/sessions/{first['session_id']}/tool-calls",
            json={"call_id": "c-user", "tool_name": "get_positions", "arguments": {}},
            headers={"Authorization": f"Bearer {wrong_user_token}"},
        )

    assert mismatch.status_code == 401
    assert wrong_user.status_code == 401
