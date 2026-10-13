from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI

from app.api.agent import router
from app.config import Settings
from app.container import Container, build_container
from app.runtime.session import Scope, Session
from app.voice.callback_auth import CALLBACK_AUTH_VERSION, derive_callback_signature

CALLBACK_SECRET = "callback-secret-at-least-32-characters"


class StopCountingProvider:
    def __init__(self) -> None:
        self.stops = 0

    async def stop(self, session_id: str, user_id: str, attempt: int = 0) -> None:
        self.stops += 1


def _now() -> datetime:
    return datetime(2026, 8, 7, 5, 0, tzinfo=UTC)


async def _client() -> tuple[httpx.AsyncClient, Container]:
    app = FastAPI()
    container = build_container(
        Settings(
            volc_callback_url="https://runtime.example.com/v1/agent/volc-callback",
            volc_callback_secret=CALLBACK_SECRET,
            redis_url="",
        )
    )
    app.state.container = container
    app.include_router(router, prefix="/v1/agent")
    await container.session_store.create(
        Session(
            session_id="sess_callback",
            user_id="u1",
            modality="voice",
            scope=Scope(agents=("avatar",), tools=()),
            state="voice_starting",
            created_at=_now(),
            last_activity_at=_now(),
            voice_trace_id="trc_callback",
            voice_app_id="123456789012345678901234",
            voice_room_id="room_callback",
            voice_user_id="user_callback",
            voice_task_id="task_callback",
            voice_bot_user_id="agent_callback",
            voice_run_stage="start_requested",
        )
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client, container


def _event(**overrides) -> dict:
    return {
        "signature": CALLBACK_SECRET,
        "AppId": "123456789012345678901234",
        "RoomId": "room_callback",
        "TaskId": "task_callback",
        "RunStage": "taskStarted",
        "ErrorCode": 0,
    } | overrides


def _official_state_event(signature: str, **overrides: object) -> dict[str, object]:
    inner: dict[str, object] = {
        "EventTime": 1786939887000,
        "RoundID": 3,
        "Stage": {"Code": 1, "Description": "listening"},
        "TaskId": "task_callback",
        "UserID": "user_callback",
    }
    inner.update(overrides)
    payload = json.dumps(inner, separators=(",", ":")).encode()
    envelope = b"conv" + len(payload).to_bytes(4, "big") + payload
    return {
        "message": base64.b64encode(envelope).decode(),
        "binary": True,
        "signature": signature,
    }


async def _enable_derived_callback_auth(container: Container) -> str:
    session = await container.session_store.get("sess_callback")
    assert session is not None
    session = replace(session, voice_callback_auth_version=CALLBACK_AUTH_VERSION)
    await container.session_store.update(session)
    return derive_callback_signature(
        CALLBACK_SECRET,
        purpose="state",
        session_id=session.session_id,
        task_id=session.voice_task_id or "",
    )


async def test_official_binary_state_callback_without_content_type_is_accepted() -> None:
    client, container = await _client()
    signature = await _enable_derived_callback_auth(container)
    body = json.dumps(_official_state_event(signature)).encode()
    async with client:
        response = await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            content=body,
        )
    session = await container.session_store.get("sess_callback")
    assert response.status_code == 200
    assert session is not None
    assert session.state == "bot_joining"
    assert session.voice_run_stage == "listening"
    assert session.voice_asr_started_at is not None
    assert session.voice_user_audio_status == "UNKNOWN"
    assert session.voice_callback_schema_hash is not None
    assert container.voice_callback_metrics.callback_accepted_total == 1


async def test_official_callback_can_omit_task_id_when_derived_signature_binds_attempt() -> None:
    client, container = await _client()
    signature = await _enable_derived_callback_auth(container)
    event = _official_state_event(signature)
    decoded = base64.b64decode(str(event["message"]))
    inner = json.loads(decoded[8:])
    inner.pop("TaskId")
    payload = json.dumps(inner, separators=(",", ":")).encode()
    event["message"] = base64.b64encode(
        b"conv" + len(payload).to_bytes(4, "big") + payload
    ).decode()
    async with client:
        responses = [
            await client.post("/v1/agent/volc-callback?session_id=sess_callback", json=event)
            for _ in range(3)
        ]
    session = await container.session_store.get("sess_callback")
    assert all(response.status_code == 200 for response in responses)
    assert session is not None
    assert session.voice_callback_accepted_count == 1
    assert session.voice_callback_duplicate_count == 2
    assert container.voice_callback_metrics.callback_duplicate_total == 2


async def test_official_callback_wrong_task_is_stale_and_cannot_mutate_current_attempt() -> None:
    client, container = await _client()
    signature = await _enable_derived_callback_auth(container)
    async with client:
        response = await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_official_state_event(signature, TaskId="task_old_attempt"),
        )
    session = await container.session_store.get("sess_callback")
    assert response.status_code == 200
    assert session is not None
    assert session.state == "voice_starting"
    assert session.voice_callback_stale_count == 1
    assert container.voice_callback_metrics.callback_stale_total == 1


async def test_previous_attempt_derived_signature_is_acknowledged_but_ignored() -> None:
    client, container = await _client()
    session = await container.session_store.get("sess_callback")
    assert session is not None
    old_signature = derive_callback_signature(
        CALLBACK_SECRET,
        purpose="state",
        session_id=session.session_id,
        task_id=session.voice_task_id or "",
    )
    await container.session_store.update(
        replace(
            session,
            voice_callback_auth_version=CALLBACK_AUTH_VERSION,
            voice_previous_task_ids=(session.voice_task_id or "",),
            voice_task_id="task_new_attempt",
            voice_attempt=1,
            state="bot_joining",
        )
    )
    async with client:
        response = await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_official_state_event(old_signature),
        )
    current = await container.session_store.get("sess_callback")
    assert response.status_code == 200
    assert current is not None
    assert current.state == "bot_joining"
    assert current.voice_callback_event_ids == ()
    assert container.voice_callback_metrics.callback_stale_total == 1


async def test_official_callback_rejects_invalid_binary_envelope_without_payload_log(
    caplog,
) -> None:
    client, container = await _client()
    signature = await _enable_derived_callback_auth(container)
    raw_secret = "raw-state-payload-must-not-be-logged"
    event = {
        "message": base64.b64encode(raw_secret.encode()).decode(),
        "binary": True,
        "signature": signature,
    }
    with caplog.at_level(logging.INFO):
        async with client:
            response = await client.post(
                "/v1/agent/volc-callback?session_id=sess_callback", json=event
            )
    assert response.status_code == 400
    assert raw_secret not in caplog.text
    assert str(event["message"]) not in caplog.text
    assert container.voice_callback_metrics.callback_schema_rejected_total == 1


async def test_callback_does_not_mark_task_ready_on_http_acceptance() -> None:
    client, container = await _client()
    async with client:
        response = await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback", json=_event()
        )
    session = await container.session_store.get("sess_callback")
    assert response.status_code == 200
    assert session is not None
    assert session.state == "task_accepted"
    assert session.voice_run_stage == "taskStarted"
    assert session.voice_callback_received_at is not None


async def test_callback_pipeline_stage_does_not_claim_bot_joined() -> None:
    client, container = await _client()
    async with client:
        response = await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(RunStage="listening"),
        )
    session = await container.session_store.get("sess_callback")
    assert response.status_code == 200
    assert session is not None
    assert session.state == "bot_joining"
    assert session.voice_error_code is None


async def test_callback_persists_async_failure_without_logging_secret(
    caplog,
) -> None:
    client, container = await _client()
    with caplog.at_level(logging.INFO, logger="app.api.agent"):
        async with client:
            response = await client.post(
                "/v1/agent/volc-callback?session_id=sess_callback",
                json=_event(
                    RunStage="failed",
                    ErrorCode="S2S_MODEL_NOT_ENABLED",
                    ErrorMessage=("model O unavailable token=do-not-log-this " + CALLBACK_SECRET),
                ),
            )
    session = await container.session_store.get("sess_callback")
    assert response.status_code == 200
    assert session is not None
    assert session.state == "provider_failed"
    assert session.voice_error_code == "VOICE_PROVIDER_FAILED"
    assert session.voice_error_message == "连接失败，请重试"
    assert session.voice_provider_error_code == "S2S_MODEL_NOT_ENABLED"
    assert session.voice_original_failure_stage == "failed"
    assert session.voice_first_failed_at is not None
    assert "do-not-log-this" not in caplog.text
    assert CALLBACK_SECRET not in caplog.text


async def test_callback_accepts_official_nested_error_shape_and_get_exposes_it() -> None:
    client, container = await _client()
    async with client:
        callback = await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(
                EventType=1,
                RunStage="preParamCheck",
                ErrorCode=None,
                ErrorInfo={
                    "Errorcode": 1000002,
                    "Reason": "requested resource not granted",
                },
            ),
        )
        state = await client.get(
            "/v1/agent/sessions/sess_callback",
            headers={"Authorization": "Bearer test-user:u1"},
        )

    assert callback.status_code == 200
    assert state.status_code == 200
    assert state.json()["state"] == "provider_failed"
    assert state.json()["voice_error_code"] == "VOICE_PROVIDER_FAILED"
    assert state.json()["voice_error_message"] == "连接失败，请重试"
    assert state.json()["voice_run_stage"] == "preParamCheck"
    assert state.json()["voice_diagnostics"]["provider_error_code"] == "1000002"
    assert state.json()["voice_diagnostics"]["original_failure_stage"] == ("preParamCheck")
    session = await container.session_store.get("sess_callback")
    assert session is not None
    assert session.voice_provider_error_code == "1000002"


async def test_callback_does_not_regress_failed_on_late_event() -> None:
    client, container = await _client()
    async with client:
        await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(RunStage="llmOutput"),
        )
        await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(RunStage="taskStart"),
        )
    joining = await container.session_store.get("sess_callback")
    assert joining is not None
    assert joining.state == "bot_joining"

    client, container = await _client()
    async with client:
        await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(
                EventType=1,
                ErrorCode=None,
                ErrorInfo={"Errorcode": 1003006},
            ),
        )
        await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(RunStage="taskStart"),
        )
    failed = await container.session_store.get("sess_callback")
    assert failed is not None
    assert failed.state == "provider_failed"
    assert failed.voice_error_code == "VOICE_PROVIDER_FAILED"
    assert failed.voice_provider_error_code == "1003006"


async def test_callback_rejects_bad_signature_and_identity() -> None:
    client, container = await _client()
    async with client:
        bad_signature = await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(signature="wrong"),
        )
        bad_room = await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(RoomId="other-room"),
        )
        missing_task = await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(TaskId=None),
        )
    session = await container.session_store.get("sess_callback")
    assert bad_signature.status_code == 401
    assert bad_room.status_code == 400
    assert missing_task.status_code == 400
    assert session is not None
    assert session.state == "voice_starting"


async def test_duplicate_callback_is_idempotent() -> None:
    client, container = await _client()
    provider = StopCountingProvider()
    container.voice_provider = provider
    event = _event(
        EventType=1,
        RunStage="preParamCheck",
        ErrorInfo={"Errorcode": 1000002, "Reason": "requested resource not granted"},
    )
    async with client:
        responses = await asyncio.gather(
            *(
                client.post("/v1/agent/volc-callback?session_id=sess_callback", json=event)
                for _ in range(10)
            )
        )
    session = await container.session_store.get("sess_callback")
    assert all(response.status_code == 200 for response in responses)
    assert session is not None
    assert session.state == "provider_failed"
    assert session.voice_provider_error_code == "1000002"
    assert len(session.voice_callback_event_ids) == 1
    assert provider.stops == 1


async def test_failed_session_stop_callback_preserves_original_error() -> None:
    client, container = await _client()
    async with client:
        await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(
                EventType=1,
                RunStage="preParamCheck",
                ErrorCode="S2S_MODEL_NOT_ENABLED",
                ErrorMessage="model is unavailable",
            ),
        )
        await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(RunStage="taskStopped", ErrorCode=0),
        )
    session = await container.session_store.get("sess_callback")
    assert session is not None
    assert session.state == "provider_failed"
    assert session.voice_run_stage == "preParamCheck"
    assert session.voice_error_code == "VOICE_PROVIDER_FAILED"
    assert session.voice_provider_error_code == "S2S_MODEL_NOT_ENABLED"
    assert session.voice_original_failure_stage == "preParamCheck"
    assert session.voice_cleanup_status == "success"


async def test_ready_callback_tracks_audio_and_interrupt_without_claiming_audibility() -> None:
    client, container = await _client()
    session = await container.session_store.get("sess_callback")
    assert session is not None
    await container.session_store.update(replace(session, state="voice_ready"))
    async with client:
        audio = await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(RunStage="answerStart", TraceId="trace_audio"),
        )
        interrupted = await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(RunStage="interrupted", TraceId="trace_interrupt"),
        )
    updated = await container.session_store.get("sess_callback")
    assert audio.status_code == interrupted.status_code == 200
    assert updated is not None
    assert updated.state == "voice_ready"
    assert updated.voice_provider_audio_started_at is not None
    assert updated.voice_interrupted_at is not None
    assert updated.voice_callback_trace_id == "trace_interrupt"


async def test_old_callback_after_retry_or_stop_cannot_mutate_session() -> None:
    client, container = await _client()
    session = await container.session_store.get("sess_callback")
    assert session is not None
    await container.session_store.update(
        replace(
            session,
            state="voice_stopped",
            voice_task_id="task_new_attempt",
            voice_attempt=1,
        )
    )
    async with client:
        stale = await client.post("/v1/agent/volc-callback?session_id=sess_callback", json=_event())
    unchanged = await container.session_store.get("sess_callback")
    assert stale.status_code == 200
    assert unchanged is not None
    assert unchanged.state == "voice_stopped"
    assert unchanged.voice_task_id == "task_new_attempt"


async def test_callback_accepts_camelcase_and_nested_containers() -> None:
    client, container = await _client()
    async with client:
        res = await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json={
                "signature": CALLBACK_SECRET,
                "header": {"appId": "123456789012345678901234"},
                "payload": {
                    "roomId": "room_callback",
                    "taskId": "task_callback",
                    "run_stage": "listening",
                },
            },
        )
    session = await container.session_store.get("sess_callback")
    assert res.status_code == 200
    assert session is not None
    assert session.state == "bot_joining"
    assert session.voice_run_stage == "listening"


async def test_complete_callback_pipeline_records_each_safe_stage() -> None:
    client, container = await _client()
    session = await container.session_store.get("sess_callback")
    assert session is not None
    await container.session_store.update(
        replace(session, state="voice_ready", voice_bot_joined_at=_now())
    )
    stages = (
        "taskStart",
        "inputAudioReceived",
        "asrStarted",
        "userTranscript",
        "llmStarted",
        "llmCompleted",
        "ttsStarted",
        "assistantTranscript",
        "audioStart",
        "taskStopped",
    )
    async with client:
        responses = [
            await client.post(
                "/v1/agent/volc-callback?session_id=sess_callback",
                json=_event(RunStage=stage, TraceId=f"trace_{index}"),
            )
            for index, stage in enumerate(stages)
        ]

    updated = await container.session_store.get("sess_callback")
    assert all(response.status_code == 200 for response in responses)
    assert updated is not None
    assert updated.state == "voice_stopped"
    assert updated.voice_task_started_at is not None
    assert updated.voice_first_provider_callback_at is not None
    assert updated.voice_user_audio_status == "OBSERVED"
    assert updated.voice_asr_started_at is not None
    assert updated.voice_user_transcript_at is not None
    assert updated.voice_llm_started_at is not None
    assert updated.voice_llm_completed_at is not None
    assert updated.voice_tts_started_at is not None
    assert updated.voice_assistant_transcript_at is not None
    assert updated.voice_provider_audio_started_at is not None
    assert updated.voice_stopped_at is not None
    assert updated.voice_cleanup_status == "success"
    assert len(updated.voice_callback_event_ids) == len(stages)


async def test_asr_success_then_llm_failure_preserves_exact_failure_stage() -> None:
    client, container = await _client()
    session = await container.session_store.get("sess_callback")
    assert session is not None
    await container.session_store.update(
        replace(session, state="voice_ready", voice_bot_joined_at=_now())
    )
    async with client:
        await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(RunStage="userTranscript", TraceId="trace_user"),
        )
        failed = await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(
                RunStage="llmFailed",
                ErrorCode="ARK_QUOTA_EXCEEDED",
                ErrorMessage="model quota unavailable",
                TraceId="trace_llm_failed",
            ),
        )

    updated = await container.session_store.get("sess_callback")
    assert failed.status_code == 200
    assert updated is not None
    assert updated.voice_asr_started_at is not None
    assert updated.voice_user_transcript_at is not None
    assert updated.voice_llm_completed_at is None
    assert updated.state == "provider_failed"
    assert updated.voice_original_failure_stage == "llmFailed"
    assert updated.voice_provider_error_code == "ARK_QUOTA_EXCEEDED"


async def test_llm_success_then_tts_failure_preserves_exact_failure_stage() -> None:
    client, container = await _client()
    session = await container.session_store.get("sess_callback")
    assert session is not None
    await container.session_store.update(
        replace(session, state="voice_ready", voice_bot_joined_at=_now())
    )
    async with client:
        await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(RunStage="llmCompleted", TraceId="trace_llm_done"),
        )
        failed = await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(
                RunStage="ttsFailed",
                ErrorCode="TTS_PROVIDER_UNAVAILABLE",
                ErrorMessage="speech provider unavailable",
                TraceId="trace_tts_failed",
            ),
        )

    updated = await container.session_store.get("sess_callback")
    assert failed.status_code == 200
    assert updated is not None
    assert updated.voice_llm_started_at is not None
    assert updated.voice_llm_completed_at is not None
    assert updated.voice_tts_started_at is None
    assert updated.voice_assistant_transcript_at is None
    assert updated.state == "provider_failed"
    assert updated.voice_original_failure_stage == "ttsFailed"
    assert updated.voice_provider_error_code == "TTS_PROVIDER_UNAVAILABLE"


async def test_callback_structured_log_fields_are_aligned_and_secret_safe(caplog) -> None:
    client, container = await _client()
    session = await container.session_store.get("sess_callback")
    assert session is not None
    await container.session_store.update(
        replace(session, voice_provider_request_id="start_request_exact")
    )
    secret_payload = "raw-payload-must-never-be-logged"
    with caplog.at_level(logging.INFO, logger="app.api.agent"):
        async with client:
            response = await client.post(
                "/v1/agent/volc-callback?session_id=sess_callback",
                json=_event(
                    RunStage="taskStart",
                    TraceId="callback_trace_exact",
                    ExtraSecret=secret_payload,
                ),
            )

    assert response.status_code == 200
    callback_record = next(
        record for record in caplog.records if record.message.startswith("volc_callback event=")
    )
    event = json.loads(callback_record.message.removeprefix("volc_callback event="))
    assert event == {
        "callback_trace_id": "callback_trace_exact",
        "error_code": "0",
        "error_message": None,
        "room_hash": "ac8e102eadff",
        "run_stage": "taskStart",
        "session_id": "sess_callback",
        "start_request_id": "start_request_exact",
        "task_hash": "45ce263b3c13",
    }
    assert CALLBACK_SECRET not in caplog.text
    assert secret_payload not in caplog.text
    assert "room_callback" not in caplog.text
    assert "task_callback" not in caplog.text


async def test_late_asr_clears_mobile_silence_error_but_keeps_diagnostic_history() -> None:
    client, container = await _client()
    session = await container.session_store.get("sess_callback")
    assert session is not None
    await container.session_store.update(
        replace(
            session,
            state="voice_ready",
            voice_bot_joined_at=_now(),
            voice_provider_audio_started_at=_now(),
            voice_error_code="VOICE_UPLINK_OR_ASR_SILENT",
            voice_error_message="未检测到语音，请检查麦克风后重试。",
            voice_original_failure_stage="waiting_for_uplink_or_asr",
            voice_first_failed_at=_now(),
        )
    )
    async with client:
        response = await client.post(
            "/v1/agent/volc-callback?session_id=sess_callback",
            json=_event(RunStage="asrStarted", TraceId="trace_recovered"),
        )

    recovered = await container.session_store.get("sess_callback")
    assert response.status_code == 200
    assert recovered is not None
    assert recovered.state == "voice_ready"
    assert recovered.voice_asr_started_at is not None
    assert recovered.voice_error_code is None
    assert recovered.voice_error_message is None
    assert recovered.voice_original_failure_stage == "waiting_for_uplink_or_asr"
    assert recovered.voice_first_failed_at is not None
