"""受控工具执行的公共落点（资金安全红线）。

不管指令是从文本 WS、rt_token 旁挂通道，还是 WebRTC 直连通话里回传的，都必须
走这一条路径：以 ``session.scope.tools`` 二次强校验工具名（绝不信任客户端上报的
名字）→ 过 mandate/policies → 由服务端持 AGENT 凭证调 Core → 全程留痕。

端点层只负责把 ``ToolExecutionOutcome`` 映射成各自的响应模型。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from fastapi.responses import JSONResponse

from app.api.errors import (
    _map_upstream_error,
    error_response,
    market_data_error_response,
    market_resolution_error_response,
    new_trace_id,
)
from app.audit.hashing import args_hash
from app.audit.store import AuditRecord
from app.container import Container
from app.policies.engine import PolicyViolationError
from app.runtime.session import Session
from app.telemetry.spans import set_tool_status, tool_span
from app.tools.agentos.trade_flow import (
    MarketInstrumentResolutionError,
    TradingReadinessError,
)
from app.tools.core_client import MarketDataError
from app.tools.executor import ToolNotAllowedError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolExecutionOutcome:
    """执行结果。``error`` 非空即代表已生成对外错误响应，端点直接返回即可。"""

    trace_id: str
    output: dict[str, Any] | None = None
    trade_draft_id: str | None = None
    error: JSONResponse | None = None


async def execute_controlled_tool(
    container: Container,
    session: Session,
    tool_name: str,
    arguments: dict[str, Any],
    now: datetime,
    *,
    user_bearer_token: str | None = None,
    idempotency_key: str | None = None,
    intent_source: str = "TEXT",
) -> ToolExecutionOutcome:
    """执行一个受控工具。

    ``user_bearer_token`` 是调用者本人的 Core USER 凭据。私有账户工具
    （readiness / balances / positions / draft）在 Core 侧按「本人」鉴权，
    缺它会被 executor 直接拒成 TradingReadinessError —— 凡是拿得到 USER
    Token 的入口都必须透传，绝不能拿 AGENT Token 冒名顶替。
    """

    trace_id = new_trace_id()
    hashed = args_hash(arguments)

    async def _audit(result_ref: str, status: str) -> None:
        # 每次 Tool Call 都留痕（含被拒绝的）
        set_tool_status(span, status)
        await container.audit_store.record_tool_call(
            AuditRecord(
                trace_id=trace_id,
                session_id=session.session_id,
                tool_name=tool_name,
                args_hash=hashed,
                result_ref=result_ref,
                created_at=now,
                status=status,
            )
        )

    # 只落 hash 与键名：参数原文里有金额与账户标识，与审计同一条红线
    # （app/audit/hashing.py：只落 hash 与指针，不落原文）。键名不敏感，
    # 但足以判断是哪种调用形态。
    arg_keys = ",".join(sorted(arguments)) or "-"

    def _failed(error: JSONResponse, code: str, reason: str) -> ToolExecutionOutcome:
        # 线上排查的唯一线索：uvicorn 只记 HTTP 状态码，端点的错误体不落盘。
        logger.warning(
            "tool_execution_failed trace_id=%s session_id=%s tool_name=%s "
            "error_code=%s http_status=%s reason=%s args_hash=%s arg_keys=%s",
            trace_id,
            session.session_id,
            tool_name,
            code,
            error.status_code,
            reason[:200],
            hashed,
            arg_keys,
        )
        return ToolExecutionOutcome(trace_id=trace_id, error=error)

    # 一次受控工具执行 = 一个 span。链路上要能直接看到「哪个工具、哪个会话、
    # 什么结局、耗时多少」，而不是只看到一条 200 的 HTTP 请求。
    with tool_span(tool_name, session_id=session.session_id, args_hash=hashed) as span:
        try:
            out = await container.executor.execute(
                tool_name,
                session.user_id,
                arguments,
                list(session.scope.tools),
                user_bearer_token=user_bearer_token,
                idempotency_key=idempotency_key,
                intent_source=intent_source,
            )
        except ToolNotAllowedError:
            await _audit("not_in_scope", "rejected")
            return _failed(
                error_response("TOOL_NOT_IN_SCOPE", f"工具 {tool_name} 不在会话白名单", 403),
                "TOOL_NOT_IN_SCOPE",
                "not_in_scope",
            )
        except PolicyViolationError as exc:
            await _audit(exc.reason, "rejected")
            return _failed(
                error_response("POLICY_VIOLATION", exc.reason, 403),
                "POLICY_VIOLATION",
                exc.reason,
            )
        except MarketDataError as exc:
            await _audit(exc.request_id or "market_data_error", "tool_error")
            return _failed(
                market_data_error_response(
                    exc,
                    observer=container.core_credential_readiness,
                    trace_id=trace_id,
                ),
                f"MARKET_DATA:{exc.code}",
                exc.request_id or "market_data_error",
            )
        except MarketInstrumentResolutionError as exc:
            await _audit(exc.reason_code, "tool_error")
            return _failed(
                market_resolution_error_response(exc, trace_id=trace_id),
                exc.reason_code,
                str(exc),
            )
        except httpx.HTTPError as exc:
            await _audit("core_http_error", "tool_error")
            return _failed(
                _map_upstream_error(
                    exc,
                    observer=container.core_credential_readiness,
                    trace_id=trace_id,
                ),
                "CORE_HTTP_ERROR",
                type(exc).__name__,
            )
        except TradingReadinessError as exc:
            await _audit("trading_readiness_error", "tool_error")
            return _failed(
                error_response("POLICY_VIOLATION", str(exc)[:256], 409, trace_id=trace_id),
                "TRADING_READINESS",
                str(exc),
            )
        except ValueError as exc:
            await _audit("invalid_tool_arguments", "tool_error")
            return _failed(
                error_response("INVALID_REQUEST", str(exc)[:256], 400, trace_id=trace_id),
                "INVALID_REQUEST",
                str(exc),
            )

        draft_id = out.get("trade_draft_id")
        await _audit(draft_id or f"{tool_name}:{session.session_id}", "ok")
        return ToolExecutionOutcome(trace_id=trace_id, output=out, trade_draft_id=draft_id)
