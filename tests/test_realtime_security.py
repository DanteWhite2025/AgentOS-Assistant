"""Real Realtime configuration, credential issuance, and secret hygiene."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.container import build_container
from app.telemetry.logging import SecretRedactionFilter
from app.voice.config import (
    default_volc_voice_config,
    load_volc_voice_config,
    validate_volc_voice_config,
)
from app.voice.provider import FakeVoiceProvider, OpenAIVoiceProvider, VolcRtcVoiceProvider

REAL = {
    "provider_mode": "real",
    "env": "staging",
    "realtime_voice_provider": "openai",
    "openai_api_key": "sk-real-not-logged",
    "llm_base_url": "https://newapi.example.com/v1",
    "llm_api_key": "llm-gateway-not-logged",
    "llm_model": "deepseek-chat",
    "followin_api_key": "followin-key-for-test",
    "core_actor_token": "agent-staging-token",
    "core_base_url": "https://core-staging.example.com",
    "rt_ws_base": "wss://runtime-staging.example.com",
    "redis_url": "",
}

VOLC_REAL = REAL | {
    "realtime_voice_provider": "volc_rtc",
    "openai_api_key": "",
    "rtc_app_id": "123456789012345678901234",
    "rtc_app_key": "rtc-key",
    "volc_access_key_id": "ak-test",
    "volc_secret_access_key": "sk-test",
    "huochan_s2s_app_id": "speech-app-id",
    "huochan_api_key": "speech-token",
    "huochan_s2s_token": "s2s-token",
    "volc_callback_url": "https://runtime-staging.example.com/v1/agent/volc-callback",
    "volc_callback_secret": "callback-secret-at-least-32-characters",
    "volc_function_callback_url": (
        "https://runtime-staging.example.com/v1/agent/volc-function-callback"
    ),
    "volc_function_callback_secret": "function-secret-at-least-32-characters",
    "volc_ark_model": "doubao-seed-1-6",
    "volc_ark_endpoint_id": "ep-custom-test",
}


@pytest.mark.parametrize(
    "override",
    [
        {"openai_api_key": ""},
        {"llm_api_key": ""},
        # 缺 Followin Key 时新闻链路会静默返回模拟数据，real 模式必须启动即失败
        {"followin_api_key": ""},
        {"core_actor_token": ""},
        {"core_base_url": ""},
        {"rt_ws_base": ""},
        {"rt_ws_base": "ws://runtime-staging.example.com"},
        {"rt_ws_base": "wss://localhost:8100"},
        {"rt_ws_base": "wss://127.0.0.1:8100"},
    ],
)
def test_staging_real_mode_rejects_missing_or_non_public_configuration(
    override: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        Settings(**(REAL | override))


def test_staging_forbids_fake_voice_provider() -> None:
    with pytest.raises(ValidationError):
        Settings(provider_mode="fake", env="staging")


def test_core_configuration_prefers_new_names_and_supports_deprecated_aliases(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("AGENT_CORE_BASE_URL", "https://new-core.example")
    monkeypatch.setenv("AGENT_AGENTOS_STAGING_BASE_URL", "https://legacy-core.example")
    monkeypatch.setenv("AGENT_CORE_ACTOR_TOKEN", "new-agent-token")
    monkeypatch.setenv("AGENT_AGENTOS_STAGING_TOKEN", "legacy-agent-token")
    with caplog.at_level(logging.WARNING, logger="app.config"):
        settings = Settings(_env_file=None)
    assert settings.core_base_url == "https://new-core.example"
    assert settings.core_actor_token == "new-agent-token"
    assert settings.staging_base_url == settings.core_base_url
    assert "AGENT_AGENTOS_STAGING_TOKEN" in caplog.text
    assert "new-agent-token" not in caplog.text
    assert "legacy-agent-token" not in caplog.text


def test_legacy_core_token_names_are_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_CORE_ACTOR_TOKEN", raising=False)
    monkeypatch.setenv("AGENT_STAGING_TOKEN", "expired-legacy-token")
    monkeypatch.setenv("AGENT_AGENTOS_STAGING_TOKEN", "another-legacy-token")
    settings = Settings(_env_file=None)
    assert settings.core_actor_token == ""


@pytest.mark.parametrize(
    "override",
    [
        {"volc_callback_url": ""},
        {"huochan_s2s_token": ""},
        {"volc_function_callback_url": ""},
        {"volc_function_callback_secret": ""},
        {"volc_function_callback_secret": "too-short"},
        {"volc_ark_model": ""},
        {"volc_ark_endpoint_id": ""},
        {"volc_ark_endpoint_id": "ep-m-20260525131453-csn47"},
        {"volc_ark_endpoint_id": "not-an-endpoint"},
        {"volc_callback_secret": ""},
        {"volc_callback_secret": "too-short"},
        {"volc_callback_url": "http://runtime-staging.example.com/callback"},
        {"volc_callback_url": "https://localhost/callback"},
        {"volc_callback_url": "https://127.0.0.1/callback"},
        {"volc_callback_url": "https://runtime.example.com/callback?secret=x"},
    ],
)
def test_staging_real_volc_mode_rejects_unsafe_callback_configuration(
    override: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        Settings(**(VOLC_REAL | override))


PIPELINE_REAL = REAL | {
    "realtime_voice_provider": "pipeline",
    "openai_api_key": "",
    "huochan_api_key": "volc-tts-api-key",
}

DUAL_VOICE_REAL = PIPELINE_REAL | {
    "enabled_voice_providers": "pipeline,gemini_live",
    "gemini_api_key": "gemini-key-not-logged",
}


def test_pipeline_mode_only_requires_the_volc_tts_api_key() -> None:
    """流水线自己签不了票也不带 Bot，RTC/S2S/回调那一整套凭证与它无关。

    落到 volc_rtc 那条 else 分支会凭空要求十来个用不到的 Secret，部署时
    只能拿假值糊上去 —— 那比不校验更危险。
    """

    settings = Settings(**PIPELINE_REAL)
    assert settings.realtime_voice_provider == "pipeline"


def test_pipeline_mode_rejects_a_missing_volc_tts_api_key() -> None:
    with pytest.raises(ValidationError):
        Settings(**(PIPELINE_REAL | {"huochan_api_key": ""}))


def test_dual_voice_mode_requires_both_provider_credentials() -> None:
    settings = Settings(**DUAL_VOICE_REAL)
    assert settings.enabled_voice_provider_names == ("pipeline", "gemini_live")

    with pytest.raises(ValidationError):
        Settings(**(DUAL_VOICE_REAL | {"gemini_api_key": ""}))
    with pytest.raises(ValidationError):
        Settings(**(DUAL_VOICE_REAL | {"huochan_api_key": ""}))


def test_enabled_voice_providers_must_include_the_default_and_be_known() -> None:
    with pytest.raises(ValidationError):
        Settings(**(PIPELINE_REAL | {"enabled_voice_providers": "gemini_live"}))
    with pytest.raises(ValidationError):
        Settings(**(PIPELINE_REAL | {"enabled_voice_providers": "pipeline,unknown"}))


def test_volc_tts_defaults_match_the_approved_09_persona_voice() -> None:
    """09 甜美小源 + 1.15 语速是团队听感评审签收过的参数，默认值就得是它。"""

    settings = Settings(_env_file=None)
    assert settings.huochan_voice_type == "zh_female_tianmeixiaoyuan_uranus_bigtts"
    assert settings.huochan_speed_ratio == 1.15


def test_default_volc_s2s_config_satisfies_required_pipeline_contract() -> None:
    config = default_volc_voice_config(
        s2s_app_id="s2s-app-id",
        s2s_token="s2s-token-never-log",
    )
    validate_volc_voice_config(config)


def test_endpointing_window_overrides_stale_complete_voice_json() -> None:
    configured = default_volc_voice_config(
        s2s_app_id="s2s-app-id",
        s2s_token="s2s-token-never-log",
        end_smooth_window_ms=1500,
    )

    loaded = load_volc_voice_config(
        override_json=json.dumps(configured),
        s2s_app_id="unused-app-id",
        s2s_token="unused-token",
        end_smooth_window_ms=800,
    )

    assert loaded["S2SConfig"]["ProviderParams"]["asr"]["extra"] == {"end_smooth_window_ms": 800}
    validate_volc_voice_config(loaded)


@pytest.mark.parametrize("window_ms", [499, 50001])
def test_endpointing_window_outside_provider_range_is_rejected(window_ms: int) -> None:
    with pytest.raises(ValidationError):
        Settings(**(VOLC_REAL | {"volc_asr_end_smooth_window_ms": window_ms}))


@pytest.mark.parametrize(
    "broken_config",
    [
        {},
        {"S2SConfig": {"Provider": "other", "OutputMode": 1}},
        {"S2SConfig": {"Provider": "volcano", "OutputMode": 0}},
        {
            "S2SConfig": {
                "Provider": "volcano",
                "OutputMode": 1,
                "ProviderParams": {
                    "app": {"appid": "app", "token": "token"},
                    "dialog": {"extra": {"model": "not-O"}},
                    "asr": {"extra": {}},
                    "tts": {
                        "speaker": "speaker",
                        "audio_config": {
                            "channel": 1,
                            "format": "pcm_s16le",
                            "sample_rate": 24000,
                        },
                    },
                },
            },
            "SubtitleConfig": {"DisableRTSSubtitle": False},
            "InterruptMode": 0,
        },
        {
            "S2SConfig": {
                "Provider": "volcano",
                "OutputMode": 1,
                "ProviderParams": {
                    "app": {"appid": "app", "token": "token"},
                    "dialog": {"extra": {"model": "O"}},
                    "asr": {},
                    "tts": {
                        "speaker": "speaker",
                        "audio_config": {
                            "channel": 1,
                            "format": "pcm_s16le",
                            "sample_rate": 24000,
                        },
                    },
                },
            },
            "SubtitleConfig": {"DisableRTSSubtitle": False},
            "InterruptMode": 0,
        },
        {
            "S2SConfig": {
                "Provider": "volcano",
                "OutputMode": 1,
                "ProviderParams": {
                    "app": {"appid": "app", "token": "token"},
                    "dialog": {"extra": {"model": "O"}},
                    "asr": {"extra": {}},
                    "tts": {"speaker": "speaker", "audio_config": {}},
                },
            },
            "SubtitleConfig": {"DisableRTSSubtitle": False},
            "InterruptMode": 0,
        },
        {
            "S2SConfig": {
                "Provider": "volcano",
                "OutputMode": 1,
                "ProviderParams": {
                    "app": {"appid": "app", "token": "token"},
                    "dialog": {"extra": {"model": "O"}},
                    "asr": {"extra": {}},
                    "tts": {
                        "speaker": "speaker",
                        "audio_config": {
                            "channel": 1,
                            "format": "pcm_s16le",
                            "sample_rate": 24000,
                        },
                    },
                },
            },
            "SubtitleConfig": {"DisableRTSSubtitle": True},
            "InterruptMode": 0,
        },
        {
            "S2SConfig": {
                "Provider": "volcano",
                "OutputMode": 1,
                "ProviderParams": {
                    "app": {"appid": "app", "token": "token"},
                    "dialog": {"extra": {"model": "O"}},
                    "asr": {"extra": {}},
                    "tts": {
                        "speaker": "speaker",
                        "audio_config": {
                            "channel": 1,
                            "format": "pcm_s16le",
                            "sample_rate": 24000,
                        },
                    },
                },
            },
            "SubtitleConfig": {"DisableRTSSubtitle": False},
            "InterruptMode": 2,
        },
    ],
)
def test_volc_s2s_config_rejects_missing_or_incompatible_pipeline_fields(
    broken_config: dict,
) -> None:
    with pytest.raises(ValueError):
        validate_volc_voice_config(broken_config)


def test_real_volc_mode_validates_override_json_without_leaking_values() -> None:
    broken = default_volc_voice_config(
        s2s_app_id="s2s-app-id",
        s2s_token="secret-s2s-token",
    )
    broken["SubtitleConfig"]["DisableRTSSubtitle"] = True
    with pytest.raises(ValidationError) as exc_info:
        Settings(**(VOLC_REAL | {"volc_voice_chat_config_json": json.dumps(broken)}))
    assert "secret-s2s-token" not in str(exc_info.value)


async def test_real_mode_never_installs_fake_voice_provider() -> None:
    container = build_container(Settings(**REAL))
    try:
        assert isinstance(container.voice_provider, OpenAIVoiceProvider)
        assert not isinstance(container.voice_provider, FakeVoiceProvider)
    finally:
        await container.aclose()


async def test_real_volc_mode_does_not_require_openai_key() -> None:
    settings = Settings(**VOLC_REAL)
    container = build_container(settings)
    try:
        assert isinstance(container.voice_provider, VolcRtcVoiceProvider)
    finally:
        await container.aclose()


async def test_openai_realtime_client_secret_is_real_provider_response() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"value": "ek_live_ephemeral", "expires_at": 2_000_000_000})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        credential = await OpenAIVoiceProvider("sk-server-key", client).issue_credentials(
            "sess_core", "u1", ("get_positions",), "gpt-realtime", datetime.now(UTC)
        )

    assert credential.client_secret == "ek_live_ephemeral"
    assert requests[0].url.path == "/v1/realtime/client_secrets"
    assert requests[0].headers["Authorization"] == "Bearer sk-server-key"
    assert json.loads(requests[0].content)["session"]["type"] == "realtime"


def test_bearer_openai_key_and_client_secret_are_redacted_from_logs() -> None:
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "auth=%s api=sk-server-secret ephemeral=ek-client-secret",
        ("Bearer opaque-core-token",),
        None,
    )
    SecretRedactionFilter().filter(record)
    rendered = record.getMessage()
    assert "opaque-core-token" not in rendered
    assert "sk-server-secret" not in rendered
    assert "ek-client-secret" not in rendered
    assert rendered.count("[REDACTED]") == 3
