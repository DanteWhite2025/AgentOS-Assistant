from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app.api.agent import router as agent_router
from app.api.errors import _map_upstream_error, market_resolution_tool_error
from app.api.ws import router as ws_router
from app.config import Settings
from app.container import Container, build_container
from app.tools.agentos.trade_flow import MarketInstrumentResolutionError
from app.tools.core_client import MarketDataError
from app.tools.executor import ToolExecutor

USER = {"Authorization": "Bearer test-user:u1"}
LEAKS = (
    "expired.jwt.value",
    "Authorization",
    "Bearer",
    "raw-core-body-secret",
    "Traceback",
)


class FailingRunner:
    async def run_turn(
        self,
        session,
        user_text: str,
        trace_id: str,
        now: datetime,
        *,
        user_bearer_token: str | None = None,
        idempotency_key: str | None = None,
        on_tool_start=None,
        on_tool_end=None,
        web_search: bool = False,
    ):
        raise MarketDataError(
            "UNAUTHENTICATED",
            "raw-core-body-secret expired.jwt.value",
            401,
            "req_core_401",
            credential_refresh_required=True,
        )

    async def run_turn_stream(self, *args, **kwargs):
        await self.run_turn(*args, **kwargs)
        yield  # pragma: no cover - run_turn 一定先抛


class MarketResolutionFailingRunner:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    async def run_turn(self, *args, **kwargs):
        reason = {
            400: "MARKET_INSTRUMENT_NOT_FOUND",
            403: "MARKET_INSTRUMENT_NOT_TRADABLE",
            503: "MARKET_CATALOG_STALE",
        }[self.status_code]
        raise MarketInstrumentResolutionError(reason, "安全的行情标的错误", self.status_code)

    async def run_turn_stream(self, *args, **kwargs):
        await self.run_turn(*args, **kwargs)
        yield  # pragma: no cover - run_turn 一定先抛


class FailingCore:
    def __init__(self) -> None:
        self.draft_calls = 0

    async def get_market_tickers(self, symbols: list[str], request_id: str | None = None):
        raise MarketDataError(
            "UNAUTHENTICATED",
            "raw-core-body-secret expired.jwt.value",
            401,
            "req_core_401",
            credential_refresh_required=True,
        )

    async def create_trade_draft(self, user_id: str, args: dict[str, Any]):
        self.draft_calls += 1
        raise AssertionError("a credential failure must not create a draft")

    async def get_resource(self, resource: str, user_id: str, params: dict[str, Any]):
        raise AssertionError("unexpected resource call")


def _app() -> tuple[FastAPI, Container]:
    container = build_container(Settings(redis_url=""))
    app = FastAPI()
    app.state.container = container
    app.include_router(agent_router, prefix="/v1/agent")
    app.include_router(ws_router)
    return app, container


async def _session(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/v1/agent/sessions", json={"modality": "text"}, headers=USER
    )
    assert response.status_code == 201
    return response.json()


async def test_messages_maps_expired_runtime_credential_to_safe_503() -> None:
    app, container = _app()
    container.runner = FailingRunner()  # type: ignore[assignment]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session = await _session(client)
        response = await client.post(
            "/v1/agent/messages",
            json={"session_id": session["session_id"], "text": "只查询 BTC 行情"},
            headers=USER,
        )
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "INTERNAL"
    assert body["message"] == "智能体服务凭证暂不可用"
    assert body["details"] == {
        "upstream_code": "UNAUTHENTICATED",
        "request_id": "req_core_401",
        "retryable": False,
        "credential_refresh_required": True,
    }
    assert container.core_credential_readiness.snapshot.status == "core_rejected"
    assert all(leak not in response.text for leak in LEAKS)


@pytest.mark.parametrize(
    ("status_code", "runtime_code", "retryable"),
    [(400, "INVALID_REQUEST", False), (403, "FORBIDDEN", False), (503, "INTERNAL", True)],
)
async def test_messages_classifies_market_resolution_errors(
    status_code: int, runtime_code: str, retryable: bool
) -> None:
    app, container = _app()
    container.runner = MarketResolutionFailingRunner(status_code)  # type: ignore[assignment]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session = await _session(client)
        response = await client.post(
            "/v1/agent/messages",
            json={"session_id": session["session_id"], "text": "BTCUSDC 行情"},
            headers=USER,
        )
    assert response.status_code == status_code
    body = response.json()
    assert body["code"] == runtime_code
    assert body["message"] == "安全的行情标的错误"
    assert body["details"]["retryable"] is retryable
    assert body["details"]["credential_refresh_required"] is False
    assert all(leak not in response.text for leak in LEAKS)


def test_voice_market_resolution_error_uses_same_safe_semantics() -> None:
    error = MarketInstrumentResolutionError(
        "MARKET_INSTRUMENT_AMBIGUOUS", "请明确行情标的", 400
    )
    assert market_resolution_tool_error(error) == {
        "status": "error",
        "error_code": "INVALID_REQUEST",
        "message": "请明确行情标的",
        "reason_code": "MARKET_INSTRUMENT_AMBIGUOUS",
        "retryable": False,
        "credential_refresh_required": False,
    }


async def test_relay_uses_same_mapping_and_never_creates_draft() -> None:
    app, container = _app()
    core = FailingCore()
    container.executor = ToolExecutor(container.registry, core)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session = await _session(client)
        response = await client.post(
            f"/v1/agent/sessions/{session['session_id']}/tool-calls",
            json={
                "call_id": "call_market",
                "tool_name": "market_tickers",
                "arguments": {"symbols": ["BTC"]},
            },
            headers={"Authorization": f"Bearer {session['rt_token']}"},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "INTERNAL"
    assert core.draft_calls == 0
    assert all(leak not in response.text for leak in LEAKS)


def test_websocket_sends_safe_error_and_keeps_authenticated_connection_open() -> None:
    app, container = _app()
    container.runner = FailingRunner()  # type: ignore[assignment]
    with TestClient(app) as client:
        session = client.post(
            "/v1/agent/sessions", json={"modality": "text"}, headers=USER
        ).json()
        with client.websocket_connect(
            f"/v1/agent/stream?session_id={session['session_id']}&token={session['rt_token']}"
        ) as ws:
            assert ws.receive_json()["type"] == "session.state"
            ws.send_json({"type": "user.message", "data": {"text": "BTC 行情"}})
            assert ws.receive_json()["type"] == "response.started"
            assert ws.receive_json()["type"] == "avatar.event"
            error = ws.receive_json()
            idle = ws.receive_json()
            assert error["type"] == "error"
            assert error["data"]["code"] == "INTERNAL"
            assert error["data"]["details"]["credential_refresh_required"] is True
            assert idle["data"] == {"kind": "state", "value": "Idle"}
            assert all(leak not in str(error) for leak in LEAKS)

            # Connection behavior is deterministic: operational credential
            # failures do not sign the USER out or close the rt_token socket.
            ws.send_json({"type": "ping", "data": {}})
            assert ws.receive_json()["type"] == "pong"


def test_core_timeout_and_network_failure_have_distinct_safe_statuses() -> None:
    request = httpx.Request("GET", "https://core.example/internal/v1/strategy-bindings")
    timeout = _map_upstream_error(httpx.ReadTimeout("sensitive timeout", request=request))
    unavailable = _map_upstream_error(
        httpx.ConnectError("sensitive network detail", request=request)
    )
    assert timeout.status_code == 504
    assert json_body(timeout)["code"] == "TOOL_UPSTREAM_TIMEOUT"
    assert unavailable.status_code == 503
    assert json_body(unavailable)["code"] == "INTERNAL"
    assert b"sensitive" not in timeout.body + unavailable.body


def test_core_http_statuses_keep_stable_distinct_runtime_semantics() -> None:
    expected = {
        400: (400, "INVALID_REQUEST"),
        404: (404, "INVALID_REQUEST"),
        409: (409, "POLICY_VIOLATION"),
        422: (422, "INVALID_REQUEST"),
        429: (429, "RATE_LIMITED"),
        500: (502, "INTERNAL"),
        503: (503, "INTERNAL"),
    }
    for upstream_status, (runtime_status, code) in expected.items():
        request = httpx.Request("GET", "https://core.example/internal/v1/account-summary")
        response = httpx.Response(upstream_status, request=request, json={"private": "secret"})
        mapped = _map_upstream_error(
            httpx.HTTPStatusError("sensitive", request=request, response=response)
        )
        assert mapped.status_code == runtime_status
        assert json_body(mapped)["code"] == code
        assert b"secret" not in mapped.body


class RecordingObserver:
    def __init__(self) -> None:
        self.http_errors: list[tuple[int, str, str | None]] = []

    def observe_market_data_error(self, exc: MarketDataError) -> None:
        raise AssertionError("HTTP error mapping must not use the market observer path")

    def observe_core_http_error(
        self, *, status_code: int, upstream_code: str, request_id: str | None
    ) -> None:
        self.http_errors.append((status_code, upstream_code, request_id))


def _core_status_error(status: int, code: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://core.example/internal/v1/trade-drafts")
    response = httpx.Response(
        status,
        request=request,
        json={
            "error": {
                "code": code,
                "message": "raw-core-body-secret",
                "retryable": False,
            },
            "request_id": "req_draft_rejected",
        },
    )
    return httpx.HTTPStatusError("sensitive", request=request, response=response)


def test_business_forbidden_does_not_invalidate_global_core_credential() -> None:
    observer = RecordingObserver()
    mapped = _map_upstream_error(
        _core_status_error(403, "FORBIDDEN"), observer=observer
    )
    body = json_body(mapped)

    assert mapped.status_code == 403
    assert body["code"] == "FORBIDDEN"
    assert body["message"] == "当前智能体无权执行此操作"
    assert body["details"] == {
        "upstream_code": "FORBIDDEN",
        "request_id": "req_draft_rejected",
        "retryable": False,
        "credential_refresh_required": False,
    }
    assert observer.http_errors == []
    assert all(leak not in bytes(mapped.body).decode() for leak in LEAKS)


def test_mandate_business_403_has_stable_policy_mapping() -> None:
    observer = RecordingObserver()
    mapped = _map_upstream_error(
        _core_status_error(403, "MANDATE_LIMIT_EXCEEDED"), observer=observer
    )
    body = json_body(mapped)

    assert mapped.status_code == 409
    assert body["code"] == "POLICY_VIOLATION"
    assert body["message"] == "当前交易计划超过允许额度"
    assert body["details"]["upstream_code"] == "MANDATE_LIMIT_EXCEEDED"
    assert body["details"]["credential_refresh_required"] is False
    assert observer.http_errors == []


def test_credential_403_scope_failure_still_invalidates_readiness() -> None:
    observer = RecordingObserver()
    mapped = _map_upstream_error(
        _core_status_error(403, "INSUFFICIENT_SCOPE"), observer=observer
    )

    assert mapped.status_code == 503
    assert json_body(mapped)["code"] == "INTERNAL"
    assert observer.http_errors == [(403, "INSUFFICIENT_SCOPE", "req_draft_rejected")]


def json_body(response) -> dict[str, Any]:
    import json

    return json.loads(response.body)


class ExplodingRunner:
    """模拟 LLM 网关故障：抛一个不在 /messages 已知异常清单里的异常。"""

    async def run_turn(
        self,
        session,
        user_text: str,
        trace_id: str,
        now: datetime,
        *,
        user_bearer_token: str | None = None,
        idempotency_key: str | None = None,
        on_tool_start=None,
        on_tool_end=None,
        web_search: bool = False,
    ):
        raise RuntimeError(
            "connection to https://llm-gateway.internal/v1/chat failed: AGENT_TOKEN rejected"
        )

    async def run_turn_stream(self, *args, **kwargs):
        await self.run_turn(*args, **kwargs)
        yield  # pragma: no cover - run_turn 一定先抛


async def test_messages_returns_error_envelope_when_llm_gateway_explodes() -> None:
    """回归：/messages 只捕获了几类已知异常，LLM/中转站故障会漏成 FastAPI 裸 500，
    既不走错误信封、也和 WS 路径的行为不一致（WS 会发 error 事件）。"""
    app, container = _app()
    container.runner = ExplodingRunner()  # type: ignore[assignment]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        session = await _session(client)
        response = await client.post(
            "/v1/agent/messages",
            json={"session_id": session["session_id"], "text": "随便聊聊"},
            headers=USER,
        )

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "INTERNAL"
    assert body["trace_id"]
    # 原始异常带着内部网关地址和凭证名，只能进日志。
    assert "llm-gateway.internal" not in response.text
    assert "AGENT_TOKEN" not in response.text
