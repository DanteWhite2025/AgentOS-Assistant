"""控制面 REST 端点（真实逻辑）。

契约见 docs/api/agent-mobile/openapi.agent.yaml。前缀 /v1/agent 由 main.py 挂载。

打向说明（见 README §0）：
- sessions / token / messages / history → USER Bearer Token 经 Core actor-context 验证
- tool-calls → App 直连本服务（rt_token 鉴权）；落地"白名单二次校验"红线
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, cast

import httpx
from fastapi import APIRouter, Depends, File, Header, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response

from app.api import schemas
from app.api.deps import (
    bearer_token,
    get_authenticated_user,
    get_container,
    owned_session,
)
from app.api.errors import (
    _map_upstream_error,
    core_http_tool_error,
    error_response,
    market_data_error_response,
    market_data_tool_error,
    market_resolution_error_response,
    market_resolution_tool_error,
    new_trace_id,
)
from app.api.tool_execution import execute_controlled_tool
from app.audit.hashing import args_hash
from app.audit.store import AuditRecord
from app.auth.core_user import AuthenticatedUser
from app.container import Container, VoiceProviderUnavailableError
from app.history.models import ConversationSummary
from app.history.transcript import pair_transcript
from app.runtime.session import (
    MAX_HISTORY_TURNS,
    ConversationTurn,
    Scope,
    Session,
    TextTurnResult,
)
from app.tools.agentos.trade_flow import (
    IntentParseError,
    MarketInstrumentResolutionError,
    StrategyBindingError,
    TradingReadinessError,
    create_draft_from_text,
    parse_trading_readiness,
    require_draft_ready,
    resolve_strategy_execution_context,
)
from app.tools.core_client import MarketDataError
from app.tools.executor import ToolNotAllowedError
from app.voice.callback_auth import CALLBACK_AUTH_VERSION, derive_callback_signature
from app.voice.gemini_live import GeminiLiveProviderError
from app.voice.volc_asr import VolcASRError, VolcASRRetryableError
from app.voice.volc_callbacks import (
    MAX_CALLBACK_BYTES,
    VolcCallbackSchemaError,
    parse_function_callback,
    parse_state_callback,
)
from app.voice.volc_callbacks import (
    callback_value as _callback_value,
)
from app.voice.volc_openapi import VolcOpenAPIError

logger = logging.getLogger(__name__)

_CALLBACK_FAILURE_STAGES = {"failed", "error", "taskfailed", "taskerror"}
_CALLBACK_STOP_STAGES = {"taskstop", "stopped", "taskstopped"}
_CALLBACK_TASK_START_STAGES = {"taskstart", "taskstarted"}
_CALLBACK_USER_AUDIO_STAGES = {
    "inputaudioreceived",
    "inputaudiostart",
    "useraudioreceived",
}
_CALLBACK_ASR_START_STAGES = {
    "asrstart",
    "asrstarted",
    "listening",
    "speechstart",
}
_CALLBACK_USER_SPEECH_START_STAGES = {"speechstart", "userspeechstart", "vadstart"}
_CALLBACK_USER_SPEECH_END_STAGES = {"speechend", "userspeechend", "vadend"}
_CALLBACK_USER_TRANSCRIPT_STAGES = {
    "asrcompleted",
    "asrend",
    "asrfinal",
    "asrresult",
    "usertranscript",
    "usertranscriptcompleted",
}
_CALLBACK_LLM_START_STAGES = {
    "llmstart",
    "llmstarted",
    "llmoutput",
    "reasoningstart",
    "thinking",
}
_CALLBACK_LLM_COMPLETE_STAGES = {
    "answerfinish",
    "llmcomplete",
    "llmcompleted",
    "llmend",
    "llmfinish",
}
_CALLBACK_FIRST_TOKEN_STAGES = {"llmoutput"}
_CALLBACK_TTS_START_STAGES = {
    "answering",
    "answerstart",
    "ttsstart",
    "ttsstarted",
    "ttsoutput",
}
_CALLBACK_ASSISTANT_TRANSCRIPT_STAGES = {
    "assistanttranscript",
    "assistanttranscriptcompleted",
    "ttstranscript",
}
_CALLBACK_AUDIO_STAGES = {"answering", "answerstart", "audiostart", "ttsoutput"}
_CALLBACK_INTERRUPT_STAGES = {"interrupt", "interrupted", "userinterrupt"}
_CALLBACK_MAX_BYTES = MAX_CALLBACK_BYTES
_AGENTOS_DRAFT_TOOL = "agentos_create_trade_draft"

router = APIRouter(tags=["agent"])

_ERR = {"model": schemas.ErrorEnvelope}
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
Ctn = Annotated[Container, Depends(get_container)]
User = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_id(value: str | None) -> str:
    if not value:
        return "none"
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _text_turn_identity(
    session: Session,
    user: AuthenticatedUser,
    body: schemas.SingleTurnRequest,
    idempotency_key: str | None,
) -> tuple[str, str, str]:
    request_json = json.dumps(
        {
            "session_id": body.session_id,
            "text": body.text,
            "web_search": body.web_search,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    request_hash = hashlib.sha256(request_json.encode()).hexdigest()
    supplied = idempotency_key.strip() if idempotency_key is not None else ""
    if supplied and len(supplied) > 256:
        raise ValueError("Idempotency-Key 长度不能超过 256")
    stable_key = supplied or f"auto_{request_hash}"
    operation_hash = hashlib.sha256(
        f"{user.user_id}\0{session.session_id}\0{stable_key}".encode()
    ).hexdigest()
    return operation_hash, request_hash, stable_key


def _cached_text_turn(
    session: Session,
    operation_hash: str,
    request_hash: str,
) -> schemas.SingleTurnResponse | None:
    cached = session.text_turn_result(operation_hash)
    if cached is None:
        return None
    if not hmac.compare_digest(cached.request_hash, request_hash):
        raise ValueError("Idempotency-Key 已用于不同消息")
    try:
        return schemas.SingleTurnResponse.model_validate_json(cached.response_json)
    except ValueError as exc:
        # Never re-run a possibly completed Draft when its local recovery record
        # is corrupt. Core remains authoritative and the operation must be audited.
        raise RuntimeError("已完成消息的恢复记录不可用") from exc


def _safe_callback_identifier(value: object) -> str | None:
    if value is None:
        return None
    identifier = str(value)[:128]
    if re.fullmatch(r"[A-Za-z0-9_.:-]+", identifier):
        return identifier
    return f"hash:{_safe_id(identifier)}"


def _provider_error(exc: Exception) -> tuple[str, str]:
    message = _safe_callback_message(str(exc)) or type(exc).__name__
    code = message.split(":", 1)[0][:128] if ":" in message else type(exc).__name__
    return code, message


def _expected_callback_signature(
    session: Session, master_secret: str, *, purpose: Literal["state", "function"]
) -> str:
    if session.voice_callback_auth_version == CALLBACK_AUTH_VERSION:
        return derive_callback_signature(
            master_secret,
            purpose=purpose,
            session_id=session.session_id,
            task_id=session.voice_task_id or "",
        )
    # Backward compatibility is limited to tasks persisted before the derived
    # signature rollout. New tasks always set voice_callback_auth_version.
    return master_secret


def _callback_signature_status(
    session: Session,
    supplied: object,
    master_secret: str,
    *,
    purpose: Literal["state", "function"],
) -> Literal["current", "stale", "invalid"]:
    if not isinstance(supplied, str):
        return "invalid"
    expected = _expected_callback_signature(session, master_secret, purpose=purpose)
    if hmac.compare_digest(supplied, expected):
        return "current"
    if session.voice_callback_auth_version != CALLBACK_AUTH_VERSION:
        return "invalid"
    for task_id in session.voice_previous_task_ids[-4:]:
        stale = derive_callback_signature(
            master_secret,
            purpose=purpose,
            session_id=session.session_id,
            task_id=task_id,
        )
        if hmac.compare_digest(supplied, stale):
            return "stale"
    return "invalid"


def _log_volc_callback(
    session: Session,
    *,
    run_stage: str | None,
    error_code: str | None,
    error_message: str | None,
    callback_trace_id: str | None,
) -> None:
    event = {
        "session_id": session.session_id,
        "task_hash": _safe_id(session.voice_task_id),
        "room_hash": _safe_id(session.voice_room_id),
        "run_stage": run_stage,
        "error_code": error_code,
        "error_message": error_message,
        "callback_trace_id": callback_trace_id,
        "start_request_id": session.voice_provider_request_id,
    }
    logger.info(
        "volc_callback event=%s",
        json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
    )


async def _monitor_asr_silence(container: Container, session_id: str, attempt: int) -> None:
    try:
        await asyncio.sleep(container.settings.volc_asr_silence_timeout_seconds)
        session = await container.session_store.get(session_id)
        if (
            session is None
            or session.voice_attempt != attempt
            or session.state != "voice_ready"
            or session.voice_bot_joined_at is None
            or session.voice_provider_audio_started_at is None
            or session.voice_asr_started_at is not None
            or session.voice_user_transcript_at is not None
        ):
            return
        now = _now()
        session = replace(
            session,
            last_activity_at=now,
            voice_error_code="VOICE_UPLINK_OR_ASR_SILENT",
            voice_error_message="未检测到语音，请检查麦克风后重试。",
            voice_original_failure_stage=(
                session.voice_original_failure_stage or "waiting_for_uplink_or_asr"
            ),
            voice_first_failed_at=session.voice_first_failed_at or now,
        )
        await container.session_store.update(session)
        logger.warning(
            "voice_uplink_or_asr_silent trace_id=%s session_id=%s task_hash=%s "
            "room_hash=%s user_audio_status=%s",
            session.voice_trace_id,
            session.session_id,
            _safe_id(session.voice_task_id),
            _safe_id(session.voice_room_id),
            session.voice_user_audio_status,
        )
    finally:
        current = asyncio.current_task()
        if container.voice_silence_tasks.get(session_id) is current:
            container.voice_silence_tasks.pop(session_id, None)


def _schedule_asr_silence_monitor(container: Container, session_id: str, attempt: int) -> None:
    old = container.voice_silence_tasks.pop(session_id, None)
    if old is not None:
        old.cancel()
    task = asyncio.create_task(
        _monitor_asr_silence(container, session_id, attempt),
        name=f"voice-asr-silence:{session_id}:{attempt}",
    )
    container.voice_silence_tasks[session_id] = task


def _cancel_asr_silence_monitor(container: Container, session_id: str) -> None:
    task = container.voice_silence_tasks.pop(session_id, None)
    if task is not None:
        task.cancel()


async def _record_function_call_stage(
    container: Container,
    session: Session,
    stage: Literal["started", "completed", "failed"],
) -> Session:
    now = _now()
    if stage == "started":
        session = replace(session, last_activity_at=now, voice_function_call_started_at=now)
    elif stage == "completed":
        session = replace(session, last_activity_at=now, voice_function_call_completed_at=now)
    else:
        session = replace(session, last_activity_at=now, voice_function_call_failed_at=now)
    await container.session_store.update(session)
    return session


async def _delayed_function_progress(
    container: Container,
    session: Session,
    tool_call_id: str,
) -> None:
    """Play a neutral status phrase only when a tool call remains slow."""

    try:
        await asyncio.sleep(container.settings.volc_function_progress_delay_seconds)
        current = await container.session_store.get(session.session_id)
        if (
            current is None
            or current.state in {"voice_stopped", "closed"}
            or current.voice_attempt != session.voice_attempt
            or current.voice_task_id != session.voice_task_id
            or tool_call_id in current.voice_tool_delivered
        ):
            return
        update = await _session_provider(container, current).send_progress_message(
            current.session_id,
            current.user_id,
            "正在查询，请稍候。",
            current.voice_attempt,
        )
        logger.info(
            "volc_function_progress trace_id=%s request_id=%s session_id=%s "
            "task_hash=%s tool_call_hash=%s status=sent",
            current.voice_trace_id,
            update.provider_request_id,
            current.session_id,
            _safe_id(current.voice_task_id),
            _safe_id(tool_call_id),
        )
    except asyncio.CancelledError:
        raise
    except (httpx.HTTPError, VolcOpenAPIError) as exc:
        logger.warning(
            "volc_function_progress trace_id=%s session_id=%s task_hash=%s "
            "tool_call_hash=%s status=failed error_type=%s",
            session.voice_trace_id,
            session.session_id,
            _safe_id(session.voice_task_id),
            _safe_id(tool_call_id),
            type(exc).__name__,
        )


def _bot_presence_poll_delay(configured: float, poll_index: int) -> float:
    """Bounded fast-backoff schedule; at most 2 polls/s per active attempt."""

    if configured != 1.0:
        # Preserve explicit operator overrides and focused-test settings.
        return configured
    return (0.5, 0.75, 1.0, 1.5)[min(poll_index, 3)]


async def _monitor_bot_join(container: Container, session_id: str, attempt: int) -> None:
    """Poll authoritative RTC room membership until this attempt's Bot joins."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + container.settings.volc_bot_join_timeout_seconds
    last_request_id: str | None = None
    last_error: str | None = None
    try:
        session = await container.session_store.get(session_id)
        if session is None or session.voice_attempt != attempt:
            return
        if session.voice_task_submitted_at is not None:
            elapsed = max(0.0, (_now() - session.voice_task_submitted_at).total_seconds())
            deadline = loop.time() + max(
                0.0, container.settings.volc_bot_join_timeout_seconds - elapsed
            )
        if session.state == "task_accepted":
            await container.session_store.update(
                session.with_voice_status(
                    _now(), state="bot_joining", run_stage="bot_joining", error_code=None
                )
            )

        poll_index = 0
        while loop.time() < deadline:
            session = await container.session_store.get(session_id)
            if session is None or session.voice_attempt != attempt:
                return
            if session.state in {
                "voice_ready",
                "provider_failed",
                "bot_join_timeout",
                "voice_stopped",
                "closed",
            }:
                return
            try:
                presence = await _session_provider(container, session).presence(
                    session.session_id, session.user_id, attempt
                )
                last_request_id = presence.provider_request_id or last_request_id

                logger.info(
                    "voice_presence_polled trace_id=%s session_id=%s room_hash=%s bot_joined=%s online_users=[%s]",
                    session.voice_trace_id,
                    session.session_id,
                    _safe_id(session.voice_room_id),
                    presence.bot_joined,
                    ",".join(sorted(_safe_id(u) for u in presence.online_user_ids)),
                )

                if (
                    session.voice_mobile_joined_at is None
                    and session.voice_user_id in presence.online_user_ids
                ):
                    mobile_joined_at = _now()
                    session = session.with_voice_status(
                        mobile_joined_at,
                        state=session.state,
                        run_stage=session.voice_run_stage,
                        error_code=session.voice_error_code,
                        mobile_joined_at=mobile_joined_at,
                    )
                    await container.session_store.update(session)

                if presence.bot_joined:
                    now = _now()
                    session = session.with_voice_status(
                        now,
                        state="voice_ready",
                        run_stage="bot_joined",
                        error_code=None,
                        error_message=None,
                        presence_request_id=last_request_id,
                        bot_joined_at=now,
                    )
                    await container.session_store.update(session)
                    logger.info(
                        "voice_bot_joined trace_id=%s session_id=%s task_hash=%s "
                        "room_hash=%s mobile_user_hash=%s bot_user_hash=%s joined_at=%s",
                        session.voice_trace_id,
                        session.session_id,
                        _safe_id(session.voice_task_id),
                        _safe_id(session.voice_room_id),
                        _safe_id(session.voice_user_id),
                        _safe_id(session.voice_bot_user_id),
                        now.isoformat(),
                    )
                    if session.voice_provider_audio_started_at is not None:
                        _schedule_asr_silence_monitor(container, session.session_id, attempt)
                    return
            except VolcOpenAPIError as exc:
                code, message = _provider_error(exc)
                cleanup_status = "success"
                stopped_at: datetime | None = None
                try:
                    await _session_provider(container, session).stop(
                        session.session_id, session.user_id, attempt
                    )
                    stopped_at = _now()
                except (httpx.HTTPError, VolcOpenAPIError):
                    cleanup_status = "failed"
                session = session.with_voice_status(
                    _now(),
                    state="provider_failed",
                    run_stage="presence_check",
                    error_code="VOICE_PROVIDER_FAILED",
                    error_message="连接失败，请重试",
                    provider_error_code=code,
                    provider_error_message=message,
                    presence_request_id=last_request_id,
                    stopped_at=stopped_at,
                    cleanup_status=cleanup_status,
                )
                await container.session_store.update(session)
                logger.warning(
                    "voice_presence_provider_failed trace_id=%s session_id=%s task_hash=%s "
                    "error_code=%s error_message=%s",
                    session.voice_trace_id,
                    session.session_id,
                    _safe_id(session.voice_task_id),
                    code,
                    message,
                )
                return
            except httpx.HTTPError as exc:
                _, last_error = _provider_error(exc)
            delay = _bot_presence_poll_delay(
                container.settings.volc_bot_presence_poll_seconds, poll_index
            )
            poll_index += 1
            await asyncio.sleep(min(delay, max(0.0, deadline - loop.time())))

        session = await container.session_store.get(session_id)
        if session is None or session.voice_attempt != attempt or session.state == "voice_ready":
            return
        stop_error: str | None = None
        cleanup_status = "success"
        try:
            await _session_provider(container, session).stop(
                session.session_id, session.user_id, attempt
            )
        except (httpx.HTTPError, VolcOpenAPIError) as exc:
            _, stop_error = _provider_error(exc)
            cleanup_status = "failed"
        stopped_at = _now() if cleanup_status == "success" else None
        session = session.with_voice_status(
            _now(),
            state="bot_join_timeout",
            run_stage="bot_join_timeout",
            error_code="VOICE_BOT_NOT_JOINED",
            error_message="连接失败，请重试",
            provider_error_code=("VOICE_STOP_FAILED" if stop_error else None),
            provider_error_message=(
                stop_error or last_error or "Bot did not appear in the RTC room before timeout"
            ),
            presence_request_id=last_request_id,
            stopped_at=stopped_at,
            cleanup_status=cleanup_status,
        )
        await container.session_store.update(session)
        logger.warning(
            "voice_bot_join_timeout trace_id=%s session_id=%s task_hash=%s room_hash=%s "
            "mobile_user_hash=%s bot_user_hash=%s cleanup_status=%s",
            session.voice_trace_id,
            session.session_id,
            _safe_id(session.voice_task_id),
            _safe_id(session.voice_room_id),
            _safe_id(session.voice_user_id),
            _safe_id(session.voice_bot_user_id),
            cleanup_status,
        )
    finally:
        current = asyncio.current_task()
        if container.voice_monitor_tasks.get(session_id) is current:
            container.voice_monitor_tasks.pop(session_id, None)


def _schedule_bot_monitor(
    container: Container, session_id: str, attempt: int, session: Session | None = None
) -> None:
    # 客户端直连渠道没有 Bot 可等；跑监控只会在超时后误判成连接失败。
    if session is not None and not _needs_bot_presence(container, session):
        return
    old = container.voice_monitor_tasks.pop(session_id, None)
    if old is not None:
        old.cancel()
    task = asyncio.create_task(
        _monitor_bot_join(container, session_id, attempt),
        name=f"voice-bot-join:{session_id}:{attempt}",
    )
    container.voice_monitor_tasks[session_id] = task


def _safe_callback_message(value: object, *, secrets: tuple[str, ...] = ()) -> str | None:
    if value is None:
        return None
    message = " ".join(str(value).split())[:256]
    message = re.sub(
        r"(?i)(authorization|api[_-]?key|token|secret)\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        message,
    )
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message


def _needs_bot_presence(container: Container, session: Session) -> bool:
    """本会话的渠道是否有服务端 Bot 需要等进房。

    缺省 True：未声明该属性的实现按火山那套保守处理，不会因为漏声明就
    跳掉真正需要的清理链路。
    """

    return bool(getattr(_session_provider(container, session), "requires_bot_presence", True))


# 流水线渠道下，客户端来领直连凭证是用错了端点，不是故障。给一句能指路的
# 文案，比让它变成 500 好排查得多。
_NO_DIRECT_VOICE_LINK = (
    "当前语音渠道不签发直连凭证：音频由服务端合成后经 WebSocket 下发，"
    "请改用 WS 的 audio.chunk 事件播放"
)


def _issues_credentials(provider: object) -> bool:
    """这个渠道是否需要给客户端签票。

    缺省 True：未声明的实现按「客户端直连、必须签票」处理，漏声明不会静默
    跳掉真正需要的票。
    """

    return bool(getattr(provider, "issues_credentials", True))


def _session_provider(container: Container, session: Session):
    """按会话记录的渠道取 Provider —— 降级后 start/stop/presence 必须跟着走。"""

    try:
        return container.voice_provider_for(session.voice_provider_name)
    except VoiceProviderUnavailableError:
        # 渠道曾经装配成功才会被写进会话；此处兜底回主力，避免清理链路卡死。
        return container.voice_provider


def _voice_schema(cred) -> schemas.VoiceCredentials:
    return schemas.VoiceCredentials(
        provider=cred.provider,
        endpoint_url=cred.endpoint_url,
        expires_at=cred.expires_at,
        allowed_tools=list(cred.allowed_tools),
        client_secret=cred.client_secret,
        client_secret_expires_at=cred.expires_at if cred.client_secret else None,
        new_session_expires_at=cred.new_session_expires_at,
        model=cred.model,
        call_url=cred.call_url,
        app_id=cred.app_id,
        room_id=cred.room_id,
        user_id=cred.user_id,
        token=cred.token,
        token_expires_at=cred.expires_at if cred.token else None,
        task_id=cred.task_id,
        bot_user_id=cred.bot_user_id,
    )


def _gemini_provider_error_response(exc: GeminiLiveProviderError) -> JSONResponse:
    """Gemini 控制面错误不能变成用户登录 401，也不能泄露上游响应。"""

    upstream_status = exc.upstream_status
    if upstream_status == 409:
        code: schemas.ErrorCode = "IDEMPOTENCY_CONFLICT"
        status_code = 409
        message = "语音凭证正在处理，请重试"
    elif upstream_status == 429:
        code = "RATE_LIMITED"
        status_code = 429
        message = "语音服务请求频率受限，请稍后重试"
    else:
        code = "INTERNAL"
        status_code = 503
        message = "智能体语音凭证暂不可用"
    return error_response(
        code,
        message,
        status_code,
        details={
            "upstream_code": exc.reason,
            "upstream_status": upstream_status,
            "retryable": exc.retryable,
            # 这是 Runtime 的 Provider 凭证，不是 Mobile 的 Core USER Token。
            "credential_refresh_required": False,
            "provider_credential_refresh_required": upstream_status == 401,
        },
    )


async def _owned_session(
    session_id: str, user: AuthenticatedUser, container: Container
) -> Session | None:
    return await owned_session(session_id, user, container)


# 移动端端侧识别对中文和币圈专有名词（BTC、滑点、做多做空……）识别率差，
# 短语音统一改走服务端调火山豆包 Seed ASR Flash。10MB 覆盖住几十秒的短语音，
# 更长的录音应该走流式接口而不是整段上传。
_AUDIO_TRANSCRIBE_MAX_BYTES = 10 * 1024 * 1024
_AUDIO_TRANSCRIBE_FORMATS = {
    ".m4a": "m4a",
    ".wav": "wav",
    ".mp3": "mp3",
    ".aac": "aac",
    ".ogg": "ogg",
    ".pcm": "pcm",
}


@router.post(
    "/audio/transcribe",
    response_model=schemas.AudioTranscribeResponse,
    summary="上传短语音，返回火山豆包 Seed ASR Flash 识别出的文字",
    responses={400: _ERR, 401: _ERR, 502: _ERR},
)
async def transcribe_audio(
    container: Ctn,
    user: User,
    file: Annotated[UploadFile, File(description="音频文件，≤10MB")],
):
    trace_id = new_trace_id()
    suffix = Path(file.filename or "").suffix.lower()
    audio_format = _AUDIO_TRANSCRIBE_FORMATS.get(suffix)
    if audio_format is None:
        return error_response(
            "INVALID_REQUEST",
            f"不支持的音频格式：{suffix or '(未知)'}",
            400,
            trace_id=trace_id,
        )

    audio_bytes = await file.read()
    if not audio_bytes:
        return error_response("INVALID_REQUEST", "音频文件为空", 400, trace_id=trace_id)
    if len(audio_bytes) > _AUDIO_TRANSCRIBE_MAX_BYTES:
        return error_response("INVALID_REQUEST", "音频文件超过 10MB 限制", 400, trace_id=trace_id)

    asr_client = container.asr_client
    if asr_client is None:
        logger.warning("audio_transcribe_asr_client_unconfigured user_id=%s", _safe_id(user.user_id))
        return error_response("INTERNAL", "语音识别服务暂不可用，请稍后重试", 502, trace_id=trace_id)

    attempt = 0
    while True:
        try:
            result = await asr_client.transcribe(audio_bytes, audio_format=audio_format)
            break
        except VolcASRError as exc:
            retryable = isinstance(exc, VolcASRRetryableError)
            if retryable and attempt < 1:
                attempt += 1
                continue
            # 只记异常类型和是否重试过，绝不记 exc 本身——它可能带回声的 Key。
            logger.warning(
                "audio_transcribe_upstream_failed retryable=%s attempt=%d error_type=%s",
                retryable,
                attempt,
                type(exc).__name__,
            )
            return error_response(
                "INTERNAL", "语音识别服务暂不可用，请稍后重试", 502, trace_id=trace_id
            )

    return schemas.AudioTranscribeResponse(
        transcript=result.text,
        duration_ms=result.duration_ms,
        trace_id=trace_id,
    )


@router.post(
    "/sessions",
    response_model=schemas.SessionCredentials,
    status_code=status.HTTP_201_CREATED,
    summary="创建 Agent 会话，返回凭证包与实时通道 endpoints",
    responses={401: _ERR, 403: _ERR, 429: _ERR, 503: _ERR},
)
async def create_session(
    body: schemas.SessionCreateRequest,
    container: Ctn,
    user: User,
    idempotency_key: IdempotencyKey = None,
) -> schemas.SessionCredentials | JSONResponse:
    now = _now()
    session_id = "sess_" + uuid.uuid4().hex
    trace_id = new_trace_id()
    scope = Scope(agents=("avatar", "strategy"), tools=container.registry.names())

    voice = None
    cred = None
    # 流水线渠道没有票可签：音频由 Runtime 合成后经 WebSocket 下发，客户端不
    # 直连第三方。这里必须按能力跳过，不能让 issue_credentials 抛出去 ——
    # App 的入口大多用 voice/both，一炸就是整个进不去，而不只是没声音。
    if body.modality in ("voice", "both") and _issues_credentials(container.voice_provider):
        # 文字 → 语音衔接：把最近的画像与情景记忆（含上一通电话摘要）注入
        # Realtime Session，接通即可接着前面的聊天说，不用用户重述一遍。
        try:
            cred = await container.voice_provider.issue_credentials(
                session_id,
                user.user_id,
                scope.tools,
                container.settings.voice_model,
                now,
                context_summary=await container.context_builder.build_voice_context(user.user_id),
            )
        except GeminiLiveProviderError as exc:
            return _gemini_provider_error_response(exc)
        voice = _voice_schema(cred)

    session = Session(
        session_id=session_id,
        user_id=user.user_id,
        modality=body.modality,
        scope=scope,
        state="created",
        created_at=now,
        last_activity_at=now,
        voice_trace_id=trace_id if cred is not None else None,
        voice_app_id=cred.app_id if cred is not None else None,
        voice_room_id=cred.room_id if cred is not None else None,
        voice_user_id=cred.user_id if cred is not None else None,
        voice_task_id=cred.task_id if cred is not None else None,
        voice_bot_user_id=cred.bot_user_id if cred is not None else None,
        voice_provider_name=(
            container.settings.realtime_voice_provider if cred is not None else None
        ),
    )
    await container.session_store.create(session)
    if cred is not None:
        logger.info(
            "voice_session_created trace_id=%s session_id=%s room_hash=%s "
            "mobile_user_hash=%s bot_user_hash=%s task_hash=%s",
            trace_id,
            session_id,
            _safe_id(session.voice_room_id),
            _safe_id(session.voice_user_id),
            _safe_id(session.voice_bot_user_id),
            _safe_id(session.voice_task_id),
        )
    return _session_credentials(container, session, scope, voice, trace_id, now)


def _session_credentials(
    container: Ctn,
    session: Session,
    scope: Scope,
    voice: schemas.VoiceCredentials | None,
    trace_id: str,
    now: datetime,
) -> schemas.SessionCredentials:
    """签发凭证包。create_session 与 resume_conversation 共用这一份。

    抽出来的理由是防漂移：resume 必须返回与创建**完全一致**的结构，客户端
    才能复用既有处理逻辑；两处各写一遍，改一处漏一处就会在真机上表现成
    「从历史进去连不上 WS」。
    """

    rt_token, rt_exp = container.token_service.issue_rt_token(
        session.session_id, session.user_id, scope.tools, now
    )
    return schemas.SessionCredentials(
        session_id=session.session_id,
        rt_token=rt_token,
        rt_token_expires_at=rt_exp,
        ws_url=(
            f"{container.settings.rt_ws_base}/v1/agent/stream?session_id={session.session_id}"
        ),
        voice=voice,
        scope=schemas.AgentScope(
            agents=cast(list[schemas.AgentName], list(scope.agents)),
            tools=list(scope.tools),
        ),
        trace_id=trace_id,
    )


@router.get(
    "/sessions/{session_id}",
    response_model=schemas.SessionState,
    summary="查询会话状态（WS 重连后补齐状态用）",
    responses={401: _ERR, 403: _ERR, 404: _ERR, 503: _ERR},
)
async def get_session(session_id: str, container: Ctn, user: User):
    session = await _owned_session(session_id, user, container)
    if session is None:
        return error_response("SESSION_NOT_FOUND", "会话不存在", 404)
    if session.state in {"task_accepted", "bot_joining"} and session_id not in (
        container.voice_monitor_tasks
    ):
        # Recover monitoring after a process restart when Mobile resumes polling.
        _schedule_bot_monitor(container, session.session_id, session.voice_attempt, session)
    if (
        session.state == "voice_ready"
        and session.voice_bot_joined_at is not None
        and session.voice_provider_audio_started_at is not None
        and session.voice_asr_started_at is None
        and session.voice_user_transcript_at is None
        and session.voice_error_code != "VOICE_UPLINK_OR_ASR_SILENT"
        and session_id not in container.voice_silence_tasks
    ):
        _schedule_asr_silence_monitor(container, session.session_id, session.voice_attempt)
    return schemas.SessionState(
        session_id=session.session_id,
        state=session.state,
        modality=session.modality,
        created_at=session.created_at,
        last_activity_at=session.last_activity_at,
        scope=schemas.AgentScope(
            agents=cast(list[schemas.AgentName], list(session.scope.agents)),
            tools=list(session.scope.tools),
        ),
        voice_error_code=session.voice_error_code,
        voice_error_message=session.voice_error_message,
        voice_run_stage=session.voice_run_stage,
        voice_provider_request_id=session.voice_provider_request_id,
        voice_task_submitted_at=session.voice_task_submitted_at,
        voice_bot_joined_at=session.voice_bot_joined_at,
        voice_task_creation_result=session.voice_task_creation_result,
        voice_callback_received_at=session.voice_callback_received_at,
        voice_stopped_at=session.voice_stopped_at,
        voice_cleanup_status=session.voice_cleanup_status,
        voice_user_transcript_at=session.voice_user_transcript_at,
        voice_assistant_transcript_at=session.voice_assistant_transcript_at,
        voice_provider_audio_started_at=session.voice_provider_audio_started_at,
        voice_interrupted_at=session.voice_interrupted_at,
        voice_diagnostics=(
            schemas.VoiceDiagnostics(
                original_failure_stage=session.voice_original_failure_stage,
                provider_error_code=session.voice_provider_error_code,
                provider_error_message=session.voice_provider_error_message,
                first_failed_at=session.voice_first_failed_at,
                start_request_id=session.voice_provider_request_id,
                presence_request_id=session.voice_presence_request_id,
                callback_trace_id=session.voice_callback_trace_id,
                room_hash=_safe_id(session.voice_room_id),
                mobile_user_hash=_safe_id(session.voice_user_id),
                bot_user_hash=_safe_id(session.voice_bot_user_id),
                task_hash=_safe_id(session.voice_task_id),
                start_requested_at=session.voice_start_requested_at,
                start_accepted_at=session.voice_start_accepted_at,
                mobile_joined_at=session.voice_mobile_joined_at,
                bot_joined_at=session.voice_bot_joined_at,
                task_started_at=session.voice_task_started_at,
                first_provider_callback_at=session.voice_first_provider_callback_at,
                user_audio_status=session.voice_user_audio_status,
                asr_started_at=session.voice_asr_started_at,
                user_speech_started_at=session.voice_user_speech_started_at,
                user_speech_ended_at=session.voice_user_speech_ended_at,
                first_user_transcript_at=session.voice_user_transcript_at,
                llm_started_at=session.voice_llm_started_at,
                llm_completed_at=session.voice_llm_completed_at,
                assistant_first_token_at=session.voice_assistant_first_token_at,
                function_call_started_at=session.voice_function_call_started_at,
                function_call_completed_at=session.voice_function_call_completed_at,
                function_call_failed_at=session.voice_function_call_failed_at,
                tts_started_at=session.voice_tts_started_at,
                tts_first_packet_at=session.voice_tts_first_packet_at,
                first_assistant_transcript_at=session.voice_assistant_transcript_at,
                provider_audio_started_at=session.voice_provider_audio_started_at,
                interrupted_at=session.voice_interrupted_at,
                stopped_at=session.voice_stopped_at,
                cleanup_status=session.voice_cleanup_status,
                callback_schema_hash=session.voice_callback_schema_hash,
                callback_received_total=session.voice_callback_received_count,
                callback_accepted_total=session.voice_callback_accepted_count,
                callback_duplicate_total=session.voice_callback_duplicate_count,
                callback_stale_total=session.voice_callback_stale_count,
            )
            if session.modality in {"voice", "both"}
            else None
        ),
    )


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="结束会话",
    responses={401: _ERR, 403: _ERR, 404: _ERR, 503: _ERR},
)
async def close_session(session_id: str, container: Ctn, user: User):
    session = await _owned_session(session_id, user, container)
    if session is None:
        return error_response("SESSION_NOT_FOUND", "会话不存在", 404)
    if session.state == "voice_stopped" and session.voice_cleanup_status == "success":
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    monitor = container.voice_monitor_tasks.pop(session_id, None)
    if monitor is not None:
        monitor.cancel()
    _cancel_asr_silence_monitor(container, session_id)
    already_cleaned = (
        session.voice_cleanup_status == "success" and session.voice_stopped_at is not None
    )
    if (
        not already_cleaned
        and session.voice_task_id
        and (
            session.voice_run_stage is not None
            or session.state
            in {
                "voice_starting",
                "task_accepted",
                "bot_joining",
                "voice_ready",
                "bot_join_timeout",
                "provider_failed",
            }
        )
    ):
        try:
            await _session_provider(container, session).stop(
                session.session_id, session.user_id, session.voice_attempt
            )
        except (httpx.HTTPError, VolcOpenAPIError) as exc:
            failed = session.with_voice_status(
                _now(),
                state=session.state,
                run_stage=session.voice_run_stage,
                error_code=session.voice_error_code,
                error_message=session.voice_error_message,
                cleanup_status="failed",
            )
            await container.session_store.update(failed)
            return error_response(
                "INTERNAL",
                "火山 RTC 语音任务停止失败；会话已保留，可安全重试关闭",
                502,
                details={"upstream": type(exc).__name__},
            )
    stopped_at = _now()
    session = session.with_voice_status(
        stopped_at,
        state="voice_stopped",
        run_stage=session.voice_run_stage or "voice_stopped",
        error_code=session.voice_error_code,
        error_message=session.voice_error_message,
        stopped_at=stopped_at,
        cleanup_status="success",
    )
    await container.session_store.update(session)
    logger.info(
        "voice_stopped trace_id=%s session_id=%s task_hash=%s room_hash=%s "
        "final_state=%s cleanup_status=%s",
        session.voice_trace_id,
        session.session_id,
        _safe_id(session.voice_task_id),
        _safe_id(session.voice_room_id),
        session.state,
        session.voice_cleanup_status,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/sessions/{session_id}/voice/start",
    response_model=schemas.VoiceStartResponse,
    summary="客户端成功加入 RTC 房间后启动火山 AI 对话任务",
    responses={400: _ERR, 401: _ERR, 404: _ERR, 502: _ERR, 503: _ERR},
)
async def start_voice_chat(session_id: str, container: Ctn, user: User):
    lock = container.voice_start_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        session = await _owned_session(session_id, user, container)
        if session is None:
            return error_response("SESSION_NOT_FOUND", "会话不存在", 404)
        if session.modality not in ("voice", "both"):
            return error_response("INVALID_REQUEST", "该会话未启用语音", 400)
        if not _issues_credentials(_session_provider(container, session)):
            return error_response("INVALID_REQUEST", _NO_DIRECT_VOICE_LINK, 400)
        if session.state in {"task_accepted", "bot_joining", "voice_ready"}:
            response_state = cast(
                Literal["task_accepted", "bot_joining", "voice_ready"], session.state
            )
            return schemas.VoiceStartResponse(
                session_id=session.session_id,
                state=response_state,
                trace_id=session.voice_trace_id or new_trace_id(),
            )

        attempt = session.voice_attempt
        if session.state in {"bot_join_timeout", "provider_failed", "voice_stopped"}:
            # A retry is a new provider task generation. Stop the old task first;
            # old callbacks are then rejected by the new TaskId.
            if (
                session.voice_task_submitted_at is not None
                and session.voice_cleanup_status != "success"
            ):
                try:
                    await _session_provider(container, session).stop(
                        session.session_id, session.user_id, attempt
                    )
                except (httpx.HTTPError, VolcOpenAPIError) as exc:
                    return error_response(
                        "INTERNAL",
                        "旧火山 RTC 任务清理失败，未创建重复 Bot",
                        502,
                        details={"upstream": type(exc).__name__},
                    )
            attempt += 1
            try:
                cred = await _session_provider(container, session).issue_credentials(
                    session.session_id,
                    session.user_id,
                    session.scope.tools,
                    container.settings.voice_model,
                    _now(),
                    attempt,
                )
            except GeminiLiveProviderError as exc:
                return _gemini_provider_error_response(exc)
            session = replace(
                session,
                voice_attempt=attempt,
                voice_previous_task_ids=(
                    (*session.voice_previous_task_ids, session.voice_task_id)[-4:]
                    if session.voice_task_id
                    else session.voice_previous_task_ids
                ),
                voice_task_id=cred.task_id,
                voice_error_code=None,
                voice_error_message=None,
                voice_provider_request_id=None,
                voice_presence_request_id=None,
                voice_callback_trace_id=None,
                voice_callback_auth_version=CALLBACK_AUTH_VERSION,
                voice_task_submitted_at=None,
                voice_start_requested_at=None,
                voice_start_accepted_at=None,
                voice_mobile_joined_at=None,
                voice_bot_joined_at=None,
                voice_task_started_at=None,
                voice_task_creation_result=None,
                voice_callback_received_at=None,
                voice_first_provider_callback_at=None,
                voice_callback_schema_hash=None,
                voice_callback_received_count=0,
                voice_callback_accepted_count=0,
                voice_callback_duplicate_count=0,
                voice_callback_stale_count=0,
                voice_user_audio_status="UNKNOWN",
                voice_asr_started_at=None,
                voice_user_speech_started_at=None,
                voice_user_speech_ended_at=None,
                voice_llm_started_at=None,
                voice_llm_completed_at=None,
                voice_assistant_first_token_at=None,
                voice_function_call_started_at=None,
                voice_function_call_completed_at=None,
                voice_function_call_failed_at=None,
                voice_tts_started_at=None,
                voice_tts_first_packet_at=None,
                voice_user_transcript_at=None,
                voice_assistant_transcript_at=None,
                voice_stopped_at=None,
                voice_cleanup_status=None,
                voice_original_failure_stage=None,
                voice_provider_error_code=None,
                voice_provider_error_message=None,
                voice_first_failed_at=None,
                voice_provider_audio_started_at=None,
                voice_interrupted_at=None,
                voice_callback_event_ids=(),
                voice_tool_results=(),
                voice_tool_delivered=(),
            )

        _cancel_asr_silence_monitor(container, session_id)
        start_requested_at = _now()
        session = replace(session, voice_callback_auth_version=CALLBACK_AUTH_VERSION)
        session = session.with_voice_status(
            start_requested_at,
            state="voice_starting",
            run_stage="start_requested",
            error_code=None,
            start_requested_at=start_requested_at,
        )
        await container.session_store.update(session)
        diagnostics = getattr(_session_provider(container, session), "safe_diagnostics", None)
        if callable(diagnostics):
            safe_diagnostics = diagnostics(session.session_id, session.user_id, attempt)
            safe_diagnostics["session_app_matches_provider"] = safe_diagnostics.get(
                "rtc_app_id_hash"
            ) == _safe_id(session.voice_app_id)
            safe_diagnostics["session_room_matches_provider"] = safe_diagnostics.get(
                "room_hash"
            ) == _safe_id(session.voice_room_id)
            safe_diagnostics["session_mobile_user_matches_provider"] = safe_diagnostics.get(
                "mobile_user_hash"
            ) == _safe_id(session.voice_user_id)
            safe_diagnostics["session_bot_user_matches_provider"] = safe_diagnostics.get(
                "bot_user_hash"
            ) == _safe_id(session.voice_bot_user_id)
            safe_diagnostics["session_task_matches_provider"] = safe_diagnostics.get(
                "task_hash"
            ) == _safe_id(session.voice_task_id)
            logger.info(
                "voice_start_config trace_id=%s session_id=%s attempt=%s diagnostics=%s",
                session.voice_trace_id,
                session.session_id,
                attempt,
                json.dumps(safe_diagnostics, ensure_ascii=True, sort_keys=True),
            )
        try:
            acceptance = await _session_provider(container, session).start(
                session.session_id,
                session.user_id,
                attempt,
                context_summary=await container.context_builder.build_voice_context(
                    session.user_id, turns=session.turns
                ),
            )
        except (httpx.HTTPError, VolcOpenAPIError) as exc:
            code, message = _provider_error(exc)
            session = session.with_voice_status(
                _now(),
                state="provider_failed",
                run_stage="start_rejected",
                error_code="VOICE_PROVIDER_FAILED",
                error_message="连接失败，请重试",
                provider_error_code=code,
                provider_error_message=message,
            )
            await container.session_store.update(session)
            return error_response(
                "INTERNAL",
                "火山 RTC StartVoiceChat 调用失败",
                502,
                details={"upstream": type(exc).__name__},
            )
        submitted_at = _now()
        session = session.with_voice_status(
            submitted_at,
            state="task_accepted",
            run_stage="task_accepted",
            error_code=None,
            error_message=None,
            provider_request_id=acceptance.provider_request_id,
            task_submitted_at=submitted_at,
            start_accepted_at=submitted_at,
            task_creation_result=acceptance.provider_result or "accepted",
        )
        try:
            await container.session_store.update(session)
        except Exception:
            # StartVoiceChat may start a billable upstream task. If persisting
            # the active state fails, compensate immediately so a retry cannot
            # orphan that task.
            try:
                await _session_provider(container, session).stop(
                session.session_id, session.user_id, attempt
            )
            except Exception:
                pass
            raise
        logger.info(
            "voice_task_accepted trace_id=%s request_id=%s session_id=%s task_hash=%s "
            "room_hash=%s mobile_user_hash=%s bot_user_hash=%s submitted_at=%s "
            "attempt=%s task_creation_result=%s",
            session.voice_trace_id,
            acceptance.provider_request_id,
            session.session_id,
            _safe_id(session.voice_task_id),
            _safe_id(session.voice_room_id),
            _safe_id(session.voice_user_id),
            _safe_id(session.voice_bot_user_id),
            submitted_at.isoformat(),
            attempt,
            acceptance.provider_result or "accepted",
        )
        # 客户端直连渠道（Gemini / OpenAI）的数据面由 Mobile 直连。Runtime 没有
        # 服务端 Bot presence，也收不到这条具体会话的 setupComplete，因此不能用
        # no-op presence 伪造 voice_ready —— Mobile 以自己的 setupComplete 为准，
        # Runtime 保持 task_accepted。这里按渠道能力判定而不是硬编码 provider 名：
        # 硬编码只挡得住 Gemini，OpenAI 那条同样没有 Bot 可等。
        # 闸门同时下沉进 _schedule_bot_monitor，覆盖 GET Session 的恢复调度路径
        # —— 线上正是那条路径把正常通话误判成了 bot_join_timeout。
        _schedule_bot_monitor(container, session.session_id, attempt, session)
        return schemas.VoiceStartResponse(
            session_id=session.session_id,
            state="task_accepted",
            trace_id=session.voice_trace_id or new_trace_id(),
        )


@router.post(
    "/volc-callback",
    status_code=status.HTTP_200_OK,
    summary="火山引擎 RTC Server State Callback",
)
async def volc_callback(
    request: Request,
    container: Ctn,
    session_id: Annotated[str | None, Query()] = None,
):
    if not session_id:
        return await _volc_callback_serialized(request, container, session_id)
    lock_key = f"state:{session_id}"
    lock = container.voice_callback_locks.setdefault(lock_key, asyncio.Lock())
    try:
        async with lock:
            return await _volc_callback_serialized(request, container, session_id)
    finally:
        if not lock.locked():
            container.voice_callback_locks.pop(lock_key, None)


async def _volc_callback_serialized(
    request: Request,
    container: Container,
    session_id: str | None,
) -> Response:
    metrics = container.voice_callback_metrics
    metrics.increment("received")
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _CALLBACK_MAX_BYTES:
        metrics.increment("schema_rejected")
        return Response(status_code=413)
    raw_body = await request.body()
    if len(raw_body) > _CALLBACK_MAX_BYTES:
        metrics.increment("schema_rejected")
        return Response(status_code=413)
    try:
        body = json.loads(raw_body)
    except (TypeError, ValueError):
        metrics.increment("schema_rejected")
        return Response(status_code=400)
    if not isinstance(body, dict):
        metrics.increment("schema_rejected")
        return Response(status_code=400)

    if not session_id:
        metrics.increment("schema_rejected")
        return Response(status_code=400)
    session = await container.session_store.get(session_id)
    if session is None:
        # A task may have been stopped and its Session deleted before a delayed
        # provider callback arrives. Acknowledge it without recreating state.
        metrics.increment("stale")
        return Response(status_code=200)
    signature = _callback_value(body, "signature", "Signature")
    signature_status = _callback_signature_status(
        session,
        signature,
        container.settings.volc_callback_secret,
        purpose="state",
    )
    if signature_status == "stale":
        metrics.increment("stale")
        return Response(status_code=200)
    if signature_status == "invalid":
        metrics.increment("signature_rejected")
        logger.warning(
            "volc_callback rejected invalid signature session_id=%s task_hash=%s",
            session_id,
            _safe_id(session.voice_task_id),
        )
        return Response(status_code=401)

    if session.state in {"voice_stopped", "closed"}:
        metrics.increment("stale")
        return Response(status_code=200)

    try:
        event = parse_state_callback(body)
    except VolcCallbackSchemaError as exc:
        metrics.increment("schema_rejected")
        logger.warning(
            "volc_callback schema_rejected session_id=%s task_hash=%s reason=%s",
            session_id,
            _safe_id(session.voice_task_id),
            str(exc),
        )
        return Response(status_code=400)

    app_id = event.app_id
    room_id = event.room_id
    task_id = event.task_id
    # Official conversation-state callbacks omit AppId and RoomId. The URL
    # session binding plus a signature derived from the current VoiceChat task
    # remains authoritative. Any identity field that is present must match.
    room_mismatch = room_id is not None and room_id != session.voice_room_id
    app_mismatch = app_id is not None and app_id != session.voice_app_id

    if room_mismatch or app_mismatch:
        metrics.increment("schema_rejected")
        logger.warning(
            "volc_callback identity_mismatch session_id=%s trace_id=%s "
            "got_app_hash=%s got_room_hash=%s got_task_hash=%s "
            "expected_app_hash=%s expected_room_hash=%s expected_task_hash=%s",
            session_id,
            session.voice_trace_id,
            _safe_id(str(app_id)) if app_id is not None else "none",
            _safe_id(str(room_id)) if room_id is not None else "none",
            _safe_id(str(task_id)) if task_id is not None else "none",
            _safe_id(session.voice_app_id),
            _safe_id(session.voice_room_id),
            _safe_id(session.voice_task_id),
        )
        return Response(status_code=400)
    if task_id is not None and task_id != session.voice_task_id:
        metrics.increment("stale")
        session = replace(
            session,
            voice_callback_received_count=session.voice_callback_received_count + 1,
            voice_callback_stale_count=session.voice_callback_stale_count + 1,
            voice_callback_schema_hash=event.schema_hash,
        )
        await container.session_store.update(session)
        logger.info(
            "volc_callback stale_task session_id=%s trace_id=%s task_hash=%s schema_hash=%s",
            session_id,
            session.voice_trace_id,
            _safe_id(str(task_id)),
            event.schema_hash,
        )
        return Response(status_code=200)
    if session.voice_callback_auth_version != CALLBACK_AUTH_VERSION and task_id is None:
        # Pre-rollout sessions used a shared secret. They do not get the
        # session-only fallback because that would weaken old callback binding.
        metrics.increment("schema_rejected")
        return Response(status_code=400)

    run_stage = event.run_stage
    if run_stage is None:
        metrics.increment("schema_rejected")
        logger.warning(
            "volc_callback schema_rejected session_id=%s task_hash=%s schema_hash=%s "
            "schema_fields=%s reason=missing_stage",
            session_id,
            _safe_id(session.voice_task_id),
            event.schema_hash,
            ",".join(event.schema_fields),
        )
        return Response(status_code=400)
    normalized_stage = (run_stage or "").replace("_", "").replace("-", "").lower()
    error_code = event.error_code
    error_message = _safe_callback_message(
        event.error_message,
        secrets=(
            container.settings.volc_callback_secret,
            container.settings.volc_secret_access_key,
            container.settings.huochan_api_key,
            container.settings.rtc_app_key,
            container.settings.openai_api_key,
        ),
    )
    event_type = event.event_type
    callback_trace_id = _safe_callback_identifier(event.provider_trace_id) or session.voice_trace_id
    callback_event_id = hashlib.sha256(
        json.dumps(
            {
                "app_id": app_id,
                "room_id": room_id,
                "task_id": task_id,
                "run_stage": run_stage,
                "error_code": error_code,
                "event_type": event_type,
                "provider_trace_id": callback_trace_id,
                "event_time": event.event_time,
                "round_id": event.round_id,
                "schema_hash": event.schema_hash,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if callback_event_id in session.voice_callback_event_ids:
        metrics.increment("duplicate")
        session = replace(
            session,
            voice_callback_received_count=session.voice_callback_received_count + 1,
            voice_callback_duplicate_count=session.voice_callback_duplicate_count + 1,
            voice_callback_schema_hash=event.schema_hash,
        )
        await container.session_store.update(session)
        return Response(status_code=200)

    session = replace(
        session,
        voice_callback_received_count=session.voice_callback_received_count + 1,
        voice_callback_schema_hash=event.schema_hash,
    )

    _log_volc_callback(
        session,
        run_stage=run_stage,
        error_code=error_code,
        error_message=error_message,
        callback_trace_id=callback_trace_id,
    )
    logger.info(
        "volc_callback_schema session_id=%s task_hash=%s schema_kind=%s schema_hash=%s "
        "schema_fields=%s",
        session_id,
        _safe_id(session.voice_task_id),
        event.schema_kind,
        event.schema_hash,
        ",".join(event.schema_fields),
    )

    now = _now()
    has_error = str(event_type) == "1" or error_code not in (
        None,
        "",
        "0",
        "OK",
        "Success",
        "success",
    )
    audio_started_at = now if normalized_stage in _CALLBACK_AUDIO_STAGES else None
    interrupted_at = now if normalized_stage in _CALLBACK_INTERRUPT_STAGES else None
    asr_evidence = normalized_stage in (
        _CALLBACK_ASR_START_STAGES | _CALLBACK_USER_TRANSCRIPT_STAGES
    )
    llm_evidence = normalized_stage in (_CALLBACK_LLM_START_STAGES | _CALLBACK_LLM_COMPLETE_STAGES)
    tts_evidence = normalized_stage in (_CALLBACK_TTS_START_STAGES | _CALLBACK_AUDIO_STAGES)
    first_token_evidence = normalized_stage in _CALLBACK_FIRST_TOKEN_STAGES
    tts_first_packet_evidence = normalized_stage in _CALLBACK_AUDIO_STAGES
    # Record pipeline evidence independently from the lifecycle transition.
    # In particular, ASR/LLM/TTS events never prove RTC room presence.
    session = session.with_voice_status(
        now,
        state=session.state,
        run_stage=session.voice_run_stage,
        error_code=session.voice_error_code,
        error_message=session.voice_error_message,
        task_started_at=(now if normalized_stage in _CALLBACK_TASK_START_STAGES else None),
        user_audio_status=("OBSERVED" if normalized_stage in _CALLBACK_USER_AUDIO_STAGES else None),
        asr_started_at=now if asr_evidence else None,
        user_speech_started_at=(
            now if normalized_stage in _CALLBACK_USER_SPEECH_START_STAGES else None
        ),
        user_speech_ended_at=(
            now if normalized_stage in _CALLBACK_USER_SPEECH_END_STAGES else None
        ),
        user_transcript_at=(now if normalized_stage in _CALLBACK_USER_TRANSCRIPT_STAGES else None),
        llm_started_at=now if llm_evidence else None,
        llm_completed_at=(now if normalized_stage in _CALLBACK_LLM_COMPLETE_STAGES else None),
        assistant_first_token_at=now if first_token_evidence else None,
        tts_started_at=now if tts_evidence else None,
        tts_first_packet_at=now if tts_first_packet_evidence else None,
        assistant_transcript_at=(
            now if normalized_stage in _CALLBACK_ASSISTANT_TRANSCRIPT_STAGES else None
        ),
        provider_audio_started_at=audio_started_at,
        interrupted_at=interrupted_at,
    )
    if normalized_stage in _CALLBACK_USER_TRANSCRIPT_STAGES:
        metrics.observe_pipeline("user_transcript", at=now)
    if first_token_evidence:
        metrics.observe_pipeline("assistant_first_token", at=now)
    if tts_first_packet_evidence:
        metrics.observe_pipeline("tts_first_packet", at=now)

    if has_error or normalized_stage in _CALLBACK_FAILURE_STAGES:
        cleanup_status = session.voice_cleanup_status
        stopped_at = session.voice_stopped_at
        if cleanup_status != "success":
            try:
                await _session_provider(container, session).stop(
                    session.session_id, session.user_id, session.voice_attempt
                )
                cleanup_status = "success"
                stopped_at = _now()
            except (httpx.HTTPError, VolcOpenAPIError):
                cleanup_status = "failed"
        session = session.with_voice_status(
            now,
            state="provider_failed",
            run_stage=run_stage,
            error_code="VOICE_PROVIDER_FAILED",
            error_message="连接失败，请重试",
            provider_error_code=error_code or "VOLC_VOICE_TASK_FAILED",
            provider_error_message=error_message,
            callback_trace_id=callback_trace_id,
            callback_received_at=now,
            callback_event_id=callback_event_id,
            stopped_at=stopped_at,
            cleanup_status=cleanup_status,
        )
    elif session.state in {"provider_failed", "bot_join_timeout"}:
        # Events may be retried or arrive out of order. Preserve the original
        # failure and only add callback/cleanup evidence.
        session = session.with_voice_status(
            now,
            state=session.state,
            run_stage=session.voice_run_stage,
            error_code=session.voice_error_code,
            error_message=session.voice_error_message,
            callback_trace_id=callback_trace_id,
            callback_received_at=now,
            callback_event_id=callback_event_id,
            stopped_at=now if normalized_stage in _CALLBACK_STOP_STAGES else None,
            cleanup_status=(
                "success"
                if normalized_stage in _CALLBACK_STOP_STAGES
                else session.voice_cleanup_status
            ),
        )
    elif normalized_stage in _CALLBACK_STOP_STAGES:
        session = session.with_voice_status(
            now,
            state="voice_stopped",
            run_stage=run_stage,
            error_code=None,
            callback_trace_id=callback_trace_id,
            callback_received_at=now,
            callback_event_id=callback_event_id,
            stopped_at=now,
            cleanup_status="success",
        )
    elif session.state == "voice_ready":
        session = session.with_voice_status(
            now,
            state="voice_ready",
            run_stage=run_stage or session.voice_run_stage,
            error_code=None,
            callback_trace_id=callback_trace_id,
            callback_received_at=now,
            callback_event_id=callback_event_id,
        )
    elif normalized_stage in _CALLBACK_TASK_START_STAGES:
        session = session.with_voice_status(
            now,
            state="bot_joining" if session.state == "bot_joining" else "task_accepted",
            run_stage=run_stage,
            error_code=None,
            callback_trace_id=callback_trace_id,
            callback_received_at=now,
            callback_event_id=callback_event_id,
        )
    else:
        # ASR/LLM/TTS events prove pipeline progress, not RTC room membership.
        # The presence poller is the only path that promotes to voice_ready.
        session = session.with_voice_status(
            now,
            state="bot_joining",
            run_stage=run_stage,
            error_code=None,
            callback_trace_id=callback_trace_id,
            callback_received_at=now,
            callback_event_id=callback_event_id,
        )
    if asr_evidence and session.voice_error_code == "VOICE_UPLINK_OR_ASR_SILENT":
        # The diagnostic is actionable during the silent window, but a later
        # proven ASR event means the current session recovered. Keep the first
        # failure timestamp/stage for diagnosis while clearing the Mobile error.
        session = replace(
            session,
            voice_error_code=None,
            voice_error_message=None,
        )
    session = replace(
        session,
        voice_callback_accepted_count=session.voice_callback_accepted_count + 1,
    )
    metrics.increment("accepted")
    await container.session_store.update(session)

    if (
        session.state != "voice_ready"
        or has_error
        or normalized_stage in _CALLBACK_STOP_STAGES
        or asr_evidence
        or normalized_stage in _CALLBACK_USER_TRANSCRIPT_STAGES
    ):
        _cancel_asr_silence_monitor(container, session_id)
    elif (
        session.state == "voice_ready"
        and session.voice_bot_joined_at is not None
        and session.voice_provider_audio_started_at is not None
        and session.voice_asr_started_at is None
        and session.voice_user_transcript_at is None
        and session_id not in container.voice_silence_tasks
    ):
        _schedule_asr_silence_monitor(container, session_id, session.voice_attempt)

    return Response(status_code=200)


def _function_call(body: dict) -> tuple[str | None, str | None, object]:
    calls = _callback_value(body, "tool_calls", "ToolCalls")
    call = calls[0] if isinstance(calls, list) and len(calls) == 1 else None
    if not isinstance(call, dict):
        call = body
    function = call.get("function") or call.get("Function")
    function = function if isinstance(function, dict) else {}
    tool_call_id = call.get("id") or call.get("ToolCallID") or call.get("tool_call_id")
    tool_name = (
        function.get("name")
        or function.get("Name")
        or call.get("tool_name")
        or call.get("ToolName")
    )
    arguments = (
        function.get("arguments")
        if "arguments" in function
        else function.get("Arguments", call.get("arguments", call.get("Arguments")))
    )
    return (
        str(tool_call_id) if tool_call_id is not None else None,
        str(tool_name) if tool_name is not None else None,
        arguments,
    )


@router.post(
    "/volc-function-callback",
    status_code=status.HTTP_200_OK,
    summary="火山 VoiceChat Function Calling 回调",
)
async def volc_function_callback(
    request: Request,
    container: Ctn,
    session_id: Annotated[str | None, Query()] = None,
):
    if not session_id:
        return await _volc_function_callback_serialized(request, container, session_id)
    lock_key = f"function:{session_id}"
    lock = container.voice_callback_locks.setdefault(lock_key, asyncio.Lock())
    try:
        async with lock:
            return await _volc_function_callback_serialized(request, container, session_id)
    finally:
        if not lock.locked():
            container.voice_callback_locks.pop(lock_key, None)


async def _volc_function_callback_serialized(
    request: Request,
    container: Container,
    session_id: str | None,
) -> Response:
    started = time.perf_counter()
    metrics = container.voice_callback_metrics
    metrics.increment("received")
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > _CALLBACK_MAX_BYTES:
        metrics.increment("schema_rejected")
        return Response(status_code=413)
    raw_body = await request.body()
    if len(raw_body) > _CALLBACK_MAX_BYTES:
        metrics.increment("schema_rejected")
        return Response(status_code=413)
    try:
        body = json.loads(raw_body)
    except (ValueError, TypeError):
        metrics.increment("schema_rejected")
        return Response(status_code=400)
    if not isinstance(body, dict):
        metrics.increment("schema_rejected")
        return Response(status_code=400)
    if not session_id:
        metrics.increment("schema_rejected")
        return Response(status_code=400)

    session = await container.session_store.get(session_id)
    if session is None:
        metrics.increment("stale")
        return Response(status_code=200)
    signature = _callback_value(body, "signature", "Signature")
    signature_status = _callback_signature_status(
        session,
        signature,
        container.settings.volc_function_callback_secret,
        purpose="function",
    )
    if signature_status == "stale":
        metrics.increment("stale")
        return Response(status_code=200)
    if signature_status == "invalid":
        metrics.increment("signature_rejected")
        logger.warning(
            "volc_function_callback rejected invalid signature session_id=%s task_hash=%s",
            session_id,
            _safe_id(session.voice_task_id),
        )
        return Response(status_code=401)
    if session.state in {"voice_stopped", "closed"}:
        metrics.increment("stale")
        return Response(status_code=200)

    try:
        function_event = parse_function_callback(body)
    except VolcCallbackSchemaError as exc:
        metrics.increment("schema_rejected")
        logger.warning(
            "volc_function_callback schema_rejected session_id=%s task_hash=%s reason=%s",
            session_id,
            _safe_id(session.voice_task_id),
            str(exc),
        )
        return Response(status_code=400)

    app_id = function_event.app_id
    room_id = function_event.room_id
    function_task_id = function_event.function_task_id
    if (app_id is not None and app_id != session.voice_app_id) or (
        room_id is not None and room_id != session.voice_room_id
    ):
        metrics.increment("schema_rejected")
        logger.warning(
            "volc_function_callback identity_mismatch trace_id=%s session_id=%s "
            "room_hash=%s function_task_hash=%s",
            session.voice_trace_id,
            session_id,
            _safe_id(room_id),
            _safe_id(function_task_id),
        )
        return Response(status_code=400)
    if session.voice_callback_auth_version != CALLBACK_AUTH_VERSION and (
        app_id is None or room_id is None
    ):
        metrics.increment("schema_rejected")
        return Response(status_code=400)

    logger.info(
        "volc_function_callback_schema trace_id=%s session_id=%s voice_task_hash=%s "
        "function_task_hash=%s schema_hash=%s schema_fields=%s event_type=%s",
        session.voice_trace_id,
        session_id,
        _safe_id(session.voice_task_id),
        _safe_id(function_task_id),
        function_event.schema_hash,
        ",".join(function_event.schema_fields),
        function_event.event_type,
    )

    if function_event.event_type == "information":
        notification = function_event.notification or {}
        event_id = hashlib.sha256(
            json.dumps(
                {
                    "kind": "function_information",
                    "function_task_id": function_task_id,
                    "notification": notification,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if event_id in session.voice_callback_event_ids:
            metrics.increment("duplicate")
            return Response(status_code=200)
        session = session.with_voice_status(
            _now(),
            state=session.state,
            run_stage=session.voice_run_stage,
            error_code=session.voice_error_code,
            callback_event_id=event_id,
            callback_received_at=_now(),
        )
        await container.session_store.update(session)
        metrics.increment("accepted")
        return Response(status_code=200)

    callback_body = dict(body)
    if function_event.tool_calls is not None:
        callback_body["tool_calls"] = function_event.tool_calls
    tool_call_id, tool_name, raw_arguments = _function_call(callback_body)
    if not tool_call_id or len(tool_call_id) > 128 or not tool_name or len(tool_name) > 128:
        metrics.increment("schema_rejected")
        return Response(status_code=400)
    voice_task_id = session.voice_task_id
    voice_attempt = session.voice_attempt
    if not voice_task_id:
        metrics.increment("stale")
        return Response(status_code=200)

    if function_event.event_type not in (None, "tool_calls"):
        metrics.increment("schema_rejected")
        return Response(status_code=400)

    if function_task_id is None:
        logger.info(
            "volc_function_callback missing_function_task_id trace_id=%s session_id=%s "
            "voice_task_hash=%s",
            session.voice_trace_id,
            session_id,
            _safe_id(voice_task_id),
        )
    session = await _record_function_call_stage(container, session, "started")
    if not isinstance(raw_arguments, str):
        metrics.increment("schema_rejected")
        await _record_function_call_stage(container, session, "failed")
        return Response(status_code=400)
    try:
        arguments = json.loads(raw_arguments)
    except ValueError:
        metrics.increment("schema_rejected")
        await _record_function_call_stage(container, session, "failed")
        return Response(status_code=400)
    if not isinstance(arguments, dict):
        metrics.increment("schema_rejected")
        await _record_function_call_stage(container, session, "failed")
        return Response(status_code=400)

    trace_id = new_trace_id()
    hashed = args_hash(arguments)
    if not container.registry.is_allowed(tool_name, list(session.scope.tools)):
        await _record_function_call_stage(container, session, "failed")
        await container.audit_store.record_tool_call(
            AuditRecord(
                trace_id=trace_id,
                session_id=session_id,
                tool_name=tool_name,
                args_hash=hashed,
                result_ref=tool_call_id,
                created_at=_now(),
                status="rejected",
            )
        )
        return Response(status_code=403)

    lock_key = f"{session_id}:{tool_call_id}"
    lock = container.voice_tool_locks.setdefault(lock_key, asyncio.Lock())
    try:
        async with lock:
            session = await container.session_store.get(session_id)
            if session is None or session.state in {"voice_stopped", "closed"}:
                metrics.increment("stale")
                return Response(status_code=200)
            if session.voice_task_id != voice_task_id or session.voice_attempt != voice_attempt:
                metrics.increment("stale")
                return Response(status_code=200)
            if tool_call_id in session.voice_tool_delivered:
                metrics.increment("duplicate")
                logger.info(
                    "volc_function_callback duplicate trace_id=%s session_id=%s task_hash=%s "
                    "tool_call_hash=%s tool_name=%s status=already_delivered",
                    trace_id,
                    session_id,
                    _safe_id(voice_task_id),
                    _safe_id(tool_call_id),
                    tool_name,
                )
                return Response(status_code=200)

            cached = session.voice_tool_result(tool_call_id)
            core_request_id: str | None = None
            tool_status = "ok"
            if cached is not None:
                content = json.loads(cached)
            else:
                progress_task = asyncio.create_task(
                    _delayed_function_progress(container, session, tool_call_id),
                    name=f"voice-function-progress:{session_id}:{_safe_id(tool_call_id)}",
                )
                try:
                    idempotency_context = hashlib.sha256(
                        (f"{session.user_id}\0{session.session_id}\0{tool_name}\0{hashed}").encode()
                    ).hexdigest()
                    content = await container.executor.execute(
                        tool_name,
                        session.user_id,
                        arguments,
                        list(session.scope.tools),
                        idempotency_key=f"idem_voice_{idempotency_context}",
                        intent_source="VOICE",
                    )
                    core_request_id = str(content.get("request_id") or "")[:128] or None
                except MarketDataError as exc:
                    core_request_id = exc.request_id
                    tool_status = "tool_error"
                    content = market_data_tool_error(
                        exc, observer=container.core_credential_readiness
                    )
                except MarketInstrumentResolutionError as exc:
                    tool_status = "tool_error"
                    content = market_resolution_tool_error(exc)
                except httpx.HTTPError as exc:
                    tool_status = "tool_error"
                    content = core_http_tool_error(
                        exc, observer=container.core_credential_readiness
                    )
                    core_request_id = str(content.get("request_id") or "")[:128] or None
                except TradingReadinessError as exc:
                    tool_status = "tool_error"
                    content = {
                        "status": "error",
                        "error_code": "POLICY_VIOLATION",
                        "message": str(exc)[:256],
                        "retryable": False,
                        "credential_refresh_required": False,
                    }
                except (ValueError, ToolNotAllowedError) as exc:
                    tool_status = "tool_error"
                    content = {
                        "status": "error",
                        "error_code": "INVALID_TOOL_ARGUMENTS",
                        "message": str(exc)[:256],
                        "retryable": False,
                    }
                finally:
                    progress_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await progress_task
                encoded = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
                session = session.with_voice_tool_result(_now(), tool_call_id, encoded)
                await container.session_store.update(session)

            try:
                update = await _session_provider(container, session).send_function_result(
                    session.session_id,
                    session.user_id,
                    tool_call_id,
                    content,
                    voice_attempt,
                )
            except (httpx.HTTPError, VolcOpenAPIError) as exc:
                await _record_function_call_stage(container, session, "failed")
                await container.audit_store.record_tool_call(
                    AuditRecord(
                        trace_id=trace_id,
                        session_id=session_id,
                        tool_name=tool_name,
                        args_hash=hashed,
                        result_ref=core_request_id or tool_call_id,
                        created_at=_now(),
                        status="update_failed",
                    )
                )
                logger.warning(
                    "volc_function_result_failed trace_id=%s session_id=%s task_hash=%s "
                    "tool_call_hash=%s tool_name=%s core_request_id=%s duration_ms=%s "
                    "status=update_failed error_type=%s",
                    trace_id,
                    session_id,
                    _safe_id(voice_task_id),
                    _safe_id(tool_call_id),
                    tool_name,
                    core_request_id,
                    int((time.perf_counter() - started) * 1000),
                    type(exc).__name__,
                )
                return Response(status_code=502)

            session = await _record_function_call_stage(
                container,
                session,
                "completed" if tool_status == "ok" else "failed",
            )
            session = session.with_voice_tool_delivered(_now(), tool_call_id)
            await container.session_store.update(session)
            await container.audit_store.record_tool_call(
                AuditRecord(
                    trace_id=trace_id,
                    session_id=session_id,
                    tool_name=tool_name,
                    args_hash=hashed,
                    result_ref=core_request_id or tool_call_id,
                    created_at=_now(),
                    status=tool_status,
                )
            )
            logger.info(
                "volc_function_result trace_id=%s request_id=%s session_id=%s task_hash=%s "
                "tool_call_hash=%s tool_name=%s core_request_id=%s duration_ms=%s status=%s",
                trace_id,
                update.provider_request_id,
                session_id,
                _safe_id(voice_task_id),
                _safe_id(tool_call_id),
                tool_name,
                core_request_id,
                int((time.perf_counter() - started) * 1000),
                tool_status,
            )
            metrics.increment("accepted")
            return Response(status_code=200)
    finally:
        if not lock.locked():
            container.voice_tool_locks.pop(lock_key, None)


@router.post(
    "/sessions/{session_id}/token",
    response_model=schemas.RefreshedCredentials,
    summary="续签短期凭证（rt_token 与/或 voice.client_secret）",
    responses={401: _ERR, 403: _ERR, 404: _ERR, 409: _ERR, 410: _ERR, 503: _ERR},
)
async def refresh_token(
    session_id: str,
    container: Ctn,
    user: User,
    body: schemas.RefreshTokenRequest | None = None,
):
    session = await _owned_session(session_id, user, container)
    if session is None:
        return error_response("SESSION_NOT_FOUND", "会话不存在", 404)
    now = _now()
    want = body.want if body and body.want else ["rt_token", "voice"]
    requested = body.provider if body else None
    current = session.voice_provider_name or container.settings.realtime_voice_provider
    switching = requested is not None and requested != current
    if switching and session.voice_task_submitted_at is not None and (
        session.voice_cleanup_status != "success"
    ):
        # 绝不允许在有活跃 provider 任务时换渠道：否则后续 stop 会打到新渠道上，
        # 旧任务在原 provider 侧一直计费。客户端应先 DELETE 会话再重开。
        return error_response(
            "POLICY_VIOLATION",
            "会话仍有未清理的语音任务，无法切换语音渠道；请先结束会话再重试",
            409,
        )

    rt_token = rt_exp = voice = None
    if "rt_token" in want:
        rt_token, rt_exp = container.token_service.issue_rt_token(
            session_id, session.user_id, session.scope.tools, now
        )
    if "voice" in want:
        try:
            provider = container.voice_provider_for(requested or current)
        except VoiceProviderUnavailableError:
            logger.warning("voice_fallback_provider_unavailable session_id=%s", session_id)
            return error_response("INTERNAL", "备用语音渠道暂不可用，请稍后重试", 503)
        if not _issues_credentials(provider):
            return error_response("INVALID_REQUEST", _NO_DIRECT_VOICE_LINK, 400)
        try:
            cred = await provider.issue_credentials(
                session_id,
                session.user_id,
                session.scope.tools,
                container.settings.voice_model,
                now,
                session.voice_attempt,
                context_summary=await container.context_builder.build_voice_context(
                    session.user_id, turns=session.turns
                ),
            )
        except GeminiLiveProviderError as exc:
            return _gemini_provider_error_response(exc)
        voice = _voice_schema(cred)
        resolved = requested or current
        # 同渠道续签时凭证身份是确定性推导的，这里等价于 no-op；换渠道时必须落库，
        # 否则 DELETE 拿着旧渠道的 task_id 判断，会漏掉对新任务的 Stop。
        refreshed = replace(
            session,
            voice_provider_name=resolved,
            voice_app_id=cred.app_id,
            voice_room_id=cred.room_id,
            voice_user_id=cred.user_id,
            voice_task_id=cred.task_id,
            voice_bot_user_id=cred.bot_user_id,
        )
        if refreshed != session:
            session = replace(refreshed, last_activity_at=now)
            await container.session_store.update(session)
            logger.info(
                "voice_provider_ticket_issued session_id=%s provider=%s task_hash=%s",
                session_id,
                resolved,
                _safe_id(session.voice_task_id),
            )
    return schemas.RefreshedCredentials(
        rt_token=rt_token, rt_token_expires_at=rt_exp, voice=voice, trace_id=new_trace_id()
    )


@router.post(
    "/sessions/{session_id}/tool-calls",
    response_model=schemas.ToolCallResult,
    summary="语音工具旁挂执行（App 转发 OpenAI 的 function_call 给 RT）",
    responses={401: _ERR, 403: _ERR, 404: _ERR, 429: _ERR, 502: _ERR, 503: _ERR, 504: _ERR},
)
async def relay_tool_call(
    session_id: str,
    body: schemas.ToolCallRequest,
    container: Ctn,
    authorization: Annotated[str | None, Header()] = None,
    idempotency_key: IdempotencyKey = None,
):
    now = _now()
    token = bearer_token(authorization)
    claims = container.token_service.verify_rt_token(token, now) if token else None
    if claims is None or claims.session_id != session_id:
        return error_response("UNAUTHORIZED", "rt_token 无效或与会话不符", 401)

    session = await container.session_store.get(session_id)
    if session is None:
        return error_response("SESSION_NOT_FOUND", "会话不存在", 404)
    if claims.user_id != session.user_id:
        return error_response("UNAUTHORIZED", "rt_token 与会话用户不符", 401)

    # 红线：以 session.scope.tools 二次强校验（不信任客户端上报的 tool_name）。
    # 与 /api/voice/tools/execute 共用同一条风控执行 + 留痕路径。
    outcome = await execute_controlled_tool(
        container,
        session,
        body.tool_name,
        body.arguments,
        now,
        idempotency_key=idempotency_key,
        intent_source="VOICE" if session.modality != "text" else "TEXT",
    )
    if outcome.error is not None:
        return outcome.error
    return schemas.ToolCallResult(
        call_id=body.call_id,
        status="ok",
        output=outcome.output,
        trade_draft_id=outcome.trade_draft_id,
    )


@router.get(
    "/tools",
    response_model=list[schemas.ToolSpec],
    summary="查询受控 Function Calling 工具列表及其 Schema",
    responses={401: _ERR, 503: _ERR},
)
async def list_tools(container: Ctn, user: User) -> list[schemas.ToolSpec]:
    """返回系统登记的所有 Function Call 工具配置与 Schema 格式。"""
    return [
        schemas.ToolSpec(
            name=t.name,
            description=t.description,
            parameters=t.parameters,
            write=t.write,
            external=t.external,
        )
        for t in container.registry.all_tools()
    ]


@router.post(
    "/messages",
    response_model=schemas.SingleTurnResponse,
    summary="文本单轮（非流式，简单场景；主路径请用文本 WS）",
    responses={
        400: _ERR,
        401: _ERR,
        403: _ERR,
        404: _ERR,
        429: _ERR,
        502: _ERR,
        503: _ERR,
        504: _ERR,
    },
)
async def post_message(
    body: schemas.SingleTurnRequest,
    container: Ctn,
    user: User,
    authorization: Annotated[str | None, Header()] = None,
    idempotency_key: IdempotencyKey = None,
):
    session = await _owned_session(body.session_id, user, container)
    if session is None:
        return error_response("SESSION_NOT_FOUND", "会话不存在", 404)
    user_token = bearer_token(authorization)
    if user_token is None:
        # This is normally rejected by the shared authentication dependency;
        # keep the invariant explicit before the token is passed request-locally.
        return error_response("UNAUTHENTICATED", "缺少 USER Bearer Token", 401)
    try:
        operation_hash, request_hash, stable_key = _text_turn_identity(
            session, user, body, idempotency_key
        )
        cached = _cached_text_turn(session, operation_hash, request_hash)
    except ValueError as exc:
        return error_response(
            "IDEMPOTENCY_CONFLICT",
            str(exc)[:256],
            409,
            details={"retryable": False},
        )
    except RuntimeError:
        return error_response(
            "INTERNAL",
            "消息结果暂时无法恢复，请联系支持并提供 trace_id",
            503,
            details={"retryable": False},
        )
    if cached is not None:
        return cached

    in_flight_hash = container.text_turn_request_hashes.get(operation_hash)
    if in_flight_hash is not None and not hmac.compare_digest(in_flight_hash, request_hash):
        return error_response(
            "IDEMPOTENCY_CONFLICT",
            "Idempotency-Key 已用于不同消息",
            409,
            details={"retryable": False},
        )

    async def complete_turn() -> schemas.SingleTurnResponse:
        current = await _owned_session(body.session_id, user, container)
        if current is None:
            raise LookupError("session_not_found")
        recovered = _cached_text_turn(current, operation_hash, request_hash)
        if recovered is not None:
            return recovered
        now = _now()
        trace_id = new_trace_id()
        result = await container.runner.run_turn(
            current,
            body.text,
            trace_id,
            now,
            user_bearer_token=user_token,
            idempotency_key=stable_key,
            web_search=body.web_search,
        )
        response = schemas.SingleTurnResponse(
            text=result.text,
            tool_calls=[
                schemas.ToolCallResult(
                    call_id=e.call_id,
                    status="ok",
                    output=e.result,
                    trade_draft_id=e.trade_draft_id,
                )
                for e in result.executed_tools
            ],
            blockers=list(result.blockers),
            trace_id=trace_id,
        )
        stored = TextTurnResult(
            operation_hash=operation_hash,
            request_hash=request_hash,
            response_json=response.model_dump_json(),
            created_at=now,
        )
        latest = await container.session_store.get(current.session_id)
        if latest is None or latest.user_id != user.user_id:
            raise LookupError("session_not_found")
        await container.session_store.update(latest.with_text_turn_result(now, stored))
        return response

    task = container.text_turn_tasks.get(operation_hash)
    if task is None:
        container.text_turn_request_hashes[operation_hash] = request_hash
        task = asyncio.create_task(
            complete_turn(),
            name=f"text-turn:{session.session_id}:{operation_hash[:12]}",
        )
        container.text_turn_tasks[operation_hash] = task

        def cleanup(completed: asyncio.Task) -> None:
            if container.text_turn_tasks.get(operation_hash) is completed:
                container.text_turn_tasks.pop(operation_hash, None)
                container.text_turn_request_hashes.pop(operation_hash, None)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(cleanup)

    trace_id = new_trace_id()
    try:
        return await asyncio.shield(task)
    except LookupError:
        return error_response("SESSION_NOT_FOUND", "会话不存在", 404, trace_id=trace_id)
    except MarketDataError as exc:
        return market_data_error_response(
            exc,
            observer=container.core_credential_readiness,
            trace_id=trace_id,
        )
    except MarketInstrumentResolutionError as exc:
        return market_resolution_error_response(exc, trace_id=trace_id)
    except httpx.HTTPError as exc:
        return _map_upstream_error(
            exc,
            observer=container.core_credential_readiness,
            trace_id=trace_id,
        )
    except TradingReadinessError as exc:
        return error_response("POLICY_VIOLATION", str(exc)[:256], 409, trace_id=trace_id)
    except ValueError as exc:
        return error_response("INVALID_REQUEST", str(exc)[:256], 400, trace_id=trace_id)
    except Exception:
        # 上面几个 except 只覆盖了已知的上游异常。LLM 网关/中转站故障不在其中，
        # 没有这一层就会漏成 FastAPI 的裸 500——既不走错误信封，也和 WS 路径
        # 的行为不一致（那边会发一个 error 事件）。原始异常只进日志：它可能带着
        # 内部网关地址和凭证名。
        logger.exception("text_turn_unhandled_error trace_id=%s", trace_id)
        return error_response(
            "INTERNAL", "服务暂时不可用，请稍后重试。", 503, trace_id=trace_id
        )


@router.post(
    "/trade-draft",
    response_model=schemas.TradeDraftCard,
    status_code=status.HTTP_201_CREATED,
    summary="文本下单意图 → 构造 AgentOS 交易草稿并返回确认卡片（不代为确认）",
    responses={400: _ERR, 401: _ERR, 403: _ERR, 404: _ERR, 502: _ERR, 503: _ERR},
)
async def create_trade_draft_card(
    body: schemas.TradeDraftCardRequest,
    container: Ctn,
    user: User,
    authorization: Annotated[str | None, Header()] = None,
    idempotency_key: IdempotencyKey = None,
):
    session = await _owned_session(body.session_id, user, container)
    if session is None:
        return error_response("SESSION_NOT_FOUND", "会话不存在", 404)
    # 红线：external 工具也必须在会话白名单内（scope 是唯一权威来源）
    if _AGENTOS_DRAFT_TOOL not in session.scope.tools:
        return error_response(
            "TOOL_NOT_IN_SCOPE", f"工具 {_AGENTOS_DRAFT_TOOL} 不在会话白名单", 403
        )
    if container.agentos_client is None:
        return error_response("INTERNAL", "交易真链路未配置（缺 AGENT_CORE_ACTOR_TOKEN）", 503)

    now = _now()
    trace_id = new_trace_id()
    user_token = bearer_token(authorization)
    if user_token is None:
        return error_response("UNAUTHENTICATED", "缺少 USER Bearer Token", 401)
    # 客户端只选择 strategy_id；Core 根据 Runtime 的 AGENT Token 校验绑定归属，
    # Runtime 再精确交叉校验绑定账户，不接受客户端传账户或 Mandate。
    try:
        readiness_response = await container.agentos_client.get_user_trading_readiness(user_token)
        readiness = parse_trading_readiness(
            readiness_response,
            expected_agent_id=container.settings.core_credential_agent_id,
            expected_strategy_id=body.strategy_id,
        )
        require_draft_ready(readiness)
        execution = await resolve_strategy_execution_context(
            container.agentos_client,
            body.strategy_id,
            expected_agent_id=container.settings.core_credential_agent_id,
        )
        if (
            execution.unified_account_id != readiness.unified_account_id
            or execution.venue_account_id != readiness.venue_account_id
            or execution.agent_trading_account_id != readiness.agent_trading_account_id
            or execution.mandate_id != readiness.mandate_id
        ):
            raise TradingReadinessError(
                "Core USER Readiness 与 AGENT Strategy Binding 账户上下文不一致"
            )
    except TradingReadinessError as exc:
        return error_response("POLICY_VIOLATION", str(exc)[:256], 409, trace_id=trace_id)
    except StrategyBindingError as exc:
        return error_response("POLICY_VIOLATION", f"策略绑定不可执行：{exc}", 409)
    except httpx.HTTPError as exc:
        return _map_upstream_error(
            exc,
            observer=container.core_credential_readiness,
            trace_id=trace_id,
        )

    try:
        stable_idempotency_key = idempotency_key or (
            "idem_agent_text_"
            + args_hash(
                {
                    "session_id": body.session_id,
                    "strategy_id": body.strategy_id,
                    "text": body.text,
                }
            )
        )
        draft, card = await create_draft_from_text(
            container.agentos_client,
            container.llm,
            body.text,
            unified_account_id=execution.unified_account_id,
            venue_account_id=execution.venue_account_id,
            strategy_id=execution.strategy_id,
            mandate_id=execution.mandate_id,
            default_leverage=container.settings.agentos_default_leverage,
            default_max_slippage=container.settings.agentos_default_max_slippage,
            idempotency_key=stable_idempotency_key,
        )
    except IntentParseError as exc:
        return error_response("INVALID_REQUEST", f"无法解析交易意图：{exc}", 400)
    except httpx.HTTPError as exc:
        return _map_upstream_error(
            exc,
            observer=container.core_credential_readiness,
            trace_id=trace_id,
        )

    data = draft.get("data") or {}
    draft_id = data.get("id")
    # 留痕（与 tool-calls 一致：每次受控写都记账）
    await container.audit_store.record_tool_call(
        AuditRecord(
            trace_id=trace_id,
            session_id=body.session_id,
            tool_name=_AGENTOS_DRAFT_TOOL,
            args_hash=args_hash(
                {
                    "text": body.text,
                    "strategy_id": execution.strategy_id,
                    "unified_account_id": execution.unified_account_id,
                    "venue_account_id": execution.venue_account_id,
                }
            ),
            result_ref=draft_id or "no_draft",
            created_at=now,
            status="ok",
        )
    )
    await container.session_store.update(session.touch(now, state="text_active"))
    return schemas.TradeDraftCard(
        card=card,
        draft_id=draft_id,
        core_draft_id=draft_id,
        draft_version=data.get("version"),
        status=data.get("status"),
        draft=draft,
        trace_id=trace_id,
    )


async def _archive_voice_turns(
    container: Ctn,
    session: Session,
    segments: Sequence[schemas.TranscriptTurn],
    now: datetime,
) -> None:
    """把上报的语音字幕归档进历史库。

    失败只记日志：这个端点的本职是上报语音证据时间戳（ASR 静默监控、
    首字延迟都靠它），历史库抖动不该把语音链路的可观测性一起拖下水。

    去重靠 (role, content, created_at) —— created_at 取上报里的 started_at，
    所以重传的批次天然命中。App 是异步批量上报，网络重试会重发同一批，
    而重复历史是用户可见的数据损坏。
    """

    store = getattr(container, "conversation_store", None)
    if store is None:
        return
    try:
        for turn in pair_transcript(segments, fallback=now):
            if await store.has_message(session.session_id, "user", turn.user_text, turn.at):
                continue
            await store.record_turn(
                session.session_id,
                session.user_id,
                turn.user_text,
                turn.assistant_text,
                turn.at,
                modality="voice",
            )
    except Exception:
        logger.warning(
            "语音字幕归档失败，本次不进历史 session_id=%s", session.session_id, exc_info=True
        )


async def _owned_conversation(conversation_id: str, user: User, container: Ctn):
    """只返回属于该 Core USER 的历史会话。

    与 _owned_session 同一套语义：不存在与跨用户一律折叠成同一个 404。
    用状态码区分两者，等于给了一个枚举别人 conversation_id 的接口。
    """

    convo = await container.conversation_store.get_conversation(conversation_id)
    if convo is None or convo.user_id != user.user_id:
        return None
    return convo


@router.get(
    "/conversations",
    response_model=schemas.ConversationPage,
    summary="历史会话列表（cursor 分页）",
    responses={400: _ERR, 401: _ERR, 403: _ERR, 503: _ERR},
)
async def list_conversations(
    container: Ctn,
    user: User,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    try:
        items, next_cursor = await container.conversation_store.list_conversations(
            user.user_id, limit, cursor
        )
    except ValueError as exc:
        # 只有游标解析会抛 ValueError
        return error_response("INVALID_REQUEST", f"游标不合法：{exc}", 400)
    return schemas.ConversationPage(
        items=[_conversation_summary(convo) for convo in items],
        next_cursor=next_cursor,
    )


def _conversation_summary(convo: ConversationSummary) -> schemas.ConversationSummary:
    """领域记录 → 对外投影。列表与重命名共用这一份字段映射。

    这里就是 `session_id` → `conversation_id` 的唯一映射点（见 spec §3.4）：
    两处各写一遍的话，改一处漏一处就会出现「列表给的字段名和重命名返回的
    不一样」这种前端最难查的不一致。
    """

    return schemas.ConversationSummary(
        conversation_id=convo.session_id,
        title=convo.title,
        modality=convo.modality,
        created_at=convo.created_at,
        last_message_at=convo.last_message_at,
        message_count=convo.message_count,
    )


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除历史会话（真删，不可恢复）",
    responses={401: _ERR, 403: _ERR, 404: _ERR, 503: _ERR},
)
async def delete_conversation(
    conversation_id: str,
    container: Ctn,
    user: User,
):
    if await _owned_conversation(conversation_id, user, container) is None:
        return error_response("SESSION_NOT_FOUND", "会话不存在", 404)

    await container.conversation_store.delete_conversation(conversation_id)
    # 运行时会话也要关掉。只删历史的话，下一轮对话会因为 record_turn 是懒
    # 创建而把会话行重新建出来 —— 用户会发现删掉的历史又回来了。
    # 绝大多数删除发生在会话早已过期之后，delete 对不存在的 id 是 no-op。
    await container.session_store.delete(conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/conversations/{conversation_id}",
    response_model=schemas.ConversationSummary,
    summary="重命名历史会话",
    responses={401: _ERR, 403: _ERR, 404: _ERR, 422: _ERR, 503: _ERR},
)
async def rename_conversation(
    conversation_id: str,
    body: schemas.ConversationRenameRequest,
    container: Ctn,
    user: User,
) -> schemas.ConversationSummary | JSONResponse:
    if await _owned_conversation(conversation_id, user, container) is None:
        return error_response("SESSION_NOT_FOUND", "会话不存在", 404)

    # 不带 only_if：用户意图无条件胜出。异步 titler 那边反过来带守卫，
    # 所以它之后完成也不会把这个名字冲掉（见 app/history/titler.py）。
    await container.conversation_store.set_title(conversation_id, body.title)

    convo = await container.conversation_store.get_conversation(conversation_id)
    if convo is None:
        # 极窄的竞态：改名与清理循环撞上了。当作已不存在处理。
        return error_response("SESSION_NOT_FOUND", "会话不存在", 404)
    return _conversation_summary(convo)


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=schemas.ConversationMessagePage,
    summary="历史对话回放（cursor 分页，正序）",
    responses={400: _ERR, 401: _ERR, 403: _ERR, 404: _ERR, 503: _ERR},
)
async def list_conversation_messages(
    conversation_id: str,
    container: Ctn,
    user: User,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    if await _owned_conversation(conversation_id, user, container) is None:
        return error_response("SESSION_NOT_FOUND", "会话不存在", 404)
    try:
        items, next_cursor = await container.conversation_store.list_messages(
            conversation_id, limit, cursor
        )
    except ValueError as exc:
        return error_response("INVALID_REQUEST", f"游标不合法：{exc}", 400)
    return schemas.ConversationMessagePage(
        items=[
            schemas.ConversationMessage(
                id=str(message.id),
                role=message.role,
                content=message.content,
                created_at=message.created_at,
            )
            for message in items
        ],
        next_cursor=next_cursor,
    )


@router.post(
    "/conversations/{conversation_id}/resume",
    response_model=schemas.SessionCredentials,
    summary="从历史会话继续提问（重建运行时会话并换发凭证）",
    responses={401: _ERR, 403: _ERR, 404: _ERR, 429: _ERR, 503: _ERR},
)
async def resume_conversation(
    conversation_id: str,
    body: schemas.SessionCreateRequest,
    container: Ctn,
    user: User,
    idempotency_key: IdempotencyKey = None,
) -> schemas.SessionCredentials | JSONResponse:
    if await _owned_conversation(conversation_id, user, container) is None:
        return error_response("SESSION_NOT_FOUND", "会话不存在", 404)

    now = _now()
    trace_id = new_trace_id()
    # scope 一律按当前策略重算，**绝不从历史快照恢复**。Scope.tools 是 RT
    # 二次校验的唯一权威来源（app/runtime/session.py 的 Scope 注释）——沿用
    # 旧白名单意味着策略收紧后，从历史进来的会话仍持有已被收回的工具。
    scope = Scope(agents=("avatar", "strategy"), tools=container.registry.names())

    session = await container.session_store.get(conversation_id)
    if session is None or session.user_id != user.user_id:
        session = await _rehydrate_session(container, conversation_id, user.user_id, scope, now)
        await container.session_store.create(session)
    else:
        # 会话还活着：不重建、不重复灌历史，只更新活跃时间。
        session = session.touch(now)
        await container.session_store.update(session)

    # 无论上面走了哪个分支，都必须换发新票。rt_token 只活
    # rt_token_ttl_seconds（默认 300s），而会话活 3600s——命中分支下客户端
    # 手里的旧 token 基本必然已过期，沿用就是连不上 WS。
    return _session_credentials(container, session, scope, None, trace_id, now)


async def _rehydrate_session(
    container: Ctn, conversation_id: str, user_id: str, scope: Scope, now: datetime
) -> Session:
    """从持久化记录重建运行时会话，并灌回最近几轮上下文。

    逐轮走 with_conversation_turn 而不是直接塞 turns：那个方法带 3 轮 /
    6000 字的裁剪，绕过它就等于把窗口上限做成了两套。
    """

    session = Session(
        session_id=conversation_id,
        user_id=user_id,
        modality="text",
        scope=scope,
        state="created",
        created_at=now,
        last_activity_at=now,
    )
    try:
        turns = await container.conversation_store.recent_turns(
            conversation_id, MAX_HISTORY_TURNS
        )
    except Exception:
        # 上下文灌不回来只是「AI 不记得前面聊了什么」，比整个进不去好得多。
        logger.warning("历史上下文恢复失败，会话以空上下文重建", exc_info=True)
        return session
    for user_text, assistant_text in turns:
        session = session.with_conversation_turn(
            now, ConversationTurn(user_text, assistant_text, now)
        )
    return session


@router.get(
    "/sessions/{session_id}/history",
    response_model=schemas.HistoryPage,
    summary="[审计] 单会话的工具调用记录 —— 不是对话历史",
    description=(
        "命名有误导性：返回的是工具调用审计记录，不是对话消息，且 cursor 未实现。"
        "历史对话请用 /conversations 系列接口。"
    ),
    deprecated=True,
    responses={401: _ERR, 403: _ERR, 404: _ERR, 503: _ERR},
)
async def get_history(
    session_id: str,
    container: Ctn,
    user: User,
    cursor: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(le=200)] = 50,
):
    session = await _owned_session(session_id, user, container)
    if session is None:
        return error_response("SESSION_NOT_FOUND", "会话不存在", 404)
    records = await container.audit_store.list_by_session(session_id)
    items = [
        schemas.HistoryMessage(
            id=f"audit_{i}",
            role="tool",
            source="text",
            content=f"{r.tool_name} args_hash={r.args_hash} ref={r.result_ref}",
            created_at=r.created_at,
        )
        for i, r in enumerate(records[:limit])
    ]
    return schemas.HistoryPage(items=items, next_cursor=None)


@router.post(
    "/sessions/{session_id}/history",
    status_code=status.HTTP_202_ACCEPTED,
    summary="异步归档语音字幕（App 收到 transcript done 后上报，可选）",
    responses={401: _ERR, 403: _ERR, 404: _ERR, 503: _ERR},
)
async def archive_transcript(
    session_id: str,
    body: schemas.TranscriptArchiveRequest,
    container: Ctn,
    user: User,
    idempotency_key: IdempotencyKey = None,
):
    session = await _owned_session(session_id, user, container)
    if session is None:
        return error_response("SESSION_NOT_FOUND", "会话不存在", 404)
    # Session 上只留证据时间戳，不留正文（正文属于历史库，见下面的归档）。
    now = _now()
    await _archive_voice_turns(container, session, body.turns, now)
    roles = {turn.role for turn in body.turns if turn.text.strip()}
    session = replace(
        session,
        last_activity_at=now,
        voice_asr_started_at=(
            (session.voice_asr_started_at or now)
            if "user" in roles
            else session.voice_asr_started_at
        ),
        voice_user_transcript_at=(now if "user" in roles else session.voice_user_transcript_at),
        voice_assistant_transcript_at=(
            now if "assistant" in roles else session.voice_assistant_transcript_at
        ),
    )
    await container.session_store.update(session)
    if "user" in roles:
        _cancel_asr_silence_monitor(container, session_id)
    logger.info(
        "voice_transcript_evidence session_id=%s room_hash=%s user_seen=%s assistant_seen=%s",
        session_id,
        _safe_id(session.voice_room_id),
        "user" in roles,
        "assistant" in roles,
    )
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={"accepted": len(body.turns)})
