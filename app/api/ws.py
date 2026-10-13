"""文本对话 WebSocket（通道②）。

事件字典见 docs/api/agent-mobile/ws-events.md。
会话鉴权用 rt_token（query token）；可选的 Authorization USER Bearer
只在当前连接内校验并透传给私有账户工具，不写入 Session。上行 user.message/user.interrupt/ping，
下行 session.state / response.* / tool.* / avatar.event / pong / error。
response.text.delta 是模型的真实 token 流（runner.run_turn_stream），不是把完整
答案切片模拟出来的。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Mapping
from contextlib import aclosing, suppress
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from app.agents.runner import ToolOutcome, TurnDone, TurnStage, TurnTextDelta
from app.api import schemas
from app.api.deps import bearer_token
from app.api.errors import (
    core_http_error_event,
    market_data_error_event,
    market_resolution_error_payload,
)
from app.auth.core_user import UserAuthenticationError
from app.config import get_settings
from app.container import Container, build_container
from app.runtime.stage_display import describe_stage
from app.runtime.suggestion_split import SuggestionSplitter
from app.tools.agentos.trade_flow import (
    MarketInstrumentResolutionError,
    TradingReadinessError,
)
from app.tools.core_client import MarketDataError
from app.tools.registry import describe_tool_call
from app.voice.volc_stream_asr import (
    FinalTranscript,
    VolcStreamASRError,
    VolcStreamASRRetryableError,
    VolcStreamASRSession,
)
from app.voice.ws_audio import AudioLane

router = APIRouter()


def _turn_idempotency_key(
    user_id: str,
    session_id: str,
    client_message_id: object,
    response_id: str,
) -> str:
    """Derive one stable write key for every tool call in a WS user turn.

    Mobile already sends ``client_msg_id``. Reusing it after reconnect produces
    the same key, while a different user or session cannot collide. Older
    clients fall back to the server response id so a write is still protected
    within that turn instead of failing because HTTP headers do not exist on
    individual WebSocket messages.
    """

    supplied = str(client_message_id).strip() if client_message_id is not None else ""
    turn_id = supplied or response_id
    digest = hashlib.sha256(f"{user_id}\0{session_id}\0{turn_id}".encode()).hexdigest()
    return f"ws_turn_{digest}"


def _container(websocket: WebSocket):
    state = websocket.app.state
    container = getattr(state, "container", None)
    if container is None:
        container = build_container(get_settings())
        state.container = container
    return container


# tool.result 的对外状态。runner 的内部分档直接映射过来，技术细节（超时、
# 参数错误、上游异常）一律收敛成 no_data——产品口径是「不要给用户很多报错，
# 除非真的是阻塞性错误」，排查信息走 blockers 和日志，不进用户视野。
_TOOL_RESULT_SUMMARY = {
    "no_data": "未找到相关信息",
    "cancelled": "已取消",
}


def _tool_result_payload(outcome: ToolOutcome) -> dict[str, Any]:
    summary = outcome.user_text if outcome.status == "blocked" else ""
    return {
        "call_id": outcome.call_id,
        "name": outcome.tool_name or (outcome.executed.name if outcome.executed else ""),
        "status": outcome.status,
        "summary": summary or _TOOL_RESULT_SUMMARY.get(outcome.status, ""),
        "trade_draft_id": outcome.executed.trade_draft_id if outcome.executed else None,
    }


class _Seq:
    def __init__(self) -> None:
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


async def _send(ws: WebSocket, seq: _Seq, type_: str, session_id: str, data: dict) -> None:
    await ws.send_json({"type": type_, "seq": seq.next(), "session_id": session_id, "data": data})


@router.websocket("/v1/agent/stream")
async def stream(websocket: WebSocket) -> None:
    session_id = websocket.query_params.get("session_id", "")
    token = websocket.query_params.get("token")
    await websocket.accept()
    seq = _Seq()
    container = _container(websocket)

    now = datetime.now(UTC)
    claims = container.token_service.verify_rt_token(token, now) if token else None
    session = await container.session_store.get(session_id) if session_id else None
    if (
        claims is None
        or claims.session_id != session_id
        or session is None
        or claims.user_id != session.user_id
    ):
        await _send(websocket, seq, "error", session_id, {"code": "UNAUTHORIZED", "message": "rt_token 无效或会话不存在"})
        await websocket.close()
        return

    authorization = websocket.headers.get("authorization")
    user_token = bearer_token(authorization)
    if authorization is not None and user_token is None:
        await _send(
            websocket,
            seq,
            "error",
            session_id,
            {"code": "UNAUTHORIZED", "message": "缺少有效的 USER Bearer Token"},
        )
        await websocket.close()
        return
    if user_token is not None:
        try:
            user = await container.user_authenticator.authenticate(user_token)
        except UserAuthenticationError as exc:
            code = (
                "UNAUTHORIZED"
                if exc.code == "UNAUTHENTICATED"
                else "FORBIDDEN"
                if exc.code == "FORBIDDEN"
                else "INTERNAL"
            )
            await _send(
                websocket,
                seq,
                "error",
                session_id,
                {
                    "code": code,
                    "message": exc.message,
                    "details": {
                        "retryable": exc.code == "AUTH_UPSTREAM_UNAVAILABLE",
                        "credential_refresh_required": exc.code == "UNAUTHENTICATED",
                    },
                },
            )
            await websocket.close()
            return
        if user.user_id != session.user_id or user.user_id != claims.user_id:
            await _send(
                websocket,
                seq,
                "error",
                session_id,
                {"code": "UNAUTHORIZED", "message": "USER Token 与会话用户不符"},
            )
            await websocket.close()
            return

    await _send(websocket, seq, "session.state", session_id, {"state": "text_active", "scope": {"tools": list(session.scope.tools)}})

    active_turn_task: asyncio.Task[None] | None = None

    async def _handle_turn(
        text: str,
        response_id: str,
        trace_id: str,
        now: datetime,
        web_search: bool,
        read_aloud: bool,
        idempotency_key: str,
    ) -> None:
        await _send(websocket, seq, "response.started", session_id, {"response_id": response_id})
        await _send(websocket, seq, "avatar.event", session_id, {"kind": "state", "value": "Thinking"})

        # 本轮已经发过 tool.invoked、还没收到终态的调用。用户中断时要给它们补一条
        # cancelled，否则 App 上那几行「正在查询…」会一直转到会话结束。
        in_flight: dict[str, str] = {}

        def _display_for(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
            """渲染展示文案，任何意外都降级成「只有工具名」而不是让这一轮出错。

            参数值来自模型输出，什么形状都可能有；文案渲染失败时少一行漂亮的
            中文远好过整轮对话挂掉。
            """

            fallback = {"title": name, "title_key": f"tool.{name}.running", "params": {}}
            tool = container.registry.get(name)
            if tool is None:
                return fallback
            try:
                return describe_tool_call(tool, arguments)
            except Exception:
                logger.warning("工具展示文案渲染失败 name=%s，降级为工具名", name, exc_info=True)
                return fallback

        async def _notify_tool_start(call_id: str, name: str, arguments: Mapping[str, Any]) -> None:
            in_flight[call_id] = name
            await _safe_send(
                "tool.invoked",
                {"call_id": call_id, "name": name, **_display_for(name, arguments)},
            )

        async def _notify_tool_end(outcome: ToolOutcome) -> None:
            in_flight.pop(outcome.call_id, None)
            await _safe_send("tool.result", _tool_result_payload(outcome))

        async def _safe_send(type_: str, data: dict[str, Any]) -> None:
            """工具事件推送失败不能影响本轮执行——客户端断线是常态，
            而工具可能已经产生了副作用，绝不能因为发不出去就中断。"""

            try:
                await _send(websocket, seq, type_, session_id, data)
            except Exception:
                logger.warning(
                    "%s 实时推送失败（客户端可能已断线），忽略后继续执行",
                    type_,
                    exc_info=True,
                )

        # 已经吐出去的增量。它同时是两件事的依据：response.text.done 的文本，
        # 以及"出错时要不要补一条 done 把这条消息收尾"的判断。
        streamed: list[str] = []
        speaking = False
        # 追问要以 chips 单独下发，不能同时留在正文尾巴上（否则同样三句话出现
        # 两遍）。剥离是边流边做的：正文该什么时候到还是什么时候到，不为了拿
        # 完整追问去缓冲整段——那等于把首字延迟又赔回去。
        splitter = SuggestionSplitter()
        # 「已思考 N 秒」的定格。语义是首字延迟：从收到用户消息到第一个真正的
        # 字出现，含工具执行那一段——这正是币安那个「已思考 8 秒 ›」的含义。
        #
        # 为什么服务端算而不让客户端掐表：客户端只能在收到首个 delta 时停表，
        # 而流式首块可能是空的（provider 的心跳/占位），算出「已思考 0 秒」比
        # 不显示更糟。空块在 LLM 层已经过滤掉了，这里再守一道——两处都便宜。
        turn_started_at = time.monotonic()
        thinking_reported = False

        async def _report_thinking_done() -> None:
            """首字之前定格一次耗时，每轮只发一次。

            出错和被打断的路径**不**补这一条：客户端收到 error 或自己发起
            interrupt 时本就会把进度行收掉，再来一句「已思考 0 秒」既没有信息
            量，也会把 error 和 Idle 之间插进一条无关事件。
            """

            nonlocal thinking_reported
            if thinking_reported:
                return
            thinking_reported = True
            elapsed_ms = int((time.monotonic() - turn_started_at) * 1000)
            await _safe_send(
                "response.thinking.done",
                {"response_id": response_id, "elapsed_ms": elapsed_ms},
            )
        # 02 方案音频通道：只有主链路是 pipeline 时才有。文本增量照旧实时下发，
        # 音频在后台边分句边合成边发，两条线互不阻塞。
        lane = (
            AudioLane(
                container.voice_pipeline,
                send=_safe_send,
                response_id=response_id,
            )
            if read_aloud and container.voice_pipeline is not None
            else None
        )

        async def _close_message() -> None:
            """把已经吐出去的内容收成一条完整消息。

            7 个异常分支原先都假设「异常发生时一个字都没发出去」。流式下会出现
            吐了半截才炸的情况，不补这条 done 的话客户端那条消息会永远停在
            「正在输入」状态。一个字都没吐过就不补——那会显示成一条空消息。
            """

            if not streamed:
                return
            await _safe_send(
                "response.text.done",
                {"response_id": response_id, "text": "".join(streamed)},
            )

        async def _cancel_in_flight() -> None:
            for call_id, name in list(in_flight.items()):
                await _safe_send(
                    "tool.result",
                    {
                        "call_id": call_id,
                        "name": name,
                        "status": "cancelled",
                        "summary": "已取消",
                        "trade_draft_id": None,
                    },
                )
            in_flight.clear()

        async def _end_turn(*, interrupted: bool = False) -> None:
            """一轮的统一收尾：在途工具补终态、音频收尾、数字人回待机。

            所有出口（正常结束、被打断、7 个错误分支）都走这里。散在各分支里
            各写一遍的话，新增一步收尾动作必然漏掉几处 —— 音频通道就是新增的
            那一步：不收尾的话后台合成任务会挂着，客户端也等不到终态。
            """

            await _cancel_in_flight()
            if lane is not None:
                # 被打断的那一轮作废，绝不能再吐音频；正常结束要等已合成的发完。
                if interrupted:
                    await lane.abort()
                else:
                    await lane.finish()
            await _send(
                websocket, seq, "avatar.event", session_id, {"kind": "state", "value": "Idle"}
            )

        # 每轮都要重新取会话，不能用 session。它是握手那一刻的快照（本函数外层
        # 的闭包变量，这里没有 nonlocal），而多轮历史是 runner 在每轮收尾时写回
        # 存储的——拿旧快照喂模型，第二轮就看不到第一轮说过什么，"他""该基金"
        # 这类指代又会串到别人身上（就是这次要修的那个事故）。
        #
        # 取不到（会话已过期）就用握手快照兜着：这一轮没有历史，比直接报错好。
        current = await container.session_store.get(session_id) or session

        try:
            # 真流式：delta 直接来自模型的 token 流。
            #
            # aclosing 是必须的——消费方提前退出不会自动关闭被迭代的生成器，
            # 而 runner 那条流底下还套着 LLM 流（带 span 和 HTTP 连接）。runner
            # 已经把「关外层 = 关全部内层」钉死了，这里只要管好最外层这一层。
            async with aclosing(
                container.runner.run_turn_stream(
                    current,
                    text,
                    trace_id,
                    now,
                    user_bearer_token=user_token,
                    idempotency_key=idempotency_key,
                    on_tool_start=_notify_tool_start,
                    on_tool_end=_notify_tool_end,
                    web_search=web_search,
                )
            ) as turn:
                async for event in turn:
                    if isinstance(event, TurnStage):
                        await _safe_send(
                            "response.stage",
                            {"response_id": response_id, **describe_stage(event.stage)},
                        )
                    elif isinstance(event, TurnTextDelta):
                        if not event.delta:
                            # 空增量不算首字：不渲染、不停表、不切 Speaking。
                            continue
                        visible = splitter.feed(event.delta)
                        if not visible:
                            # 整段都被扣住了（标记前缀、或已进入追问区）——它还
                            # 不是可见的字，同样不能停表或切 Speaking。
                            continue
                        await _report_thinking_done()
                        if not speaking:
                            # 第一个字到了才切 Speaking——在此之前是 Thinking，
                            # 这正是真流式相对假流式的可见差别。
                            speaking = True
                            await _send(
                                websocket,
                                seq,
                                "avatar.event",
                                session_id,
                                {"kind": "state", "value": "Speaking"},
                            )
                        streamed.append(visible)
                        await _send(
                            websocket,
                            seq,
                            "response.text.delta",
                            session_id,
                            {"response_id": response_id, "delta": visible},
                        )
                        if lane is not None:
                            await lane.feed(visible)
                    elif isinstance(event, TurnDone) and event.result.blockers:
                        # WS 通道不下发 blockers（那是内部错误码，产品口径是
                        # 少报错），但它是排查的主要线索，至少要落一条日志——
                        # 此前这条链路上它完全不可见。
                        logger.info(
                            "turn_blockers session_id=%s trace_id=%s blockers=%s",
                            session_id,
                            trace_id,
                            list(event.result.blockers),
                        )
        except asyncio.CancelledError:
            logger.info("Turn task cancelled by user.interrupt session_id=%s", session_id)
            await _close_message()
            # 在途的工具已经发过 tool.invoked 但拿不到终态了，补一条 cancelled，
            # 否则 App 上那几行「正在查询…」会一直转到会话结束。
            await _end_turn(interrupted=True)
            raise
        except MarketDataError as exc:
            await _close_message()
            await _send(
                websocket,
                seq,
                "error",
                session_id,
                market_data_error_event(
                    exc,
                    observer=container.core_credential_readiness,
                    trace_id=trace_id,
                ),
            )
            await _end_turn()
            return
        except MarketInstrumentResolutionError as exc:
            await _close_message()
            await _send(
                websocket,
                seq,
                "error",
                session_id,
                market_resolution_error_payload(exc, trace_id=trace_id),
            )
            await _end_turn()
            return
        except httpx.HTTPError as exc:
            await _close_message()
            await _send(
                websocket,
                seq,
                "error",
                session_id,
                core_http_error_event(
                    exc,
                    observer=container.core_credential_readiness,
                    trace_id=trace_id,
                ),
            )
            await _end_turn()
            return
        except TradingReadinessError as exc:
            await _close_message()
            await _send(
                websocket,
                seq,
                "error",
                session_id,
                {
                    "code": "POLICY_VIOLATION",
                    "message": str(exc)[:256],
                    "details": {"retryable": False},
                    "trace_id": trace_id,
                },
            )
            await _end_turn()
            return
        except ValueError as exc:
            await _close_message()
            await _send(
                websocket,
                seq,
                "error",
                session_id,
                {
                    "code": "INVALID_REQUEST",
                    "message": str(exc)[:256],
                    "details": {"retryable": False},
                    "trace_id": trace_id,
                },
            )
            await _end_turn()
            return
        except Exception:
            logger.exception("Unhandled error during run_turn")
            # 兜底分支恰恰最需要收尾：命名分支覆盖的是已知的上游错误，任何没
            # 预料到的故障都落在这里，漏了的话那条消息永远停在「正在输入」。
            await _close_message()
            await _send(
                websocket,
                seq,
                "error",
                session_id,
                {
                    "code": "INTERNAL_ERROR",
                    "message": "大模型服务异常或中转站报错，请稍后再试。",
                    "details": {"retryable": True},
                    "trace_id": trace_id,
                },
            )
            await _end_turn()
            return

        # tool.result 已经由 on_tool_end 实时发过了，这里不再批量补发——
        # 过去那种整轮跑完才补的方式，让 result 和答案文本几乎同时到达，
        # 前端根本没机会显示中间过程。

        # 收流：把扣住的尾巴还给正文，把解析出来的追问单独下发。
        #
        # tail 非空只有两种情况，两种都意味着前面已经有正文了（所以不必在这里
        # 重复切 Speaking）：模型没按格式输出走了降级，或者流正好停在标记前缀上。
        tail, questions = splitter.finish()
        if tail:
            streamed.append(tail)
            await _safe_send(
                "response.text.delta", {"response_id": response_id, "delta": tail}
            )
            if lane is not None:
                await lane.feed(tail)

        # 一个字都没吐出来的轮次（工具全失败、早退文案）在这里补定格。必须排在
        # done 之前：客户端收到 done 就把那条消息收尾了，之后再来一条「已思考
        # N 秒」是错的顺序。有文本的轮次早在首个 delta 时就发过，这里幂等跳过。
        await _report_thinking_done()

        # 解析不出追问时不发空事件——前端少一个「questions 是空数组」的分支。
        # 排在 done 之前：chips 要跟着那条消息一起渲染。
        if questions:
            await _safe_send(
                "response.suggestions",
                {"response_id": response_id, "questions": list(questions)},
            )

        # done 的文本用累积的 delta，而不是 result.text。模型在调工具前说的前言
        # 进了 delta 流但不进 RunResult.text（那里只有最后一轮的答案）——直接用
        # result.text 会让客户端拿最终答案覆盖掉已经显示的前言。
        await _send(
            websocket,
            seq,
            "response.text.done",
            session_id,
            {"response_id": response_id, "text": "".join(streamed)},
        )
        # 音频通道在这里收尾：等已合成的分片发完再报 Idle，否则数字人会在
        # 声音还没播完时就回待机。
        await _end_turn()

        # 同样必须重新取：runner 刚在这一轮把历史写进了存储，拿握手快照
        # update 会把它整体覆盖掉（Redis 的 update 是整体 set、无 CAS），
        # 于是历史永远只剩最后一轮——而且不报错。
        latest = await container.session_store.get(session_id)
        if latest is not None:
            await container.session_store.update(latest.touch(now, state="text_active"))

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _send(
                    websocket,
                    seq,
                    "error",
                    session_id,
                    {"code": "INVALID_REQUEST", "message": "非法 JSON"},
                )
                continue

            mtype = msg.get("type")
            if mtype == "ping":
                await _send(websocket, seq, "pong", session_id, {})
                continue
            if mtype == "user.interrupt":
                if active_turn_task is not None and not active_turn_task.done():
                    active_turn_task.cancel()
                    active_turn_task = None
                await _send(
                    websocket,
                    seq,
                    "avatar.event",
                    session_id,
                    {"kind": "state", "value": "Idle"},
                )
                continue
            if mtype != "user.message":
                await _send(
                    websocket,
                    seq,
                    "error",
                    session_id,
                    {"code": "INVALID_REQUEST", "message": f"未知事件 {mtype}"},
                )
                continue

            if active_turn_task is not None and not active_turn_task.done():
                active_turn_task.cancel()

            data = msg.get("data") or {}
            text = data.get("text", "")
            web_search = data.get("web_search") is True
            read_aloud = data.get("read_aloud") is True
            response_id = "r_" + uuid.uuid4().hex[:8]
            trace_id = "trc_" + uuid.uuid4().hex
            now = datetime.now(UTC)
            idempotency_key = _turn_idempotency_key(
                session.user_id,
                session_id,
                data.get("client_msg_id"),
                response_id,
            )

            active_turn_task = asyncio.create_task(
                _handle_turn(
                    text,
                    response_id,
                    trace_id,
                    now,
                    web_search,
                    read_aloud,
                    idempotency_key,
                )
            )
    except WebSocketDisconnect:
        return
    finally:
        if active_turn_task is not None and not active_turn_task.done():
            active_turn_task.cancel()


# ---------------------------------------------------------------------------
# 双向流式语音识别（通道③）：边说边出字。见 docs/api/agent-mobile/ws-events.md
# § 5。与通道②不同，这条连接不绑定一个既有的 Agent Session——它只认用户身份，
# 一条连接内可以反复 audio.start → audio.end 做很多段独立的录音（对应移动端
# 反复按住说话）。
# ---------------------------------------------------------------------------

_STREAM_FINAL_TRANSCRIPT_GRACE_SECONDS: float = 5.0


async def _authenticate_stream_token(
    container: Container, token: str | None, now: datetime
) -> str | None:
    """接受 RT_TOKEN（会话短期票）或 USER_TOKEN 两种，任一有效即放行。

    这条通道不绑定 session_id，所以 rt_token 只取它的 user_id，不做 session
    归属校验——那是通道②的规则，这里没有 session 可比对。
    """

    if not token:
        return None
    claims = container.token_service.verify_rt_token(token, now)
    if claims is not None:
        return claims.user_id
    try:
        user = await container.user_authenticator.authenticate(token)
    except UserAuthenticationError:
        return None
    return user.user_id


async def _send_stream_error(websocket: WebSocket, code: str, message: str) -> None:
    """发一条 error 事件。发不出去就算了——客户端可能已经断线。"""

    event = schemas.AudioStreamErrorEvent(data=schemas.AudioStreamErrorData(code=code, message=message))
    with suppress(Exception):
        await websocket.send_json(event.model_dump())


async def _await_audio_start(websocket: WebSocket) -> schemas.AudioStreamStartData | None:
    """等下一个 ``audio.start``；期间的坏输入只报错，不掉线。返回 None 表示客户端已断开。"""

    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return None
        if message.get("bytes") is not None:
            await _send_stream_error(websocket, "INVALID_REQUEST", "还没有 audio.start，先收到了音频数据")
            continue
        text = message.get("text")
        if text is None:
            continue
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            await _send_stream_error(websocket, "INVALID_REQUEST", "非法 JSON")
            continue
        if not isinstance(event, dict) or event.get("type") != "audio.start":
            got = event.get("type") if isinstance(event, dict) else "(非 JSON 对象)"
            await _send_stream_error(websocket, "INVALID_REQUEST", f"需要先发送 audio.start，收到 {got}")
            continue
        data = event.get("data")
        try:
            return schemas.AudioStreamStartData.model_validate(data if isinstance(data, dict) else {})
        except ValidationError as exc:
            first_error = exc.errors()[0]["msg"] if exc.errors() else "参数非法"
            await _send_stream_error(websocket, "INVALID_REQUEST", f"audio.start 参数非法：{first_error}")
            continue


async def _pump_client_audio_to_upstream(websocket: WebSocket, session: VolcStreamASRSession) -> str:
    """把客户端发来的音频转发给火山，直到 ``audio.end`` 或断线。

    返回 ``"ended"``（正常结束，已调用 session.finish()）或 ``"disconnected"``。
    """

    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return "disconnected"

        raw_bytes = message.get("bytes")
        if raw_bytes is not None:
            await session.send_audio(raw_bytes)
            continue

        text = message.get("text")
        if text is None:
            continue
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            await _send_stream_error(websocket, "INVALID_REQUEST", "非法 JSON")
            continue
        if not isinstance(event, dict):
            await _send_stream_error(websocket, "INVALID_REQUEST", "事件必须是 JSON 对象")
            continue

        etype = event.get("type")
        if etype == "audio.data":
            payload = event.get("data")
            if not isinstance(payload, str):
                await _send_stream_error(websocket, "INVALID_REQUEST", "audio.data 缺少 base64 音频")
                continue
            try:
                chunk = base64.b64decode(payload, validate=True)
            except (ValueError, binascii.Error):
                await _send_stream_error(websocket, "INVALID_REQUEST", "audio.data 不是合法的 base64")
                continue
            await session.send_audio(chunk)
        elif etype == "audio.end":
            await session.finish()
            return "ended"
        elif etype == "audio.start":
            await _send_stream_error(websocket, "INVALID_REQUEST", "已在一段录音中，请先发送 audio.end")
        else:
            await _send_stream_error(websocket, "INVALID_REQUEST", f"未知事件 {etype}")


async def _relay_stream_transcripts(websocket: WebSocket, session: VolcStreamASRSession) -> None:
    """把火山的识别事件转成 transcript.delta / transcript.done 下发。

    上游失败在这里兜住，转成一条 error 事件——绝不把异常炸到外层连接上，
    这一段录音失败不该连累同一条 WS 上后面还能重试的段。
    """

    try:
        async for event in session.events():
            is_final = isinstance(event, FinalTranscript)
            transcript_event: schemas.TranscriptDeltaEvent | schemas.TranscriptDoneEvent = (
                schemas.TranscriptDoneEvent(data=schemas.AudioStreamTranscriptData(text=event.text, is_final=True))
                if is_final
                else schemas.TranscriptDeltaEvent(
                    data=schemas.AudioStreamTranscriptData(text=event.text, is_final=False)
                )
            )
            await websocket.send_json(transcript_event.model_dump())
    except VolcStreamASRError as exc:
        retryable = isinstance(exc, VolcStreamASRRetryableError)
        # 只记异常类型，绝不记 exc 本身——它可能带回声的 Key。
        logger.warning("audio_stream_upstream_failed retryable=%s error_type=%s", retryable, type(exc).__name__)
        await _send_stream_error(websocket, "ASR_UPSTREAM_ERROR", "语音识别服务暂时不可用")


async def _run_one_audio_stream_turn(websocket: WebSocket, stream_client: Any) -> bool:
    """跑完一段「audio.start ~ audio.end」。返回 False 表示客户端已断开连接。"""

    start_data = await _await_audio_start(websocket)
    if start_data is None:
        return False

    try:
        session = await stream_client.open_session()
        await session.start(
            audio_format=start_data.format,
            sample_rate=start_data.sample_rate,
            channels=start_data.channels,
        )
    except VolcStreamASRError as exc:
        logger.warning("audio_stream_open_failed error_type=%s", type(exc).__name__)
        await _send_stream_error(websocket, "ASR_UPSTREAM_ERROR", "语音识别服务暂不可用，请重试")
        return True

    client_task = asyncio.create_task(_pump_client_audio_to_upstream(websocket, session))
    relay_task = asyncio.create_task(_relay_stream_transcripts(websocket, session))
    disconnected = False
    try:
        # client_task 是这条连接消息流的唯一读者：它必须读到 audio.end 或断线
        # 才能停，绝不能因为 relay_task 提前收尾（上游出错、或识别提前定稿）
        # 就被取消——那样会把它还没来得及读的消息（比如客户端后续的音频分片、
        # 甚至下一段的 audio.start）留在接收队列里，串进下一轮，协议就乱了。
        try:
            outcome = await client_task
        except Exception:
            logger.warning("audio_stream_client_pump_failed", exc_info=True)
            outcome = "disconnected"
        disconnected = outcome == "disconnected"

        if disconnected:
            relay_task.cancel()
        elif not relay_task.done():
            # 客户端已经喊停：给 relay 一点时间把剩下的增量和定稿发完。
            _, still_pending = await asyncio.wait({relay_task}, timeout=_STREAM_FINAL_TRANSCRIPT_GRACE_SECONDS)
            if relay_task in still_pending:
                relay_task.cancel()

        with suppress(asyncio.CancelledError, Exception):
            await relay_task
    finally:
        with suppress(Exception):
            await session.close()

    return not disconnected


@router.websocket("/v1/agent/audio/stream")
async def audio_stream(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token")
    container = _container(websocket)
    await websocket.accept()

    user_id = await _authenticate_stream_token(container, token, datetime.now(UTC))
    if user_id is None:
        await websocket.close(code=4001, reason="token 无效或已过期")
        return

    stream_client = container.stream_asr_client
    if stream_client is None:
        await _send_stream_error(websocket, "ASR_UPSTREAM_ERROR", "语音识别服务暂不可用")
        await websocket.close(code=1011)
        return

    try:
        while True:
            if not await _run_one_audio_stream_turn(websocket, stream_client):
                return
    except WebSocketDisconnect:
        return
