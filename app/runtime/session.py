"""会话领域模型（不可变）。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

Modality = Literal["text", "voice", "both"]
SessionStateValue = Literal[
    "created",
    "text_active",
    "voice_starting",
    "task_accepted",
    "bot_joining",
    "voice_ready",
    "bot_join_timeout",
    "provider_failed",
    "voice_stopped",
    "closed",
]


@dataclass(frozen=True)
class Scope:
    """会话的 AgentMandate。tools 是工具白名单唯一权威来源。"""

    agents: tuple[str, ...]
    tools: tuple[str, ...]


@dataclass(frozen=True)
class TextTurnResult:
    """Recoverable text response keyed only by hashes, never USER credentials."""

    operation_hash: str
    request_hash: str
    response_json: str
    created_at: datetime


# 历史窗口。两道限制都要：光卡轮数拦不住 3 轮深度分析（每轮 500~800 字）把
# prompt 撑大、首字延迟拉高；光卡字数又会把一整轮切成半句，模型读到的是一句
# 没头没尾的话。
MAX_HISTORY_TURNS = 3
_MAX_HISTORY_CHARS = 6000


@dataclass(frozen=True)
class ConversationTurn:
    """一轮已完成的文字问答。

    只存纯文本，不存工具调用与结果：过期的行情数字留在上下文里会被模型当成
    当前数据用，而那种错比串人更难发现——数字看着合理，只是过期了。
    """

    user_text: str
    assistant_text: str
    created_at: datetime


@dataclass(frozen=True)
class Session:
    session_id: str
    user_id: str
    modality: Modality
    scope: Scope
    state: SessionStateValue
    created_at: datetime
    last_activity_at: datetime
    voice_error_code: str | None = None
    voice_run_stage: str | None = None
    # 本次通话实际使用的语音渠道；握手超时降级后由 /token 改写，
    # start/stop/presence 全部按它路由，避免用新 provider 去停旧任务。
    voice_provider_name: str | None = None
    voice_trace_id: str | None = None
    voice_app_id: str | None = None
    voice_room_id: str | None = None
    voice_user_id: str | None = None
    voice_task_id: str | None = None
    voice_previous_task_ids: tuple[str, ...] = ()
    voice_bot_user_id: str | None = None
    voice_attempt: int = 0
    voice_provider_request_id: str | None = None
    voice_presence_request_id: str | None = None
    voice_callback_trace_id: str | None = None
    voice_callback_auth_version: str | None = None
    voice_error_message: str | None = None
    voice_original_failure_stage: str | None = None
    voice_provider_error_code: str | None = None
    voice_provider_error_message: str | None = None
    voice_first_failed_at: datetime | None = None
    voice_task_submitted_at: datetime | None = None
    voice_start_requested_at: datetime | None = None
    voice_start_accepted_at: datetime | None = None
    voice_mobile_joined_at: datetime | None = None
    voice_bot_joined_at: datetime | None = None
    voice_task_started_at: datetime | None = None
    voice_task_creation_result: str | None = None
    voice_callback_received_at: datetime | None = None
    voice_first_provider_callback_at: datetime | None = None
    voice_callback_schema_hash: str | None = None
    voice_callback_received_count: int = 0
    voice_callback_accepted_count: int = 0
    voice_callback_duplicate_count: int = 0
    voice_callback_stale_count: int = 0
    voice_user_audio_status: Literal["UNKNOWN", "OBSERVED"] = "UNKNOWN"
    voice_asr_started_at: datetime | None = None
    voice_user_speech_started_at: datetime | None = None
    voice_user_speech_ended_at: datetime | None = None
    voice_llm_started_at: datetime | None = None
    voice_llm_completed_at: datetime | None = None
    voice_assistant_first_token_at: datetime | None = None
    voice_function_call_started_at: datetime | None = None
    voice_function_call_completed_at: datetime | None = None
    voice_function_call_failed_at: datetime | None = None
    voice_tts_started_at: datetime | None = None
    voice_tts_first_packet_at: datetime | None = None
    voice_stopped_at: datetime | None = None
    voice_cleanup_status: str | None = None
    voice_user_transcript_at: datetime | None = None
    voice_assistant_transcript_at: datetime | None = None
    voice_provider_audio_started_at: datetime | None = None
    voice_interrupted_at: datetime | None = None
    voice_callback_event_ids: tuple[str, ...] = ()
    voice_tool_results: tuple[tuple[str, str], ...] = ()
    voice_tool_delivered: tuple[str, ...] = ()
    text_turn_results: tuple[TextTurnResult, ...] = ()
    # 最近几轮的文字问答，供下一轮理解指代（"他""该基金""这只"指的是谁）。
    turns: tuple[ConversationTurn, ...] = ()

    def text_turn_result(self, operation_hash: str) -> TextTurnResult | None:
        return next(
            (item for item in self.text_turn_results if item.operation_hash == operation_hash),
            None,
        )

    def with_text_turn_result(self, now: datetime, result: TextTurnResult) -> Session:
        retained = tuple(
            item for item in self.text_turn_results if item.operation_hash != result.operation_hash
        )
        return replace(
            self,
            state="text_active",
            last_activity_at=now,
            text_turn_results=(*retained, result)[-16:],
        )

    def with_conversation_turn(self, now: datetime, turn: ConversationTurn) -> Session:
        """追加一轮历史并按窗口裁剪。

        裁剪顺序是先轮数、再字符。刚追加的那一轮永远保留——丢掉它等于这次
        写入静默失效，而调用方拿不到任何信号。
        """

        recent = (*self.turns, turn)[-MAX_HISTORY_TURNS:]
        newest = recent[-1]
        if len(newest.user_text) + len(newest.assistant_text) > _MAX_HISTORY_CHARS:
            # 最新一轮自己就超预算：截它的答案（问题不截，那是"到底问了什么"），
            # 更老的历史全部让位。
            budget = max(_MAX_HISTORY_CHARS - len(newest.user_text), 0)
            recent = (replace(newest, assistant_text=newest.assistant_text[:budget]),)
        else:
            total = sum(len(t.user_text) + len(t.assistant_text) for t in recent)
            while len(recent) > 1 and total > _MAX_HISTORY_CHARS:
                dropped, recent = recent[0], recent[1:]
                total -= len(dropped.user_text) + len(dropped.assistant_text)
        return replace(
            self,
            state="text_active",
            last_activity_at=now,
            turns=recent,
        )

    def touch(
        self,
        now: datetime,
        state: SessionStateValue | None = None,
        voice_error_code: str | None = None,
    ) -> Session:
        """返回更新了活跃时间（可选状态与错误码）的新会话。"""
        return replace(
            self,
            last_activity_at=now,
            state=state or self.state,
            voice_error_code=(
                voice_error_code if voice_error_code is not None else self.voice_error_code
            ),
        )

    def with_voice_status(
        self,
        now: datetime,
        *,
        state: SessionStateValue,
        run_stage: str | None,
        error_code: str | None,
        error_message: str | None = None,
        provider_request_id: str | None = None,
        task_submitted_at: datetime | None = None,
        start_requested_at: datetime | None = None,
        start_accepted_at: datetime | None = None,
        mobile_joined_at: datetime | None = None,
        bot_joined_at: datetime | None = None,
        task_started_at: datetime | None = None,
        task_creation_result: str | None = None,
        callback_received_at: datetime | None = None,
        user_audio_status: Literal["UNKNOWN", "OBSERVED"] | None = None,
        asr_started_at: datetime | None = None,
        user_speech_started_at: datetime | None = None,
        user_speech_ended_at: datetime | None = None,
        llm_started_at: datetime | None = None,
        llm_completed_at: datetime | None = None,
        assistant_first_token_at: datetime | None = None,
        function_call_started_at: datetime | None = None,
        function_call_completed_at: datetime | None = None,
        function_call_failed_at: datetime | None = None,
        tts_started_at: datetime | None = None,
        tts_first_packet_at: datetime | None = None,
        user_transcript_at: datetime | None = None,
        assistant_transcript_at: datetime | None = None,
        stopped_at: datetime | None = None,
        cleanup_status: str | None = None,
        presence_request_id: str | None = None,
        callback_trace_id: str | None = None,
        provider_error_code: str | None = None,
        provider_error_message: str | None = None,
        callback_event_id: str | None = None,
        provider_audio_started_at: datetime | None = None,
        interrupted_at: datetime | None = None,
    ) -> Session:
        """Apply a provider callback without retaining its untrusted raw payload."""

        is_failure = state in {"provider_failed", "bot_join_timeout"}
        callback_events = self.voice_callback_event_ids
        if callback_event_id is not None and callback_event_id not in callback_events:
            callback_events = (*callback_events, callback_event_id)[-64:]

        return replace(
            self,
            last_activity_at=now,
            state=state,
            voice_run_stage=run_stage if run_stage is not None else self.voice_run_stage,
            voice_error_code=(error_code if error_code is not None else self.voice_error_code),
            voice_error_message=(
                error_message if error_message is not None else self.voice_error_message
            ),
            voice_provider_request_id=(
                provider_request_id
                if provider_request_id is not None
                else self.voice_provider_request_id
            ),
            voice_task_submitted_at=(
                task_submitted_at if task_submitted_at is not None else self.voice_task_submitted_at
            ),
            voice_start_requested_at=(
                start_requested_at
                if start_requested_at is not None
                else self.voice_start_requested_at
            ),
            voice_start_accepted_at=(
                start_accepted_at if start_accepted_at is not None else self.voice_start_accepted_at
            ),
            voice_mobile_joined_at=(
                mobile_joined_at if mobile_joined_at is not None else self.voice_mobile_joined_at
            ),
            voice_bot_joined_at=(
                bot_joined_at if bot_joined_at is not None else self.voice_bot_joined_at
            ),
            voice_task_started_at=(
                task_started_at if task_started_at is not None else self.voice_task_started_at
            ),
            voice_task_creation_result=(
                task_creation_result
                if task_creation_result is not None
                else self.voice_task_creation_result
            ),
            voice_callback_received_at=(
                callback_received_at
                if callback_received_at is not None
                else self.voice_callback_received_at
            ),
            voice_first_provider_callback_at=(
                self.voice_first_provider_callback_at or callback_received_at
            ),
            voice_user_audio_status=(
                user_audio_status if user_audio_status is not None else self.voice_user_audio_status
            ),
            voice_asr_started_at=(
                asr_started_at if asr_started_at is not None else self.voice_asr_started_at
            ),
            voice_user_speech_started_at=(
                user_speech_started_at
                if user_speech_started_at is not None
                else self.voice_user_speech_started_at
            ),
            voice_user_speech_ended_at=(
                user_speech_ended_at
                if user_speech_ended_at is not None
                else self.voice_user_speech_ended_at
            ),
            voice_llm_started_at=(
                llm_started_at if llm_started_at is not None else self.voice_llm_started_at
            ),
            voice_llm_completed_at=(
                llm_completed_at if llm_completed_at is not None else self.voice_llm_completed_at
            ),
            voice_assistant_first_token_at=(
                assistant_first_token_at
                if assistant_first_token_at is not None
                else self.voice_assistant_first_token_at
            ),
            voice_function_call_started_at=(
                function_call_started_at
                if function_call_started_at is not None
                else self.voice_function_call_started_at
            ),
            voice_function_call_completed_at=(
                function_call_completed_at
                if function_call_completed_at is not None
                else self.voice_function_call_completed_at
            ),
            voice_function_call_failed_at=(
                function_call_failed_at
                if function_call_failed_at is not None
                else self.voice_function_call_failed_at
            ),
            voice_tts_started_at=(
                tts_started_at if tts_started_at is not None else self.voice_tts_started_at
            ),
            voice_tts_first_packet_at=(
                tts_first_packet_at
                if tts_first_packet_at is not None
                else self.voice_tts_first_packet_at
            ),
            voice_user_transcript_at=(
                user_transcript_at
                if user_transcript_at is not None
                else self.voice_user_transcript_at
            ),
            voice_assistant_transcript_at=(
                assistant_transcript_at
                if assistant_transcript_at is not None
                else self.voice_assistant_transcript_at
            ),
            voice_stopped_at=(stopped_at if stopped_at is not None else self.voice_stopped_at),
            voice_cleanup_status=(
                cleanup_status if cleanup_status is not None else self.voice_cleanup_status
            ),
            voice_presence_request_id=(
                presence_request_id
                if presence_request_id is not None
                else self.voice_presence_request_id
            ),
            voice_callback_trace_id=(
                callback_trace_id if callback_trace_id is not None else self.voice_callback_trace_id
            ),
            voice_original_failure_stage=(
                self.voice_original_failure_stage or (run_stage if is_failure else None)
            ),
            voice_provider_error_code=(
                self.voice_provider_error_code or (provider_error_code if is_failure else None)
            ),
            voice_provider_error_message=(
                self.voice_provider_error_message
                or (provider_error_message if is_failure else None)
            ),
            voice_first_failed_at=(self.voice_first_failed_at or (now if is_failure else None)),
            voice_provider_audio_started_at=(
                provider_audio_started_at
                if provider_audio_started_at is not None
                else self.voice_provider_audio_started_at
            ),
            voice_interrupted_at=(
                interrupted_at if interrupted_at is not None else self.voice_interrupted_at
            ),
            voice_callback_event_ids=callback_events,
        )

    def voice_tool_result(self, tool_call_id: str) -> str | None:
        return dict(self.voice_tool_results).get(tool_call_id)

    def with_voice_tool_result(self, now: datetime, tool_call_id: str, content: str) -> Session:
        results = dict(self.voice_tool_results)
        results[tool_call_id] = content
        return replace(
            self,
            last_activity_at=now,
            voice_tool_results=tuple(results.items()),
        )

    def with_voice_tool_delivered(self, now: datetime, tool_call_id: str) -> Session:
        delivered = tuple(dict.fromkeys((*self.voice_tool_delivered, tool_call_id)))
        return replace(self, last_activity_at=now, voice_tool_delivered=delivered)
