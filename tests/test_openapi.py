"""验证 FastAPI 自动生成的 OpenAPI（/docs 源）含完整控制面模型。"""

from __future__ import annotations

import httpx
import pytest

from app.main import app

_EXPECTED_SCHEMAS = {
    "SessionCreateRequest",
    "SessionCredentials",
    "VoiceCredentials",
    "AgentScope",
    "ToolCallRequest",
    "ToolCallResult",
    "SessionState",
    "ErrorEnvelope",
    "HistoryPage",
    "TradeDraftCardRequest",
    "TradeDraftCard",
    "VoiceToolExecuteRequest",
    "VoiceToolExecuteResult",
    "VoiceSessionEndRequest",
    "VoiceSessionEndResult",
}

_EXPECTED_PATHS = {
    "/v1/agent/sessions",
    "/v1/agent/sessions/{session_id}",
    "/v1/agent/sessions/{session_id}/token",
    "/v1/agent/sessions/{session_id}/voice/start",
    "/v1/agent/sessions/{session_id}/tool-calls",
    "/v1/agent/messages",
    "/v1/agent/trade-draft",
    "/v1/agent/sessions/{session_id}/history",
    "/api/voice/tools/execute",
    "/api/voice/session/end",
}


@pytest.mark.asyncio
async def test_openapi_exposes_control_plane() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()

    schemas = set(spec["components"]["schemas"])
    assert _EXPECTED_SCHEMAS <= schemas, f"缺少模型: {_EXPECTED_SCHEMAS - schemas}"

    paths = set(spec["paths"])
    assert _EXPECTED_PATHS <= paths, f"缺少端点: {_EXPECTED_PATHS - paths}"

    draft_schema = spec["components"]["schemas"]["TradeDraftCardRequest"]
    assert set(draft_schema["required"]) == {"session_id", "strategy_id", "text"}
    assert "unified_account_id" not in draft_schema["properties"]
    assert "venue_account_id" not in draft_schema["properties"]
    assert "mandate_id" not in draft_schema["properties"]

    voice_schema = spec["components"]["schemas"]["VoiceCredentials"]
    provider_enum = set(voice_schema["properties"]["provider"]["enum"])
    assert "gemini_live" in provider_enum
    assert {
        "endpoint_url",
        "expires_at",
        "client_secret",
        "client_secret_expires_at",
        "new_session_expires_at",
        "model",
        "allowed_tools",
    } <= set(voice_schema["properties"])


@pytest.mark.asyncio
async def test_docs_page_served() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/docs")
    assert resp.status_code == 200
    assert "swagger" in resp.text.lower()


@pytest.mark.asyncio
async def test_token_endpoint_declares_the_provider_switch_conflict() -> None:
    """移动端按契约做降级重试，409 必须写进 OpenAPI，不能只在代码里存在。"""

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/openapi.json")
    responses = resp.json()["paths"]["/v1/agent/sessions/{session_id}/token"]["post"]["responses"]
    assert "409" in responses, "会话有活跃 provider 任务时禁止切换渠道，须声明 409"
    assert "503" in responses, "备用渠道装配不出来时返回 503，须声明"
