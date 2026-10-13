"""WebRTC 实时语音网关的控制面端点（前缀 /api/voice 由 main.py 挂载）。

架构分工（见 docs/api/agent-mobile/voice-webrtc.md）：
- 数据面：客户端与云端大模型 WebRTC 毫秒级直连，音频流不经过本服务转发；
- 控制面：凭证签发、Tool 风控执行、通话结账全部走服务端。

本模块只放后两件事。凭证签发仍由 /v1/agent/sessions 与 /v1/agent/sessions/{id}/token
负责（会话生命周期在那里），此处不重复开口子。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Header

from app.api import schemas
from app.api.deps import (
    bearer_token,
    get_authenticated_user,
    get_container,
    owned_session,
)
from app.api.errors import error_response, new_trace_id
from app.api.tool_execution import execute_controlled_tool
from app.audit.hashing import args_hash
from app.audit.store import AuditRecord
from app.auth.core_user import AuthenticatedUser
from app.container import Container
from app.voice.usage import VoiceUsageRecord, compute_billed_units

logger = logging.getLogger(__name__)

router = APIRouter(tags=["voice-gateway"])

_ERR = {"model": schemas.ErrorEnvelope}
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
Ctn = Annotated[Container, Depends(get_container)]
User = Annotated[AuthenticatedUser, Depends(get_authenticated_user)]


def _now() -> datetime:
    return datetime.now(UTC)


@router.post(
    "/tools/execute",
    response_model=schemas.VoiceToolExecuteResult,
    summary="直连通话的 Tool Call 回服务端执行（scope + mandate 风控）",
    responses={400: _ERR, 401: _ERR, 403: _ERR, 404: _ERR, 409: _ERR, 502: _ERR, 504: _ERR},
)
async def execute_voice_tool(
    body: schemas.VoiceToolExecuteRequest,
    container: Ctn,
    user: User,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    idempotency_key: IdempotencyKey = None,
):
    session = await owned_session(body.session_id, user, container)
    if session is None:
        return error_response("SESSION_NOT_FOUND", "会话不存在", 404)

    outcome = await execute_controlled_tool(
        container,
        session,
        body.name,
        body.arguments,
        _now(),
        # 私有账户工具在 Core 侧按「本人」鉴权：漏传这一项，
        # get_balances / get_positions / get_trading_readiness / create_trade_draft
        # 会被 executor 拒成 TradingReadinessError。
        user_bearer_token=bearer_token(authorization),
        idempotency_key=idempotency_key,
        intent_source="VOICE",
    )
    if outcome.error is not None:
        return outcome.error
    return schemas.VoiceToolExecuteResult(
        tool_call_id=body.tool_call_id,
        name=body.name,
        status="ok",
        output=outcome.output,
        trade_draft_id=outcome.trade_draft_id,
        trace_id=outcome.trace_id,
    )


@router.post(
    "/session/end",
    response_model=schemas.VoiceSessionEndResult,
    summary="通话结账：记录用量流水、写回摘要、扣减额度",
    responses={401: _ERR, 404: _ERR, 503: _ERR},
)
async def end_voice_session(
    body: schemas.VoiceSessionEndRequest,
    container: Ctn,
    user: User,
):
    session = await owned_session(body.session_id, user, container)
    if session is None:
        return error_response("SESSION_NOT_FOUND", "会话不存在", 404)

    now = _now()
    trace_id = new_trace_id()
    settlement = await container.voice_usage_store.settle(
        VoiceUsageRecord(
            session_id=session.session_id,
            user_id=session.user_id,
            provider=body.provider,
            duration_seconds=body.duration_seconds,
            audio_tokens=body.usage_tokens.audio_tokens,
            text_tokens=body.usage_tokens.text_tokens,
            billed_units=compute_billed_units(
                body.duration_seconds,
                audio_tokens=body.usage_tokens.audio_tokens,
                text_tokens=body.usage_tokens.text_tokens,
                seconds_per_unit=container.settings.voice_billing_seconds_per_unit,
                tokens_per_unit=container.settings.voice_billing_tokens_per_unit,
            ),
            recorded_at=now,
        )
    )

    # 成本记账与配额结算是两套口径：这里存原始时长与 token，不存 billed_units
    # （spec D10）。重复上报本就不该产生第二笔成本，所以只在非 duplicate 时记。
    recorder = getattr(container, "usage_recorder", None)
    if recorder is not None and not settlement.duplicate:
        await recorder.record_voice(
            session_id=settlement.record.session_id,
            user_id=settlement.record.user_id,
            provider=settlement.record.provider,
            duration_seconds=Decimal(str(settlement.record.duration_seconds)),
            audio_tokens=settlement.record.audio_tokens,
            text_tokens=settlement.record.text_tokens,
            occurred_at=settlement.record.recorded_at,
        )

    # 摘要跟着结算一起幂等：重复挂断上报不得把记忆流灌满同一段摘要。
    episode_id: str | None = None
    summary = (body.transcript_summary or "").strip()
    if summary and not settlement.duplicate:
        episode = await container.memory_store.remember_episode(
            session.user_id,
            summary,
            details={
                "provider": body.provider,
                "duration_seconds": body.duration_seconds,
            },
            metadata={"source": "voice", "session_id": session.session_id},
        )
        episode_id = episode.episode_id

    await container.audit_store.record_tool_call(
        AuditRecord(
            trace_id=trace_id,
            session_id=session.session_id,
            tool_name="voice_session_end",
            # 时长转字符串：args_hash 的冻结规范禁 float（避免跨平台序列化差异）。
            args_hash=args_hash(
                {
                    "provider": body.provider,
                    "duration_seconds": str(body.duration_seconds),
                    "audio_tokens": body.usage_tokens.audio_tokens,
                    "text_tokens": body.usage_tokens.text_tokens,
                }
            ),
            result_ref=f"billed:{settlement.record.billed_units}",
            created_at=now,
            status="duplicate" if settlement.duplicate else "ok",
        )
    )

    # 只记账，不改会话状态：把会话标成已清理会让 DELETE /sessions/{id} 跳过
    # StopVoiceChat，遗留一个计费中的火山任务。停止链路仍归 DELETE 独有。
    await container.session_store.update(replace(session, last_activity_at=now))

    logger.info(
        "voice_session_settled session_id=%s provider=%s duration=%.1fs "
        "billed_units=%s duplicate=%s summary_stored=%s",
        session.session_id,
        body.provider,
        body.duration_seconds,
        settlement.record.billed_units,
        settlement.duplicate,
        episode_id is not None,
    )

    return schemas.VoiceSessionEndResult(
        session_id=session.session_id,
        provider=body.provider,
        state=session.state,
        duplicate=settlement.duplicate,
        billed_units=_decimal_text(settlement.record.billed_units),
        total_billed_units=_decimal_text(settlement.total_billed_units),
        remaining_units=(
            None
            if settlement.remaining_units is None
            else _decimal_text(settlement.remaining_units)
        ),
        memory_episode_id=episode_id,
        trace_id=trace_id,
    )


def _decimal_text(value: Decimal) -> str:
    return format(value.normalize(), "f")
