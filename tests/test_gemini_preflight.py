"""Gemini Live v1beta startup preflight and redaction."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.voice.gemini_live import (
    GEMINI_LIVE_MODEL,
    GEMINI_LIVE_VOICE_NAME,
    GeminiLiveVoiceProvider,
)
from app.voice.gemini_preflight import GeminiLiveConnectivityPreflight

_TOKEN = "auth_tokens/preflight-token-must-never-be-logged"
_KEY = "gemini-key-must-never-be-logged"


def _provider(client: httpx.AsyncClient) -> GeminiLiveVoiceProvider:
    return GeminiLiveVoiceProvider(
        _KEY,
        client,
        instructions="必须使用简体中文回答。",
        model=GEMINI_LIVE_MODEL,
    )


def _mint_transport(status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if status == 200:
            return httpx.Response(200, json={"name": _TOKEN})
        return httpx.Response(status, json={"error": {"message": "redacted"}})

    return httpx.MockTransport(handler)


class _WebSocket:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.sent: list[dict[str, Any]] = []

    async def send(self, value: str) -> None:
        self.sent.append(json.loads(value))

    async def recv(self) -> str:
        return json.dumps(self.response)


class _Connection:
    def __init__(self, websocket: _WebSocket) -> None:
        self.websocket = websocket

    async def __aenter__(self) -> _WebSocket:
        return self.websocket

    async def __aexit__(self, *args: object) -> None:
        return None


class _Connector:
    def __init__(self, response: dict[str, Any]) -> None:
        self.websocket = _WebSocket(response)
        self.calls = 0

    def __call__(self, url: str, **kwargs: object) -> _Connection:
        assert url.startswith("wss://generativelanguage.googleapis.com/ws/")
        assert "v1beta.GenerativeService.BidiGenerateContentConstrained" in url
        assert parse_qs(urlsplit(url).query) == {"access_token": [_TOKEN]}
        self.calls += 1
        return _Connection(self.websocket)


async def test_preflight_mints_one_token_and_requires_setup_complete(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connector = _Connector({"setupComplete": {}})
    async with httpx.AsyncClient(transport=_mint_transport()) as client:
        preflight = GeminiLiveConnectivityPreflight(
            provider=_provider(client),
            api_key_configured=True,
            model=GEMINI_LIVE_MODEL,
            connector=connector,
        )
        with caplog.at_level(logging.INFO):
            snapshot = await preflight.check()

    assert snapshot.status == "valid"
    assert snapshot.connectivity_verified is True
    assert connector.calls == 1
    assert connector.websocket.sent == [
        {
            "setup": {
                "model": f"models/{GEMINI_LIVE_MODEL}",
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {"voiceName": GEMINI_LIVE_VOICE_NAME}
                        }
                    },
                },
            }
        }
    ]
    rendered = json.dumps(snapshot.public_dict(), default=str) + caplog.text
    assert _TOKEN not in rendered
    assert _KEY not in rendered
    assert "access_token=" not in rendered


async def test_preflight_fails_closed_without_setup_complete() -> None:
    connector = _Connector({"serverContent": {}})
    async with httpx.AsyncClient(transport=_mint_transport()) as client:
        preflight = GeminiLiveConnectivityPreflight(
            provider=_provider(client),
            api_key_configured=True,
            model=GEMINI_LIVE_MODEL,
            connector=connector,
        )
        snapshot = await preflight.check()

    assert snapshot.status == "contract_rejected"
    assert snapshot.connectivity_verified is False
    assert snapshot.detail == "setupComplete was not received"


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "credential_rejected"), (409, "upstream_unavailable"), (429, "upstream_unavailable"), (503, "upstream_unavailable")],
)
async def test_preflight_maps_mint_failures_without_response_body(
    status: int, expected: str
) -> None:
    async with httpx.AsyncClient(transport=_mint_transport(status)) as client:
        preflight = GeminiLiveConnectivityPreflight(
            provider=_provider(client),
            api_key_configured=True,
            model=GEMINI_LIVE_MODEL,
            connector=_Connector({"setupComplete": {}}),
        )
        snapshot = await preflight.check()

    assert snapshot.status == expected
    assert "redacted" not in snapshot.detail


@pytest.mark.live
async def test_live_single_use_token_reaches_v1beta_setup_complete() -> None:
    """Deployment/QA opt-in proof; never prints or returns the token."""

    api_key = os.getenv("AGENT_GEMINI_API_KEY", "").strip()
    if not api_key:
        pytest.skip("AGENT_GEMINI_API_KEY is not configured")
    async with httpx.AsyncClient(timeout=20.0) as client:
        preflight = GeminiLiveConnectivityPreflight(
            provider=GeminiLiveVoiceProvider(
                api_key,
                client,
                instructions="必须使用简体中文回答。",
                model=GEMINI_LIVE_MODEL,
            ),
            api_key_configured=True,
            model=GEMINI_LIVE_MODEL,
        )
        snapshot = await preflight.check()

    assert snapshot.status == "valid"
    assert snapshot.connectivity_verified is True
    assert snapshot.last_verified_at is not None
    assert snapshot.last_verified_at <= datetime.now(UTC)
