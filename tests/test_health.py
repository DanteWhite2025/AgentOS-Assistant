"""健康检查与请求校验测试（in-process ASGI）。"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from app.api.health import readyz
from app.config import get_settings
from app.container import build_container
from app.main import app
from app.tools.core_client import MarketDataError
from app.voice.gemini_preflight import GeminiLivePreflightSnapshot


@pytest.mark.asyncio
async def test_healthz_ok() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "yesup-agent-runtime"
    assert body["meaning"] == "liveness_only"


@pytest.mark.asyncio
async def test_readyz_does_not_claim_voice_e2e() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["voice_e2e_verified"] is False
    assert resp.json()["voice_configuration"] == {
        "runtime_contract_valid": False,
        "provider": "demo_fake",
    }
    assert resp.json()["gemini_connectivity_verified"] is False


@pytest.mark.asyncio
async def test_gemini_readyz_requires_real_setup_complete_preflight() -> None:
    container = build_container(get_settings())
    original_mode = container.settings.provider_mode
    original_provider = container.settings.realtime_voice_provider
    original_key = container.settings.gemini_api_key
    try:
        container.settings.provider_mode = "real"
        container.settings.realtime_voice_provider = "gemini_live"
        container.settings.gemini_api_key = "configured-not-returned"
        container.core_credential_readiness._snapshot = replace(  # type: ignore[attr-defined]
            container.core_credential_readiness.snapshot, status="valid", ready=True
        )
        failed = GeminiLivePreflightSnapshot(
            status="contract_rejected",
            credentials_configured=True,
            contract_version="v1beta",
            model="gemini-3.1-flash-live-preview",
            connectivity_verified=False,
        )
        container.gemini_live_preflight = SimpleNamespace(snapshot=failed)  # type: ignore[assignment]
        response = await readyz(container)
        assert response.status_code == 503
        body = response.body.decode()
        assert '"gemini_contract_version":"v1beta"' in body
        assert '"gemini_connectivity_verified":false' in body
        assert "rtc_application_configured" not in body

        valid = replace(failed, status="valid", connectivity_verified=True)
        container.gemini_live_preflight = SimpleNamespace(snapshot=valid)  # type: ignore[assignment]
        response = await readyz(container)
        assert response.status_code == 200
        assert b'"gemini_connectivity_verified":true' in response.body
        assert b'"voice_e2e_verified":false' in response.body
    finally:
        container.settings.provider_mode = original_mode
        container.settings.realtime_voice_provider = original_provider
        container.settings.gemini_api_key = original_key
        await container.aclose()


@pytest.mark.asyncio
async def test_pipeline_readyz_reports_the_real_channel_not_demo_fake() -> None:
    """跑着流水线却报 demo_fake，会把运维往「配置没生效」方向带偏。

    voice 分支原先只有 volc_rtc / gemini_live 两段，pipeline 落进 else 就被
    当成自测桩了 —— 而顶层 realtime_voice_provider 字段是对的，两处自相矛盾
    比单纯少一个字段更难排查。
    """

    container = build_container(get_settings())
    original_mode = container.settings.provider_mode
    original_provider = container.settings.realtime_voice_provider
    original_key = container.settings.huochan_api_key
    try:
        container.settings.provider_mode = "real"
        container.settings.realtime_voice_provider = "pipeline"
        container.settings.huochan_api_key = "configured-not-returned"
        container.core_credential_readiness._snapshot = replace(  # type: ignore[attr-defined]
            container.core_credential_readiness.snapshot, status="valid", ready=True
        )

        response = await readyz(container)

        assert response.status_code == 200
        body = response.body.decode()
        assert '"provider":"pipeline"' in body
        assert "demo_fake" not in body
        assert '"runtime_contract_valid":true' in body
        # 音色和语速是听感投诉的直接抓手，排查时第一眼就要看到线上到底在用哪个。
        assert container.settings.huochan_voice_type in body
        assert '"tts_credentials_configured":true' in body
        # 凭证只报「配没配」，绝不回显。
        assert "configured-not-returned" not in body
        # 流水线不签票、没有 Bot 进房，那两组字段不该出现在这里。
        assert "rtc_application_configured" not in body
        assert "bot_presence_verified" not in body
    finally:
        container.settings.provider_mode = original_mode
        container.settings.realtime_voice_provider = original_provider
        container.settings.huochan_api_key = original_key
        await container.aclose()


@pytest.mark.asyncio
async def test_pipeline_readyz_flags_a_missing_tts_key() -> None:
    container = build_container(get_settings())
    original_mode = container.settings.provider_mode
    original_provider = container.settings.realtime_voice_provider
    original_key = container.settings.huochan_api_key
    try:
        container.settings.provider_mode = "real"
        container.settings.realtime_voice_provider = "pipeline"
        container.settings.huochan_api_key = ""
        container.core_credential_readiness._snapshot = replace(  # type: ignore[attr-defined]
            container.core_credential_readiness.snapshot, status="valid", ready=True
        )

        response = await readyz(container)

        assert '"tts_credentials_configured":false' in response.body.decode()
    finally:
        container.settings.provider_mode = original_mode
        container.settings.realtime_voice_provider = original_provider
        container.settings.huochan_api_key = original_key
        await container.aclose()


@pytest.mark.asyncio
async def test_pipeline_readyz_reports_gemini_as_an_independent_enabled_mode() -> None:
    container = build_container(get_settings())
    original_mode = container.settings.provider_mode
    original_provider = container.settings.realtime_voice_provider
    original_enabled = container.settings.enabled_voice_providers
    original_gemini_key = container.settings.gemini_api_key
    original_tts_key = container.settings.huochan_api_key
    original_pipeline = container.voice_pipeline
    original_preflight = container.gemini_live_preflight
    try:
        container.settings.provider_mode = "real"
        container.settings.realtime_voice_provider = "pipeline"
        container.settings.enabled_voice_providers = "pipeline,gemini_live"
        container.settings.gemini_api_key = "configured-not-returned"
        container.settings.huochan_api_key = "tts-configured-not-returned"
        container.voice_pipeline = object()  # type: ignore[assignment]
        container.core_credential_readiness._snapshot = replace(  # type: ignore[attr-defined]
            container.core_credential_readiness.snapshot, status="valid", ready=True
        )
        valid = GeminiLivePreflightSnapshot(
            status="valid",
            credentials_configured=True,
            contract_version="v1beta",
            model="gemini-3.1-flash-live-preview",
            connectivity_verified=True,
        )
        container.gemini_live_preflight = SimpleNamespace(snapshot=valid)  # type: ignore[assignment]

        response = await readyz(container)
        body = response.body.decode()

        assert response.status_code == 200
        assert '"default_voice_mode":"pipeline"' in body
        assert '"pipeline":{"enabled":true,"available":true' in body
        assert '"gemini_live":{"enabled":true,"available":true' in body
        assert '"gemini_credentials_configured":true' in body
        assert "configured-not-returned" not in body
        assert "tts-configured-not-returned" not in body
    finally:
        container.settings.provider_mode = original_mode
        container.settings.realtime_voice_provider = original_provider
        container.settings.enabled_voice_providers = original_enabled
        container.settings.gemini_api_key = original_gemini_key
        container.settings.huochan_api_key = original_tts_key
        container.voice_pipeline = original_pipeline
        container.gemini_live_preflight = original_preflight
        await container.aclose()


@pytest.mark.asyncio
async def test_readyz_is_503_when_cached_core_credential_is_invalid() -> None:
    container = getattr(app.state, "container", None) or build_container(get_settings())
    original_mode = container.settings.provider_mode
    try:
        container.settings.provider_mode = "real"
        container.core_credential_readiness.observe_market_data_error(
            MarketDataError(
                "UNAUTHENTICATED",
                "not returned",
                401,
                "req_ready_401",
                credential_refresh_required=True,
            )
        )
        response = await readyz(container)
    finally:
        container.settings.provider_mode = original_mode
    assert response.status_code == 503
    assert b'"status":"not_ready"' in response.body
    assert b'"status":"core_rejected"' in response.body
    assert b"not returned" not in response.body


@pytest.mark.asyncio
async def test_session_request_validated() -> None:
    # 缺 modality → Pydantic 模型生效，返回 422（证明 schema 已挂上）
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent/sessions",
            json={},
            headers={"Authorization": "Bearer test-user:u1"},
        )
    assert resp.status_code == 422
