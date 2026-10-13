"""Redis 会话存储（redis.asyncio）。实现 SessionStore 接口，带 TTL。

session 序列化为 JSON 存 Redis；key 设 TTL 避免异常中断导致内存泄漏。
配置 AGENT_REDIS_URL + provider_mode=real 即启用（见 app/container.py）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from app.runtime.session import ConversationTurn, Scope, Session, TextTurnResult

if TYPE_CHECKING:
    from redis.asyncio import Redis


class RedisSessionStore:
    def __init__(self, redis: Redis, ttl_seconds: int = 3600, prefix: str = "sess:") -> None:
        self._redis = redis
        self._ttl = ttl_seconds
        self._prefix = prefix

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    @staticmethod
    def _dump(session: Session) -> str:
        return json.dumps(
            {
                "session_id": session.session_id,
                "user_id": session.user_id,
                "modality": session.modality,
                "scope": {"agents": list(session.scope.agents), "tools": list(session.scope.tools)},
                "state": session.state,
                "created_at": session.created_at.isoformat(),
                "last_activity_at": session.last_activity_at.isoformat(),
                "voice_error_code": session.voice_error_code,
                "voice_run_stage": session.voice_run_stage,
                "voice_provider_name": session.voice_provider_name,
                "voice_trace_id": session.voice_trace_id,
                "voice_app_id": session.voice_app_id,
                "voice_room_id": session.voice_room_id,
                "voice_user_id": session.voice_user_id,
                "voice_task_id": session.voice_task_id,
                "voice_previous_task_ids": list(session.voice_previous_task_ids),
                "voice_bot_user_id": session.voice_bot_user_id,
                "voice_attempt": session.voice_attempt,
                "voice_provider_request_id": session.voice_provider_request_id,
                "voice_presence_request_id": session.voice_presence_request_id,
                "voice_callback_trace_id": session.voice_callback_trace_id,
                "voice_callback_auth_version": session.voice_callback_auth_version,
                "voice_error_message": session.voice_error_message,
                "voice_original_failure_stage": session.voice_original_failure_stage,
                "voice_provider_error_code": session.voice_provider_error_code,
                "voice_provider_error_message": session.voice_provider_error_message,
                "voice_first_failed_at": (
                    session.voice_first_failed_at.isoformat()
                    if session.voice_first_failed_at
                    else None
                ),
                "voice_task_submitted_at": (
                    session.voice_task_submitted_at.isoformat()
                    if session.voice_task_submitted_at
                    else None
                ),
                "voice_start_requested_at": (
                    session.voice_start_requested_at.isoformat()
                    if session.voice_start_requested_at
                    else None
                ),
                "voice_start_accepted_at": (
                    session.voice_start_accepted_at.isoformat()
                    if session.voice_start_accepted_at
                    else None
                ),
                "voice_mobile_joined_at": (
                    session.voice_mobile_joined_at.isoformat()
                    if session.voice_mobile_joined_at
                    else None
                ),
                "voice_bot_joined_at": (
                    session.voice_bot_joined_at.isoformat() if session.voice_bot_joined_at else None
                ),
                "voice_task_started_at": (
                    session.voice_task_started_at.isoformat()
                    if session.voice_task_started_at
                    else None
                ),
                "voice_task_creation_result": session.voice_task_creation_result,
                "voice_callback_received_at": (
                    session.voice_callback_received_at.isoformat()
                    if session.voice_callback_received_at
                    else None
                ),
                "voice_first_provider_callback_at": (
                    session.voice_first_provider_callback_at.isoformat()
                    if session.voice_first_provider_callback_at
                    else None
                ),
                "voice_callback_schema_hash": session.voice_callback_schema_hash,
                "voice_callback_received_count": session.voice_callback_received_count,
                "voice_callback_accepted_count": session.voice_callback_accepted_count,
                "voice_callback_duplicate_count": session.voice_callback_duplicate_count,
                "voice_callback_stale_count": session.voice_callback_stale_count,
                "voice_user_audio_status": session.voice_user_audio_status,
                "voice_asr_started_at": (
                    session.voice_asr_started_at.isoformat()
                    if session.voice_asr_started_at
                    else None
                ),
                "voice_user_speech_started_at": (
                    session.voice_user_speech_started_at.isoformat()
                    if session.voice_user_speech_started_at
                    else None
                ),
                "voice_user_speech_ended_at": (
                    session.voice_user_speech_ended_at.isoformat()
                    if session.voice_user_speech_ended_at
                    else None
                ),
                "voice_llm_started_at": (
                    session.voice_llm_started_at.isoformat()
                    if session.voice_llm_started_at
                    else None
                ),
                "voice_llm_completed_at": (
                    session.voice_llm_completed_at.isoformat()
                    if session.voice_llm_completed_at
                    else None
                ),
                "voice_assistant_first_token_at": (
                    session.voice_assistant_first_token_at.isoformat()
                    if session.voice_assistant_first_token_at
                    else None
                ),
                "voice_function_call_started_at": (
                    session.voice_function_call_started_at.isoformat()
                    if session.voice_function_call_started_at
                    else None
                ),
                "voice_function_call_completed_at": (
                    session.voice_function_call_completed_at.isoformat()
                    if session.voice_function_call_completed_at
                    else None
                ),
                "voice_function_call_failed_at": (
                    session.voice_function_call_failed_at.isoformat()
                    if session.voice_function_call_failed_at
                    else None
                ),
                "voice_tts_started_at": (
                    session.voice_tts_started_at.isoformat()
                    if session.voice_tts_started_at
                    else None
                ),
                "voice_tts_first_packet_at": (
                    session.voice_tts_first_packet_at.isoformat()
                    if session.voice_tts_first_packet_at
                    else None
                ),
                "voice_stopped_at": (
                    session.voice_stopped_at.isoformat() if session.voice_stopped_at else None
                ),
                "voice_cleanup_status": session.voice_cleanup_status,
                "voice_user_transcript_at": (
                    session.voice_user_transcript_at.isoformat()
                    if session.voice_user_transcript_at
                    else None
                ),
                "voice_assistant_transcript_at": (
                    session.voice_assistant_transcript_at.isoformat()
                    if session.voice_assistant_transcript_at
                    else None
                ),
                "voice_provider_audio_started_at": (
                    session.voice_provider_audio_started_at.isoformat()
                    if session.voice_provider_audio_started_at
                    else None
                ),
                "voice_interrupted_at": (
                    session.voice_interrupted_at.isoformat()
                    if session.voice_interrupted_at
                    else None
                ),
                "voice_callback_event_ids": list(session.voice_callback_event_ids),
                "voice_tool_results": [list(item) for item in session.voice_tool_results],
                "voice_tool_delivered": list(session.voice_tool_delivered),
                "text_turn_results": [
                    {
                        "operation_hash": item.operation_hash,
                        "request_hash": item.request_hash,
                        "response_json": item.response_json,
                        "created_at": item.created_at.isoformat(),
                    }
                    for item in session.text_turn_results
                ],
                "turns": [
                    {
                        "user_text": item.user_text,
                        "assistant_text": item.assistant_text,
                        "created_at": item.created_at.isoformat(),
                    }
                    for item in session.turns
                ],
            }
        )

    @staticmethod
    def _load(raw: str) -> Session:
        data = json.loads(raw)
        return Session(
            session_id=data["session_id"],
            user_id=data["user_id"],
            modality=data["modality"],
            scope=Scope(agents=tuple(data["scope"]["agents"]), tools=tuple(data["scope"]["tools"])),
            state=data["state"],
            created_at=datetime.fromisoformat(data["created_at"]),
            last_activity_at=datetime.fromisoformat(data["last_activity_at"]),
            voice_error_code=data.get("voice_error_code"),
            voice_run_stage=data.get("voice_run_stage"),
            voice_provider_name=data.get("voice_provider_name"),
            voice_trace_id=data.get("voice_trace_id"),
            voice_app_id=data.get("voice_app_id"),
            voice_room_id=data.get("voice_room_id"),
            voice_user_id=data.get("voice_user_id"),
            voice_task_id=data.get("voice_task_id"),
            voice_previous_task_ids=tuple(
                str(item) for item in data.get("voice_previous_task_ids", [])
            ),
            voice_bot_user_id=data.get("voice_bot_user_id"),
            voice_attempt=int(data.get("voice_attempt", 0)),
            voice_provider_request_id=data.get("voice_provider_request_id"),
            voice_presence_request_id=data.get("voice_presence_request_id"),
            voice_callback_trace_id=data.get("voice_callback_trace_id"),
            voice_callback_auth_version=data.get("voice_callback_auth_version"),
            voice_error_message=data.get("voice_error_message"),
            voice_original_failure_stage=data.get("voice_original_failure_stage"),
            voice_provider_error_code=data.get("voice_provider_error_code"),
            voice_provider_error_message=data.get("voice_provider_error_message"),
            voice_first_failed_at=(
                datetime.fromisoformat(data["voice_first_failed_at"])
                if data.get("voice_first_failed_at")
                else None
            ),
            voice_task_submitted_at=(
                datetime.fromisoformat(data["voice_task_submitted_at"])
                if data.get("voice_task_submitted_at")
                else None
            ),
            voice_start_requested_at=(
                datetime.fromisoformat(data["voice_start_requested_at"])
                if data.get("voice_start_requested_at")
                else None
            ),
            voice_start_accepted_at=(
                datetime.fromisoformat(data["voice_start_accepted_at"])
                if data.get("voice_start_accepted_at")
                else None
            ),
            voice_mobile_joined_at=(
                datetime.fromisoformat(data["voice_mobile_joined_at"])
                if data.get("voice_mobile_joined_at")
                else None
            ),
            voice_bot_joined_at=(
                datetime.fromisoformat(data["voice_bot_joined_at"])
                if data.get("voice_bot_joined_at")
                else None
            ),
            voice_task_started_at=(
                datetime.fromisoformat(data["voice_task_started_at"])
                if data.get("voice_task_started_at")
                else None
            ),
            voice_task_creation_result=data.get("voice_task_creation_result"),
            voice_callback_received_at=(
                datetime.fromisoformat(data["voice_callback_received_at"])
                if data.get("voice_callback_received_at")
                else None
            ),
            voice_first_provider_callback_at=(
                datetime.fromisoformat(data["voice_first_provider_callback_at"])
                if data.get("voice_first_provider_callback_at")
                else None
            ),
            voice_callback_schema_hash=data.get("voice_callback_schema_hash"),
            voice_callback_received_count=int(data.get("voice_callback_received_count", 0)),
            voice_callback_accepted_count=int(data.get("voice_callback_accepted_count", 0)),
            voice_callback_duplicate_count=int(data.get("voice_callback_duplicate_count", 0)),
            voice_callback_stale_count=int(data.get("voice_callback_stale_count", 0)),
            voice_user_audio_status=data.get("voice_user_audio_status", "UNKNOWN"),
            voice_asr_started_at=(
                datetime.fromisoformat(data["voice_asr_started_at"])
                if data.get("voice_asr_started_at")
                else None
            ),
            voice_user_speech_started_at=(
                datetime.fromisoformat(data["voice_user_speech_started_at"])
                if data.get("voice_user_speech_started_at")
                else None
            ),
            voice_user_speech_ended_at=(
                datetime.fromisoformat(data["voice_user_speech_ended_at"])
                if data.get("voice_user_speech_ended_at")
                else None
            ),
            voice_llm_started_at=(
                datetime.fromisoformat(data["voice_llm_started_at"])
                if data.get("voice_llm_started_at")
                else None
            ),
            voice_llm_completed_at=(
                datetime.fromisoformat(data["voice_llm_completed_at"])
                if data.get("voice_llm_completed_at")
                else None
            ),
            voice_assistant_first_token_at=(
                datetime.fromisoformat(data["voice_assistant_first_token_at"])
                if data.get("voice_assistant_first_token_at")
                else None
            ),
            voice_function_call_started_at=(
                datetime.fromisoformat(data["voice_function_call_started_at"])
                if data.get("voice_function_call_started_at")
                else None
            ),
            voice_function_call_completed_at=(
                datetime.fromisoformat(data["voice_function_call_completed_at"])
                if data.get("voice_function_call_completed_at")
                else None
            ),
            voice_function_call_failed_at=(
                datetime.fromisoformat(data["voice_function_call_failed_at"])
                if data.get("voice_function_call_failed_at")
                else None
            ),
            voice_tts_started_at=(
                datetime.fromisoformat(data["voice_tts_started_at"])
                if data.get("voice_tts_started_at")
                else None
            ),
            voice_tts_first_packet_at=(
                datetime.fromisoformat(data["voice_tts_first_packet_at"])
                if data.get("voice_tts_first_packet_at")
                else None
            ),
            voice_stopped_at=(
                datetime.fromisoformat(data["voice_stopped_at"])
                if data.get("voice_stopped_at")
                else None
            ),
            voice_cleanup_status=data.get("voice_cleanup_status"),
            voice_user_transcript_at=(
                datetime.fromisoformat(data["voice_user_transcript_at"])
                if data.get("voice_user_transcript_at")
                else None
            ),
            voice_assistant_transcript_at=(
                datetime.fromisoformat(data["voice_assistant_transcript_at"])
                if data.get("voice_assistant_transcript_at")
                else None
            ),
            voice_provider_audio_started_at=(
                datetime.fromisoformat(data["voice_provider_audio_started_at"])
                if data.get("voice_provider_audio_started_at")
                else None
            ),
            voice_interrupted_at=(
                datetime.fromisoformat(data["voice_interrupted_at"])
                if data.get("voice_interrupted_at")
                else None
            ),
            voice_callback_event_ids=tuple(
                str(item) for item in data.get("voice_callback_event_ids", [])
            ),
            voice_tool_results=tuple(
                (str(item[0]), str(item[1]))
                for item in data.get("voice_tool_results", [])
                if isinstance(item, list) and len(item) == 2
            ),
            voice_tool_delivered=tuple(str(item) for item in data.get("voice_tool_delivered", [])),
            text_turn_results=tuple(
                TextTurnResult(
                    operation_hash=str(item["operation_hash"]),
                    request_hash=str(item["request_hash"]),
                    response_json=str(item["response_json"]),
                    created_at=datetime.fromisoformat(str(item["created_at"])),
                )
                for item in data.get("text_turn_results", [])
                if isinstance(item, dict)
                and all(
                    name in item
                    for name in (
                        "operation_hash",
                        "request_hash",
                        "response_json",
                        "created_at",
                    )
                )
            ),
            turns=tuple(
                ConversationTurn(
                    user_text=str(item["user_text"]),
                    assistant_text=str(item["assistant_text"]),
                    created_at=datetime.fromisoformat(str(item["created_at"])),
                )
                for item in data.get("turns", [])
                if isinstance(item, dict)
                and all(name in item for name in ("user_text", "assistant_text", "created_at"))
            ),
        )

    async def create(self, session: Session) -> None:
        await self._redis.set(self._key(session.session_id), self._dump(session), ex=self._ttl)

    async def get(self, session_id: str) -> Session | None:
        raw = await self._redis.get(self._key(session_id))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return self._load(raw)

    async def update(self, session: Session) -> None:
        await self._redis.set(self._key(session.session_id), self._dump(session), ex=self._ttl)

    async def delete(self, session_id: str) -> None:
        await self._redis.delete(self._key(session_id))
