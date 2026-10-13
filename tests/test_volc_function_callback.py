from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from app.api.agent import router
from app.config import Settings
from app.container import Container, build_container
from app.runtime.session import Scope, Session
from app.tools.core_client import HttpCoreClient
from app.tools.executor import ToolExecutor
from app.voice.callback_auth import CALLBACK_AUTH_VERSION, derive_callback_signature
from app.voice.provider import FakeVoiceProvider, VoiceTaskAcceptance
from app.voice.volc_openapi import VolcOpenAPIError

FUNCTION_SECRET = "function-secret-at-least-32-characters"


def _ticker() -> dict[str, Any]:
    return {
        "data": [
            {
                "symbol": "BTC",
                "last_price": "67345.10",
                "mark_price": "67340.25",
                "oracle_price": "67338.90",
                "change_24h": "1234.50",
                "change_percent_24h": "1.87",
                "high_24h": "68000.00",
                "low_24h": "65000.00",
                "volume_24h": "12345.678",
                "quote_volume_24h": "831000000.12",
                "funding_rate": "0.0001",
                "open_interest": "20000.50",
                "source": "HYPERLIQUID_INFO_API",
                "environment": "MAINNET",
                "as_of": "2026-08-08T00:00:00Z",
                "stale": False,
            }
        ],
        "meta": {
            "source": "HYPERLIQUID",
            "environment": "MAINNET",
            "as_of": "2026-08-08T00:00:00Z",
            "stale": False,
            "next_cursor": None,
        },
        "request_id": "core_req_function",
    }


class RecordingVoiceProvider(FakeVoiceProvider):
    def __init__(self, *, fail_update: bool = False) -> None:
        self.results: list[tuple[str, dict[str, Any], int]] = []
        self.progress_messages: list[tuple[str, int]] = []
        self.fail_update = fail_update

    async def send_function_result(
        self,
        session_id: str,
        user_id: str,
        tool_call_id: str,
        content: dict[str, Any],
        attempt: int = 0,
    ) -> VoiceTaskAcceptance:
        if self.fail_update:
            raise VolcOpenAPIError("UPDATE_FAILED: rejected")
        self.results.append((tool_call_id, content, attempt))
        return VoiceTaskAcceptance(provider_request_id="volc_update_req")

    async def send_progress_message(
        self,
        session_id: str,
        user_id: str,
        message: str,
        attempt: int = 0,
    ) -> VoiceTaskAcceptance:
        self.progress_messages.append((message, attempt))
        return VoiceTaskAcceptance(provider_request_id="volc_progress_req")


class SlowExecutor:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, *args: object, **kwargs: object) -> dict[str, Any]:
        self.calls += 1
        await asyncio.sleep(0.01)
        return {"status": "ok", "request_id": "slow_tool_req"}


class IdempotencyRecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str]] = []

    async def execute(
        self,
        tool_name: str,
        user_id: str,
        arguments: dict[str, Any],
        allowed_tools: list[str],
        *,
        idempotency_key: str | None = None,
        intent_source: str = "TEXT",
    ) -> dict[str, Any]:
        self.calls.append((tool_name, idempotency_key, intent_source))
        return {
            "status": "draft_created",
            "trade_draft_id": "draft_same_intent",
            "execution_requested": False,
        }


async def _client(
    handler,
    *,
    scope_tools: tuple[str, ...] = ("get_market_price",),
    fail_update: bool = False,
) -> tuple[httpx.AsyncClient, Container, RecordingVoiceProvider, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    core_http = httpx.AsyncClient(transport=httpx.MockTransport(capture))
    container = build_container(
        Settings(
            redis_url="",
            volc_function_callback_secret=FUNCTION_SECRET,
        )
    )
    voice = RecordingVoiceProvider(fail_update=fail_update)
    core = HttpCoreClient(
        "https://core.example",
        core_http,
        actor_token="agent-token-never-log",
        max_retries=0,
    )
    container.voice_provider = voice
    container.core_client = core
    container.executor = ToolExecutor(container.registry, core)
    container._http_clients.append(core_http)
    now = datetime.now(UTC)
    await container.session_store.create(
        Session(
            session_id="sess_function",
            user_id="canonical-user",
            modality="voice",
            scope=Scope(agents=("avatar",), tools=scope_tools),
            state="voice_ready",
            created_at=now,
            last_activity_at=now,
            voice_trace_id="voice_trace",
            voice_app_id="123456789012345678901234",
            voice_room_id="room_function",
            voice_user_id="mobile_function",
            voice_task_id="task_function_0",
            voice_bot_user_id="bot_function",
        )
    )
    app = FastAPI()
    app.state.container = container
    app.include_router(router, prefix="/v1/agent")
    return (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"),
        container,
        voice,
        requests,
    )


def _event(**overrides: Any) -> dict[str, Any]:
    return {
        "signature": FUNCTION_SECRET,
        "AppId": "123456789012345678901234",
        "RoomId": "room_function",
        "TaskId": "task_function_0",
        "tool_calls": [
            {
                "id": "call_market_1",
                "type": "function",
                "function": {
                    "name": "get_market_price",
                    "arguments": json.dumps({"symbol": "BTC"}),
                },
            }
        ],
    } | overrides


def _official_event(*, event_type: str = "tool_calls", **overrides: Any) -> dict[str, Any]:
    message: object
    if event_type == "tool_calls":
        message = _event()["tool_calls"]
    else:
        message = {
            "event_type": "function_calling",
            "function": "get_market_price",
            "tool_call_id": "call_market_1",
            "response_id": "response_1",
        }
    return {
        "Message": json.dumps(message, separators=(",", ":")),
        "Signature": FUNCTION_SECRET,
        "Type": event_type,
        "RoomID": "room_function",
        "TaskID": "function_calling_task_73bc",
        "TaskType": "voiceChat",
        "AppId": "123456789012345678901234",
    } | overrides


async def test_function_callback_executes_core_ticker_and_updates_voice_chat() -> None:
    client, container, voice, requests = await _client(
        lambda _: httpx.Response(200, json=_ticker())
    )
    async with client:
        response = await client.post(
            "/v1/agent/volc-function-callback?session_id=sess_function", json=_event()
        )

    assert response.status_code == 200
    assert len(requests) == 1
    assert requests[0].url.path == "/internal/v1/market-data/tickers"
    assert "user_id" not in requests[0].url.params
    assert voice.results == [
        (
            "call_market_1",
            {
                "symbol": "BTC",
                "market_type": "PERP",
                "last_price": "67345.10",
                "mark_price": "67340.25",
                "change_percent_24h": "1.87",
                "source": "hyperliquid",
                "environment": "mainnet",
                "as_of": "2026-08-08T00:00:00Z",
                "stale": False,
                "request_id": "core_req_function",
                "data_usage": "market data only; not an order, fill, balance, or settlement result",
            },
            0,
        )
    ]
    records = await container.audit_store.list_by_session("sess_function")
    session = await container.session_store.get("sess_function")
    assert len(records) == 1
    assert records[0].status == "ok"
    assert records[0].result_ref == "core_req_function"
    assert session is not None
    assert session.voice_function_call_started_at is not None
    assert session.voice_function_call_completed_at is not None
    assert session.voice_function_call_failed_at is None


async def test_function_callback_duplicate_is_idempotent() -> None:
    client, _, voice, requests = await _client(lambda _: httpx.Response(200, json=_ticker()))
    async with client:
        first, second = await asyncio.gather(
            client.post("/v1/agent/volc-function-callback?session_id=sess_function", json=_event()),
            client.post("/v1/agent/volc-function-callback?session_id=sess_function", json=_event()),
        )
    assert first.status_code == second.status_code == 200
    assert len(requests) == 1
    assert len(voice.results) == 1


@pytest.mark.parametrize(
    ("override", "status"),
    [
        ({"signature": "wrong"}, 401),
        ({"AppId": "wrong"}, 400),
        ({"RoomId": "wrong"}, 400),
    ],
)
async def test_function_callback_rejects_signature_and_identity(
    override: dict[str, Any], status: int
) -> None:
    client, _, voice, requests = await _client(lambda _: httpx.Response(200, json=_ticker()))
    async with client:
        response = await client.post(
            "/v1/agent/volc-function-callback?session_id=sess_function",
            json=_event(**override),
        )
    assert response.status_code == status
    assert not requests
    assert not voice.results


async def test_official_function_task_id_is_not_voice_chat_task_id() -> None:
    client, _, voice, requests = await _client(lambda _: httpx.Response(200, json=_ticker()))
    async with client:
        response = await client.post(
            "/v1/agent/volc-function-callback?session_id=sess_function",
            json=_event(TaskId="function_calling_task_73bc"),
        )
    assert response.status_code == 200
    assert len(requests) == 1
    assert len(voice.results) == 1


async def test_official_function_callback_message_is_parsed_without_content_type() -> None:
    client, _, voice, requests = await _client(lambda _: httpx.Response(200, json=_ticker()))
    async with client:
        response = await client.post(
            "/v1/agent/volc-function-callback?session_id=sess_function",
            content=json.dumps(_official_event()).encode(),
        )
    assert response.status_code == 200
    assert len(requests) == 1
    assert len(voice.results) == 1


async def test_function_information_and_stopped_session_are_acknowledged_without_execution() -> (
    None
):
    client, container, voice, requests = await _client(
        lambda _: httpx.Response(200, json=_ticker())
    )
    async with client:
        first = await client.post(
            "/v1/agent/volc-function-callback?session_id=sess_function",
            json=_official_event(event_type="information"),
        )
        duplicate = await client.post(
            "/v1/agent/volc-function-callback?session_id=sess_function",
            json=_official_event(event_type="information"),
        )
        session = await container.session_store.get("sess_function")
        assert session is not None
        await container.session_store.update(replace(session, state="voice_stopped"))
        delayed = await client.post(
            "/v1/agent/volc-function-callback?session_id=sess_function",
            json=_official_event(),
        )
    assert first.status_code == duplicate.status_code == delayed.status_code == 200
    assert not requests
    assert not voice.results
    assert container.voice_callback_metrics.callback_duplicate_total == 1
    assert container.voice_callback_metrics.callback_stale_total == 1


async def test_slow_tool_gets_one_neutral_progress_message_before_result() -> None:
    client, container, voice, requests = await _client(
        lambda _: httpx.Response(200, json=_ticker())
    )
    executor = SlowExecutor()
    container.executor = executor  # type: ignore[assignment]
    container.settings.volc_function_progress_delay_seconds = 0
    async with client:
        response = await client.post(
            "/v1/agent/volc-function-callback?session_id=sess_function",
            json=_official_event(),
        )
    assert response.status_code == 200
    assert executor.calls == 1
    assert voice.progress_messages == [("正在查询，请稍候。", 0)]
    assert len(voice.results) == 1
    assert not requests


async def test_same_voice_draft_intent_reuses_idempotency_context_without_execution() -> None:
    client, container, voice, requests = await _client(
        lambda _: httpx.Response(200, json=_ticker()),
        scope_tools=("create_perp_trade_draft",),
    )
    executor = IdempotencyRecordingExecutor()
    container.executor = executor  # type: ignore[assignment]
    arguments = {
        "symbol": "BTC",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": "0.00015",
        "limit_price": "67000",
        "leverage": 1,
    }

    def draft_event(call_id: str) -> dict[str, Any]:
        event = _official_event()
        event["Message"] = json.dumps(
            [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "create_perp_trade_draft",
                        "arguments": json.dumps(arguments, sort_keys=True),
                    },
                }
            ],
            separators=(",", ":"),
        )
        return event

    async with client:
        first = await client.post(
            "/v1/agent/volc-function-callback?session_id=sess_function",
            json=draft_event("call_draft_1"),
        )
        retry = await client.post(
            "/v1/agent/volc-function-callback?session_id=sess_function",
            json=draft_event("call_draft_2"),
        )
    assert first.status_code == retry.status_code == 200
    assert [name for name, _, _ in executor.calls] == [
        "create_perp_trade_draft",
        "create_perp_trade_draft",
    ]
    assert executor.calls[0][1] == executor.calls[1][1]
    assert executor.calls[0][1] and executor.calls[0][1].startswith("idem_voice_")
    assert executor.calls[0][2] == "VOICE"
    assert all(result[1]["execution_requested"] is False for result in voice.results)
    assert not requests


async def test_previous_attempt_function_signature_cannot_execute_tool() -> None:
    client, container, voice, requests = await _client(
        lambda _: httpx.Response(200, json=_ticker())
    )
    session = await container.session_store.get("sess_function")
    assert session is not None
    old_signature = derive_callback_signature(
        FUNCTION_SECRET,
        purpose="function",
        session_id=session.session_id,
        task_id=session.voice_task_id or "",
    )
    await container.session_store.update(
        replace(
            session,
            voice_callback_auth_version=CALLBACK_AUTH_VERSION,
            voice_previous_task_ids=(session.voice_task_id or "",),
            voice_task_id="task_function_1",
            voice_attempt=1,
        )
    )
    async with client:
        response = await client.post(
            "/v1/agent/volc-function-callback?session_id=sess_function",
            json=_official_event(Signature=old_signature),
        )
    assert response.status_code == 200
    assert not requests
    assert not voice.results
    assert container.voice_callback_metrics.callback_stale_total == 1


async def test_function_callback_rejects_out_of_scope_tool_and_invalid_json() -> None:
    client, container, voice, requests = await _client(
        lambda _: httpx.Response(200, json=_ticker()), scope_tools=()
    )
    async with client:
        forbidden = await client.post(
            "/v1/agent/volc-function-callback?session_id=sess_function", json=_event()
        )
    assert forbidden.status_code == 403
    assert not requests
    assert not voice.results
    forbidden_session = await container.session_store.get("sess_function")
    assert forbidden_session is not None
    assert forbidden_session.voice_function_call_started_at is not None
    assert forbidden_session.voice_function_call_failed_at is not None

    client, _, _, requests = await _client(lambda _: httpx.Response(200, json=_ticker()))
    event = _event()
    event["tool_calls"][0]["function"]["arguments"] = "{bad-json"
    async with client:
        invalid = await client.post(
            "/v1/agent/volc-function-callback?session_id=sess_function", json=event
        )
    assert invalid.status_code == 400
    assert not requests


async def test_core_failure_returns_structured_tool_error_not_fake_price() -> None:
    client, container, voice, _ = await _client(
        lambda _: httpx.Response(
            503,
            json={
                "error": {"code": "MARKET_DATA_UNAVAILABLE", "retryable": True},
                "request_id": "core_unavailable_req",
            },
        )
    )
    async with client:
        response = await client.post(
            "/v1/agent/volc-function-callback?session_id=sess_function", json=_event()
        )
    assert response.status_code == 200
    assert voice.results[0][1] == {
        "status": "error",
        "error_code": "INTERNAL",
        "message": "行情服务暂不可用",
        "upstream_code": "MARKET_DATA_UNAVAILABLE",
        "request_id": "core_unavailable_req",
        "retryable": True,
        "credential_refresh_required": False,
    }
    assert "mark_price" not in voice.results[0][1]
    records = await container.audit_store.list_by_session("sess_function")
    assert records[0].status == "tool_error"


async def test_expired_core_credential_gives_voice_a_safe_degraded_result() -> None:
    leaked = "raw-core-body-secret"
    client, container, voice, _ = await _client(
        lambda _: httpx.Response(
            401,
            json={
                "error": {"code": "UNAUTHENTICATED", "message": leaked},
                "request_id": "core_credential_req",
            },
        )
    )
    async with client:
        response = await client.post(
            "/v1/agent/volc-function-callback?session_id=sess_function", json=_event()
        )
    assert response.status_code == 200
    result = voice.results[0][1]
    assert result == {
        "status": "error",
        "error_code": "INTERNAL",
        "message": "智能体服务凭证暂不可用",
        "upstream_code": "UNAUTHENTICATED",
        "request_id": "core_credential_req",
        "retryable": False,
        "credential_refresh_required": True,
    }
    assert leaked not in json.dumps(result)
    assert container.core_credential_readiness.snapshot.status == "core_rejected"


async def test_update_failure_is_not_marked_delivered_and_secrets_are_not_logged(
    caplog,
) -> None:
    client, container, _, _ = await _client(
        lambda _: httpx.Response(200, json=_ticker()), fail_update=True
    )
    with caplog.at_level(logging.INFO, logger="app.api.agent"):
        async with client:
            response = await client.post(
                "/v1/agent/volc-function-callback?session_id=sess_function", json=_event()
            )
    assert response.status_code == 502
    session = await container.session_store.get("sess_function")
    assert session is not None
    assert "call_market_1" not in session.voice_tool_delivered
    assert FUNCTION_SECRET not in caplog.text
    assert "agent-token-never-log" not in caplog.text
    records = await container.audit_store.list_by_session("sess_function")
    assert records[0].status == "update_failed"
