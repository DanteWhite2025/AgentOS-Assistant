"""pipeline 渠道的装配：容器建得出流水线，签票端点被显式挡掉。

流水线不签票、不直连、没有第三方 Bot 进房 —— ``/api/voice`` 那套端点在这个
渠道下没有对应语义。返回一张用不了的空票比直接报错危险得多，所以这里要求
它抛受控异常。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.container import build_container
from app.main import create_app
from app.voice.pipeline_channel import PipelineChannelVoiceProvider
from app.voice.pipeline_provider import VoicePipeline
from app.voice.registry import default_voice_provider_registry
from app.voice.volc_tts import VolcengineTTSClient

PIPELINE_REAL = {
    "provider_mode": "real",
    "env": "staging",
    "realtime_voice_provider": "pipeline",
    "huochan_api_key": "volc-tts-api-key",
    "llm_base_url": "https://newapi.example.com/v1",
    "llm_api_key": "llm-gateway-not-logged",
    "llm_model": "deepseek-chat",
    "followin_api_key": "followin-key-for-test",
    "core_actor_token": "agent-staging-token",
    "core_base_url": "https://core-staging.example.com",
    "rt_ws_base": "wss://runtime-staging.example.com",
    "redis_url": "",
}


@contextmanager
def _pipeline_client() -> Iterator[TestClient]:
    """独立 app，语音渠道换成 pipeline，其余（Core 鉴权 / LLM / 新闻）仍走 fake。

    两个坑：绝不能改模块级 ``app`` 的容器（所有测试共享的单例，改了不恢复会让
    后面按字母序跑到的用例拿到 pipeline provider，实测把 webrtc 那组打挂过）；
    换 provider 必须在 startup **之后** —— lifespan 会重建容器，在它之前设置
    的会被静默覆盖，测试于是对着一个 fake provider 断言。
    """

    with TestClient(create_app()) as client:
        client.app.state.container.voice_provider = PipelineChannelVoiceProvider()
        yield client


def test_pipeline_is_a_registered_channel() -> None:
    assert "pipeline" in default_voice_provider_registry().names()


def test_container_builds_a_voice_pipeline_for_the_pipeline_channel() -> None:
    container = build_container(Settings(**PIPELINE_REAL))

    assert isinstance(container.voice_pipeline, VoicePipeline)


def test_container_wires_the_approved_voice_and_speed_into_the_tts_client() -> None:
    """音色和语速必须从配置流到真正发请求的那一层，不能各写一份默认值。"""

    settings = Settings(**(PIPELINE_REAL | {"huochan_speed_ratio": 1.2}))
    container = build_container(settings)

    synthesizer = container.voice_pipeline._synthesizer  # type: ignore[union-attr]
    assert isinstance(synthesizer, VolcengineTTSClient)
    assert synthesizer.config.voice == settings.huochan_voice_type
    assert synthesizer.config.speed_ratio == 1.2
    assert synthesizer.config.api_key == settings.huochan_api_key


def test_other_channels_do_not_build_a_pipeline() -> None:
    """没开 pipeline 就不该有流水线实例挂在容器上。"""

    settings = Settings(
        **(
            PIPELINE_REAL
            | {"realtime_voice_provider": "gemini_live", "gemini_api_key": "gemini-key"}
        )
    )
    container = build_container(settings)

    assert container.voice_pipeline is None


def test_dual_mode_builds_pipeline_and_gemini_preflight_together() -> None:
    settings = Settings(
        **(
            PIPELINE_REAL
            | {
                "enabled_voice_providers": "pipeline,gemini_live",
                "gemini_api_key": "gemini-key-not-logged",
            }
        )
    )

    container = build_container(settings)

    assert isinstance(container.voice_pipeline, VoicePipeline)
    assert container.gemini_live_preflight is not None
    assert container.voice_provider_for("gemini_live") is container.voice_providers["gemini_live"]


@pytest.mark.asyncio
async def test_the_pipeline_channel_refuses_to_issue_a_useless_ticket() -> None:
    container = build_container(Settings(**PIPELINE_REAL))
    provider = container.voice_provider

    with pytest.raises(NotImplementedError):
        await provider.issue_credentials(
            "sess_1", "user_1", ("get_market_price",), "model", datetime.now(UTC)
        )


@pytest.mark.asyncio
async def test_the_pipeline_channel_has_no_bot_lifecycle_to_drive() -> None:
    container = build_container(Settings(**PIPELINE_REAL))
    provider = container.voice_provider

    assert provider.requires_bot_presence is False
    with pytest.raises(NotImplementedError):
        await provider.start("sess_1", "user_1")
    with pytest.raises(NotImplementedError):
        await provider.presence("sess_1", "user_1")
    with pytest.raises(NotImplementedError):
        await provider.send_function_result("sess_1", "user_1", "call_1", {})
    with pytest.raises(NotImplementedError):
        await provider.send_progress_message("sess_1", "user_1", "hi")


@pytest.mark.parametrize("modality", ["text", "voice", "both"])
def test_creating_a_session_works_for_every_modality_on_the_pipeline_channel(
    modality: str,
) -> None:
    """流水线渠道下建会话不能报错，任何 modality 都不行。

    02 方案里音频经 WS 下发，客户端不直连第三方，所以没有票可签 —— 但「没有
    票」不等于「建不了会话」。App 的入口大多用 voice/both，这里一炸整个进不去。
    """

    # 只换语音渠道：Core 鉴权、LLM、新闻仍走 fake，免得把「签票这一步」的
    # 断言淹没在上游不可用里。
    with _pipeline_client() as client:
        response = client.post(
            "/v1/agent/sessions",
            json={"modality": modality},
            headers={"Authorization": "Bearer test-user:u1"},
        )

    assert response.status_code == 201, response.text
    body = response.json()
    # 不签票：voice 字段留空，前端据此走 WS 音频而不是找票直连。
    assert body.get("voice") is None
    assert body["session_id"]
    assert body["rt_token"]


def _pipeline_session(client: TestClient, modality: str = "both") -> str:
    response = client.post(
        "/v1/agent/sessions",
        json={"modality": modality},
        headers={"Authorization": "Bearer test-user:u1"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["session_id"])


def test_asking_for_a_direct_link_ticket_is_a_clear_400_not_a_500() -> None:
    """客户端跑来领票是用错了端点，得到的必须是能指路的 400。

    未捕获的 NotImplementedError 会变成 500，前端只会看到「服务器错误」，
    然后往凭证方向排查很久 —— 而真正的原因是这条链路在本渠道不存在。
    """

    with _pipeline_client() as client:
        session_id = _pipeline_session(client)
        response = client.post(
            f"/v1/agent/sessions/{session_id}/voice/start",
            headers={"Authorization": "Bearer test-user:u1"},
        )

    assert response.status_code == 400, response.text
    assert "WebSocket" in response.json()["message"]


def test_renewing_a_voice_token_on_the_pipeline_channel_is_also_a_clear_400() -> None:
    with _pipeline_client() as client:
        session_id = _pipeline_session(client)
        response = client.post(
            f"/v1/agent/sessions/{session_id}/token",
            json={"want": ["voice"]},
            headers={"Authorization": "Bearer test-user:u1"},
        )

    assert response.status_code == 400, response.text
    assert "audio.chunk" in response.json()["message"]


@pytest.mark.asyncio
async def test_stopping_the_pipeline_channel_is_a_no_op_not_an_error() -> None:
    """会话清理链路会无条件调 stop，它必须安全 —— 这里本来就没有任务可停。"""

    container = build_container(Settings(**PIPELINE_REAL))

    await container.voice_provider.stop("sess_1", "user_1")
