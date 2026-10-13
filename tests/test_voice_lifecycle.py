from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import FastAPI

from app.api.agent import (
    _bot_presence_poll_delay,
    _monitor_asr_silence,
    _monitor_bot_join,
    router,
)
from app.config import Settings
from app.container import Container, build_container
from app.runtime.session import Scope, Session, SessionStateValue
from app.voice.callback_auth import derive_callback_signature
from app.voice.provider import (
    VoiceCredential,
    VoicePresence,
    VoiceTaskAcceptance,
)


class LifecycleProvider:
    def __init__(self, presence: list[bool] | None = None) -> None:
        self.presence_results = presence or [False]
        self.presence_calls = 0
        self.starts = 0
        self.stops = 0

    async def issue_credentials(
        self,
        session_id: str,
        user_id: str,
        tools: tuple[str, ...],
        model: str,
        now: datetime,
        attempt: int = 0,
        context_summary: str = "",
    ) -> VoiceCredential:
        return VoiceCredential(
            provider="volc_rtc",
            expires_at=now + timedelta(minutes=10),
            app_id="123456789012345678901234",
            room_id="room_lifecycle",
            user_id="mobile_lifecycle",
            token="rtc-token-never-log",
            task_id=f"task_lifecycle_{attempt}",
            bot_user_id="bot_lifecycle",
        )

    async def start(
        self,
        session_id: str,
        user_id: str,
        attempt: int = 0,
        context_summary: str = "",
    ) -> VoiceTaskAcceptance:
        self.starts += 1
        return VoiceTaskAcceptance(provider_request_id=f"request_{attempt}")

    async def stop(self, session_id: str, user_id: str, attempt: int = 0) -> None:
        self.stops += 1

    async def presence(self, session_id: str, user_id: str, attempt: int = 0) -> VoicePresence:
        index = min(self.presence_calls, len(self.presence_results) - 1)
        self.presence_calls += 1
        bot_joined = self.presence_results[index]
        return VoicePresence(
            bot_joined=bot_joined,
            provider_request_id=f"presence_{self.presence_calls}",
            online_user_ids={"mobile_lifecycle"} | ({"bot_lifecycle"} if bot_joined else set()),
        )


def _session(state: SessionStateValue = "task_accepted") -> Session:
    now = datetime.now(UTC)
    return Session(
        session_id="sess_lifecycle",
        user_id="u1",
        modality="voice",
        scope=Scope(agents=("avatar",), tools=()),
        state=state,
        created_at=now,
        last_activity_at=now,
        voice_trace_id="trc_lifecycle",
        voice_app_id="123456789012345678901234",
        voice_room_id="room_lifecycle",
        voice_user_id="mobile_lifecycle",
        voice_task_id="task_lifecycle_0",
        voice_bot_user_id="bot_lifecycle",
        voice_task_submitted_at=now,
    )


def _container(provider: LifecycleProvider) -> Container:
    container = build_container(Settings(redis_url=""))
    container.voice_provider = provider
    container.settings.volc_bot_join_timeout_seconds = 0.03
    container.settings.volc_bot_presence_poll_seconds = 0.005
    return container


def test_bot_presence_uses_bounded_fast_backoff() -> None:
    assert [_bot_presence_poll_delay(1.0, index) for index in range(6)] == [
        0.5,
        0.75,
        1.0,
        1.5,
        1.5,
        1.5,
    ]
    assert _bot_presence_poll_delay(0.005, 20) == 0.005
    assert _bot_presence_poll_delay(2.0, 0) == 2.0


async def test_task_accepted_but_bot_never_joins_times_out() -> None:
    provider = LifecycleProvider([False])
    container = _container(provider)
    await container.session_store.create(_session())

    await _monitor_bot_join(container, "sess_lifecycle", 0)

    session = await container.session_store.get("sess_lifecycle")
    assert session is not None
    assert session.state == "bot_join_timeout"
    assert session.voice_error_code == "VOICE_BOT_NOT_JOINED"
    assert session.voice_error_message == "连接失败，请重试"
    assert session.voice_original_failure_stage == "bot_join_timeout"
    assert session.voice_first_failed_at is not None
    assert session.voice_bot_joined_at is None
    assert session.voice_cleanup_status == "success"
    assert session.voice_stopped_at is not None
    assert provider.stops == 1


async def test_close_after_join_timeout_preserves_failure_and_does_not_stop_twice() -> None:
    provider = LifecycleProvider([False])
    container = _container(provider)
    await container.session_store.create(_session())
    await _monitor_bot_join(container, "sess_lifecycle", 0)
    app = FastAPI()
    app.state.container = container
    app.include_router(router, prefix="/v1/agent")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.delete(
            "/v1/agent/sessions/sess_lifecycle",
            headers={"Authorization": "Bearer test-user:u1"},
        )
        state = await client.get(
            "/v1/agent/sessions/sess_lifecycle",
            headers={"Authorization": "Bearer test-user:u1"},
        )

    session = await container.session_store.get("sess_lifecycle")
    assert response.status_code == 204
    assert state.status_code == 200
    assert provider.stops == 1
    assert session is not None
    assert session.state == "voice_stopped"
    assert session.voice_run_stage == "bot_join_timeout"
    assert session.voice_error_code == "VOICE_BOT_NOT_JOINED"
    assert session.voice_original_failure_stage == "bot_join_timeout"
    assert state.json()["voice_diagnostics"]["original_failure_stage"] == ("bot_join_timeout")


async def test_bot_delayed_join_is_confirmed_by_room_presence() -> None:
    provider = LifecycleProvider([False, False, True])
    container = _container(provider)
    await container.session_store.create(_session())

    await _monitor_bot_join(container, "sess_lifecycle", 0)

    session = await container.session_store.get("sess_lifecycle")
    assert session is not None
    assert session.state == "voice_ready"
    assert session.voice_run_stage == "bot_joined"
    assert session.voice_bot_joined_at is not None
    assert session.voice_mobile_joined_at is not None
    assert provider.presence_calls == 3


async def test_welcome_audio_without_asr_sets_silent_diagnostic_without_restart() -> None:
    provider = LifecycleProvider([True])
    container = _container(provider)
    now = datetime.now(UTC)
    await container.session_store.create(
        replace(
            _session("voice_ready"),
            voice_bot_joined_at=now,
            voice_provider_audio_started_at=now,
        )
    )
    container.settings.volc_asr_silence_timeout_seconds = 0

    await _monitor_asr_silence(container, "sess_lifecycle", 0)

    session = await container.session_store.get("sess_lifecycle")
    audit = await container.audit_store.list_by_session("sess_lifecycle")
    assert session is not None
    assert session.state == "voice_ready"
    assert session.voice_error_code == "VOICE_UPLINK_OR_ASR_SILENT"
    assert session.voice_error_message == "未检测到语音，请检查麦克风后重试。"
    assert session.voice_original_failure_stage == "waiting_for_uplink_or_asr"
    assert session.voice_user_audio_status == "UNKNOWN"
    assert session.voice_asr_started_at is None
    assert session.voice_user_transcript_at is None
    assert provider.starts == 0
    assert provider.stops == 0
    assert audit == []


async def test_user_transcript_prevents_uplink_silent_diagnostic() -> None:
    provider = LifecycleProvider([True])
    container = _container(provider)
    now = datetime.now(UTC)
    await container.session_store.create(
        replace(
            _session("voice_ready"),
            voice_bot_joined_at=now,
            voice_provider_audio_started_at=now,
            voice_asr_started_at=now,
            voice_user_transcript_at=now,
        )
    )
    container.settings.volc_asr_silence_timeout_seconds = 0

    await _monitor_asr_silence(container, "sess_lifecycle", 0)

    session = await container.session_store.get("sess_lifecycle")
    assert session is not None
    assert session.voice_error_code is None
    assert provider.starts == 0


async def test_start_http_200_then_async_provider_failure_is_persisted() -> None:
    provider = LifecycleProvider([False])
    container = _container(provider)
    container.settings.volc_bot_join_timeout_seconds = 1
    await container.session_store.create(_session("created"))
    app = FastAPI()
    app.state.container = container
    app.include_router(router, prefix="/v1/agent")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        accepted = await client.post(
            "/v1/agent/sessions/sess_lifecycle/voice/start",
            headers={"Authorization": "Bearer test-user:u1"},
        )
        monitor = container.voice_monitor_tasks.pop("sess_lifecycle")
        monitor.cancel()
        callback = await client.post(
            "/v1/agent/volc-callback?session_id=sess_lifecycle",
            json={
                "signature": derive_callback_signature(
                    container.settings.volc_callback_secret,
                    purpose="state",
                    session_id="sess_lifecycle",
                    task_id="task_lifecycle_0",
                ),
                "AppId": "123456789012345678901234",
                "RoomId": "room_lifecycle",
                "TaskId": "task_lifecycle_0",
                "RunStage": "preParamCheck",
                "EventType": 1,
                "ErrorCode": "S2S_MODEL_NOT_ENABLED",
                "ErrorMessage": "model entitlement unavailable",
                "RequestId": "callback_request_1",
            },
        )

    session = await container.session_store.get("sess_lifecycle")
    assert accepted.status_code == callback.status_code == 200
    assert accepted.json()["state"] == "task_accepted"
    assert session is not None
    assert session.state == "provider_failed"
    assert session.voice_provider_request_id == "request_0"
    assert session.voice_callback_trace_id == "callback_request_1"
    assert session.voice_provider_error_code == "S2S_MODEL_NOT_ENABLED"
    assert session.voice_cleanup_status == "success"
    assert provider.stops == 1


async def test_repeated_retry_is_idempotent_and_cleans_old_task() -> None:
    provider = LifecycleProvider([True])
    container = _container(provider)
    await container.session_store.create(_session("provider_failed"))
    app = FastAPI()
    app.state.container = container
    app.include_router(router, prefix="/v1/agent")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first, second = await asyncio.gather(
            client.post(
                "/v1/agent/sessions/sess_lifecycle/voice/start",
                headers={"Authorization": "Bearer test-user:u1"},
            ),
            client.post(
                "/v1/agent/sessions/sess_lifecycle/voice/start",
                headers={"Authorization": "Bearer test-user:u1"},
            ),
        )
    await asyncio.sleep(0.02)

    session = await container.session_store.get("sess_lifecycle")
    assert first.status_code == second.status_code == 200
    assert provider.stops == 1
    assert provider.starts == 1
    assert session is not None
    assert session.voice_attempt == 1
    assert session.voice_task_id == "task_lifecycle_1"
    assert session.state == "voice_ready"


async def test_stop_is_idempotent_and_records_cleanup_evidence() -> None:
    provider = LifecycleProvider([True])
    container = _container(provider)
    await container.session_store.create(_session("voice_ready"))
    app = FastAPI()
    app.state.container = container
    app.include_router(router, prefix="/v1/agent")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.delete(
            "/v1/agent/sessions/sess_lifecycle",
            headers={"Authorization": "Bearer test-user:u1"},
        )
        second = await client.delete(
            "/v1/agent/sessions/sess_lifecycle",
            headers={"Authorization": "Bearer test-user:u1"},
        )

    session = await container.session_store.get("sess_lifecycle")
    assert first.status_code == second.status_code == 204
    assert provider.stops == 1
    assert session is not None
    assert session.state == "voice_stopped"
    assert session.voice_cleanup_status == "success"
    assert session.voice_stopped_at is not None


async def test_transcript_archive_stores_presence_evidence_not_content(caplog) -> None:
    provider = LifecycleProvider([True])
    container = _container(provider)
    await container.session_store.create(_session("voice_ready"))
    app = FastAPI()
    app.state.container = container
    app.include_router(router, prefix="/v1/agent")
    secret_words = "完整用户语音绝不能进入日志"

    with caplog.at_level(logging.INFO, logger="app.api.agent"):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/agent/sessions/sess_lifecycle/history",
                headers={"Authorization": "Bearer test-user:u1"},
                json={
                    "source": "voice",
                    "turns": [
                        {"role": "user", "text": secret_words},
                        {"role": "assistant", "text": "这是回答"},
                    ],
                },
            )

    session = await container.session_store.get("sess_lifecycle")
    assert response.status_code == 202
    assert session is not None
    assert session.voice_user_transcript_at is not None
    assert session.voice_assistant_transcript_at is not None
    assert secret_words not in caplog.text
    assert secret_words not in repr(session)


async def test_get_session_recovers_bot_monitor_after_runtime_restart() -> None:
    provider = LifecycleProvider([True])
    container = _container(provider)
    await container.session_store.create(
        replace(_session("bot_joining"), voice_task_submitted_at=None)
    )
    assert container.voice_monitor_tasks == {}
    app = FastAPI()
    app.state.container = container
    app.include_router(router, prefix="/v1/agent")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/agent/sessions/sess_lifecycle",
            headers={"Authorization": "Bearer test-user:u1"},
        )
    assert response.status_code == 200
    await asyncio.sleep(0.01)
    recovered = await container.session_store.get("sess_lifecycle")
    assert recovered is not None
    assert recovered.state == "voice_ready"
    assert recovered.voice_bot_joined_at is not None


class ClientDirectProvider(LifecycleProvider):
    """客户端直连渠道（Gemini / OpenAI）：服务端没有 Bot 要等进房。

    presence 恒为 False —— 这不是故障，而是「本渠道没有 presence 信号」。
    因此 bot_join 监控对它没有意义，跑起来只会在 30 秒后误判成连接失败。
    """

    requires_bot_presence = False

    async def presence(self, session_id: str, user_id: str, attempt: int = 0) -> VoicePresence:
        self.presence_calls += 1
        return VoicePresence(bot_joined=False)


def _client_direct_container() -> tuple[Container, ClientDirectProvider]:
    provider = ClientDirectProvider()
    return _container(provider), provider


async def test_client_direct_channel_does_not_wait_for_a_bot_that_will_never_join() -> None:
    """线上实测：Gemini 通话正常进行中，服务端却把会话标成了 bot_join_timeout。

    Runtime 观测不到这条会话的 setupComplete，所以**不伪造 voice_ready** ——
    保持 task_accepted，由 Mobile 以自己的 setupComplete 为准。要修的只是
    「别去等一个永远不会来的 Bot」。
    """

    container, provider = _client_direct_container()
    session = _session(state="created")
    await container.session_store.create(session)

    app = FastAPI()
    app.include_router(router, prefix="/v1/agent")
    app.state.container = container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/v1/agent/sessions/{session.session_id}/voice/start",
            headers={"Authorization": "Bearer test-user:u1"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["state"] == "task_accepted"
    stored = await container.session_store.get(session.session_id)
    assert stored.state == "task_accepted"
    # 没有服务端 Bot 可等，监控不该被调度。
    assert session.session_id not in container.voice_monitor_tasks
    # 更不该因为「等不到 Bot」去 stop 一通正在进行的通话。
    assert provider.stops == 0


async def test_client_direct_channel_never_schedules_the_bot_monitor_on_recovery() -> None:
    """进程重启后 GET Session 会补调度监控 —— 直连渠道同样要跳过。"""

    container, provider = _client_direct_container()
    session = replace(_session(state="task_accepted"), voice_provider_name="gemini_live")
    await container.session_store.create(session)

    app = FastAPI()
    app.include_router(router, prefix="/v1/agent")
    app.state.container = container
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/v1/agent/sessions/{session.session_id}",
            headers={"Authorization": "Bearer test-user:u1"},
        )

    assert response.status_code == 200
    assert session.session_id not in container.voice_monitor_tasks
    assert provider.stops == 0


async def test_bot_presence_channels_keep_the_existing_timeout_behaviour() -> None:
    """火山那条不受影响：等不到 Bot 仍然要超时并清理，避免遗留计费任务。"""

    container = _container(LifecycleProvider(presence=[False]))
    session = _session()
    await container.session_store.create(session)

    await _monitor_bot_join(container, session.session_id, 0)

    stored = await container.session_store.get(session.session_id)
    assert stored.state == "bot_join_timeout"
