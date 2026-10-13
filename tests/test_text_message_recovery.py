from __future__ import annotations

import asyncio
from datetime import datetime

import httpx
from fastapi import FastAPI

from app.agents.runner import ExecutedTool, RunResult
from app.api.agent import router as agent_router
from app.config import Settings
from app.container import Container, build_container

USER = {"Authorization": "Bearer test-user:u1"}
RAW_IDEMPOTENCY_KEY = "mobile-message-key-never-persist"


class RecoverableDraftRunner:
    def __init__(self) -> None:
        self.calls = 0
        self.confirm_calls = 0

    async def run_turn(
        self,
        session,
        user_text: str,
        trace_id: str,
        now: datetime,
        *,
        user_bearer_token: str | None = None,
        idempotency_key: str | None = None,
        web_search: bool = False,
    ) -> RunResult:
        self.calls += 1
        await asyncio.sleep(0.02)
        return RunResult(
            text="已生成待确认的 BTC 限价计划。",
            executed_tools=(
                ExecutedTool(
                    call_id="call_draft",
                    name="create_perp_trade_draft",
                    args_hash="a" * 64,
                    result={
                        "core_draft_id": "draft_core_recoverable",
                        "draft_version": 1,
                        "confirmation_mode": "ALWAYS_CONFIRM",
                        "execution_requested": False,
                    },
                    trade_draft_id="draft_core_recoverable",
                ),
            ),
        )


def _app() -> tuple[FastAPI, Container, RecoverableDraftRunner]:
    container = build_container(Settings(redis_url=""))
    runner = RecoverableDraftRunner()
    container.runner = runner  # type: ignore[assignment]
    app = FastAPI()
    app.state.container = container
    app.include_router(agent_router, prefix="/v1/agent")
    return app, container, runner


async def _create_session(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/v1/agent/sessions",
        json={"modality": "text"},
        headers=USER,
    )
    assert response.status_code == 201
    return str(response.json()["session_id"])


async def test_same_message_key_runs_once_and_recovers_same_draft_result() -> None:
    app, container, runner = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_id = await _create_session(client)
        request = {
            "session_id": session_id,
            "text": "生成 BTC 约 10 USDC 1x LIMIT Draft，但不要执行",
        }
        headers = {**USER, "Idempotency-Key": RAW_IDEMPOTENCY_KEY}
        first, duplicate = await asyncio.gather(
            client.post("/v1/agent/messages", json=request, headers=headers),
            client.post("/v1/agent/messages", json=request, headers=headers),
        )
        recovered = await client.post("/v1/agent/messages", json=request, headers=headers)

    assert first.status_code == duplicate.status_code == recovered.status_code == 200
    assert first.json() == duplicate.json() == recovered.json()
    assert runner.calls == 1
    assert runner.confirm_calls == 0
    result = recovered.json()["tool_calls"][0]
    assert result["trade_draft_id"] == "draft_core_recoverable"
    assert result["output"]["core_draft_id"] == "draft_core_recoverable"
    assert result["output"]["execution_requested"] is False
    session = await container.session_store.get(session_id)
    assert session is not None
    assert len(session.text_turn_results) == 1
    assert RAW_IDEMPOTENCY_KEY not in repr(session)
    assert "test-user:u1" not in repr(session)


async def test_same_message_without_header_uses_deterministic_recovery_key() -> None:
    app, _container, runner = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_id = await _create_session(client)
        request = {"session_id": session_id, "text": "同一个用户消息"}
        first = await client.post("/v1/agent/messages", json=request, headers=USER)
        recovered = await client.post("/v1/agent/messages", json=request, headers=USER)

    assert first.status_code == recovered.status_code == 200
    assert first.json() == recovered.json()
    assert runner.calls == 1


async def test_idempotency_key_reuse_with_different_message_is_409() -> None:
    app, _container, runner = _app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session_id = await _create_session(client)
        headers = {**USER, "Idempotency-Key": RAW_IDEMPOTENCY_KEY}
        first = await client.post(
            "/v1/agent/messages",
            json={"session_id": session_id, "text": "BTC Draft"},
            headers=headers,
        )
        conflict = await client.post(
            "/v1/agent/messages",
            json={"session_id": session_id, "text": "ETH Draft"},
            headers=headers,
        )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert runner.calls == 1
