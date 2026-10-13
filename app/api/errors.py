"""统一错误信封响应 {code, message, details, trace_id}。"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Protocol, cast

import httpx
from fastapi.responses import JSONResponse

from app.api.schemas import ErrorCode, ErrorEnvelope
from app.tools.agentos.trade_flow import MarketInstrumentResolutionError
from app.tools.core_client import MarketDataError

logger = logging.getLogger(__name__)


def new_trace_id() -> str:
    return "trc_" + uuid.uuid4().hex


def error_response(
    code: ErrorCode,
    message: str,
    status_code: int,
    details: dict[str, Any] | None = None,
    trace_id: str | None = None,
) -> JSONResponse:
    envelope = ErrorEnvelope(
        code=code, message=message, details=details, trace_id=trace_id or new_trace_id()
    )
    return JSONResponse(status_code=status_code, content=envelope.model_dump())


def market_resolution_error_payload(
    exc: MarketInstrumentResolutionError,
    *,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Map Catalog resolution failures without exposing raw Core/model input."""

    code: ErrorCode
    if exc.status_code == 400:
        code = "INVALID_REQUEST"
    elif exc.status_code == 403:
        code = "FORBIDDEN"
    else:
        code = "INTERNAL"
    return {
        "code": code,
        "message": exc.message,
        "details": {
            "reason_code": exc.reason_code,
            "retryable": exc.status_code >= 500,
            "credential_refresh_required": False,
        },
        "trace_id": trace_id or new_trace_id(),
    }


def market_resolution_error_response(
    exc: MarketInstrumentResolutionError,
    *,
    trace_id: str | None = None,
) -> JSONResponse:
    payload = market_resolution_error_payload(exc, trace_id=trace_id)
    return JSONResponse(status_code=exc.status_code, content=payload)


def market_resolution_tool_error(exc: MarketInstrumentResolutionError) -> dict[str, Any]:
    payload = market_resolution_error_payload(exc)
    details = cast(dict[str, Any], payload["details"])
    return {
        "status": "error",
        "error_code": payload["code"],
        "message": payload["message"],
        "reason_code": details["reason_code"],
        "retryable": details["retryable"],
        "credential_refresh_required": False,
    }


# Core 错误码 → (对外 ErrorCode, HTTP status)。未知一律 502 INTERNAL。
_UPSTREAM_ERR_MAP: dict[str, tuple[ErrorCode, int, str]] = {
    "INVALID_REQUEST": ("INVALID_REQUEST", 400, "Core 拒绝了请求参数"),
    "UNPROCESSABLE": ("INVALID_REQUEST", 422, "Core 无法处理该请求"),
    "IDEMPOTENCY_CONFLICT": ("IDEMPOTENCY_CONFLICT", 409, "请求幂等键冲突"),
    "INVALID_STATE_TRANSITION": ("POLICY_VIOLATION", 409, "当前状态不允许此操作"),
    "USER_CONFIRMATION_REQUIRED": ("POLICY_VIOLATION", 403, "需要用户确认"),
    "FORBIDDEN": ("FORBIDDEN", 403, "当前智能体无权执行此操作"),
    "MANDATE_LIMIT_EXCEEDED": ("POLICY_VIOLATION", 409, "当前交易计划超过允许额度"),
    "UNWIND_AUTHORIZATION_REQUIRED": (
        "POLICY_VIOLATION",
        409,
        "当前交易计划需要额外的钱包授权",
    ),
    "IDEMPOTENCY_KEY_REUSED": ("IDEMPOTENCY_CONFLICT", 409, "请求幂等键冲突"),
    "RESOURCE_NOT_FOUND": ("INVALID_REQUEST", 404, "Core 资源不存在"),
    "NOT_FOUND": ("INVALID_REQUEST", 404, "Core 资源不存在"),
    # 我方 Agent token 失效属配置问题，不暴露成用户 401
    "UNAUTHENTICATED": ("INTERNAL", 503, "智能体服务凭证暂不可用"),
    "INSUFFICIENT_SCOPE": ("INTERNAL", 503, "智能体服务凭证暂不可用"),
}


class CoreCredentialObserver(Protocol):
    def observe_market_data_error(self, exc: MarketDataError) -> None: ...

    def observe_core_http_error(
        self, *, status_code: int, upstream_code: str, request_id: str | None
    ) -> None: ...


_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _safe_upstream_error(exc: httpx.HTTPStatusError) -> tuple[str, str | None, bool]:
    try:
        payload = exc.response.json()
    except ValueError:
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    err = payload.get("error")
    err = err if isinstance(err, dict) else {}
    core_code = str(err.get("code") or "")[:128]
    request_id = str(
        payload.get("request_id") or exc.response.headers.get("X-Request-ID") or ""
    )[:128] or None
    # Core 的响应体不保证带 retryable：429 与 5xx 在传输语义上本来就是可重试的，
    # 只因为对方没写这个字段就标成不可重试，会让上游白白放弃一次退避重试。
    # 但「没写」和「明确写了 false」必须区分开——后者是 Core 在说"别再打了"
    # （配额永久耗尽、计划内维护），状态码兜底不能把它翻回 true。
    explicit_retryable = err.get("retryable")
    retryable = (
        explicit_retryable
        if isinstance(explicit_retryable, bool)
        else exc.response.status_code in _RETRYABLE_STATUS
    )
    return core_code, request_id, retryable


def _map_upstream_error(
    exc: httpx.HTTPError,
    *,
    observer: CoreCredentialObserver | None = None,
    trace_id: str | None = None,
) -> JSONResponse:
    """Map Core errors without forwarding its body, message, or traceback."""

    if isinstance(exc, httpx.TimeoutException):
        if observer is not None:
            observer.observe_core_http_error(
                status_code=504, upstream_code="CORE_TIMEOUT", request_id=None
            )
        return error_response(
            "TOOL_UPSTREAM_TIMEOUT",
            "Core 请求超时",
            504,
            details={
                "upstream_code": "CORE_TIMEOUT",
                "request_id": None,
                "retryable": True,
                "credential_refresh_required": False,
            },
            trace_id=trace_id,
        )
    if isinstance(exc, httpx.RequestError):
        if observer is not None:
            observer.observe_core_http_error(
                status_code=503, upstream_code="CORE_UNAVAILABLE", request_id=None
            )
        return error_response(
            "INTERNAL",
            "Core 服务暂不可用",
            503,
            details={
                "upstream_code": "CORE_UNAVAILABLE",
                "request_id": None,
                "retryable": True,
                "credential_refresh_required": False,
            },
            trace_id=trace_id,
        )
    if not isinstance(exc, httpx.HTTPStatusError):
        return error_response("INTERNAL", "Core 返回了非预期错误", 502, trace_id=trace_id)
    core_code, request_id, retryable = _safe_upstream_error(exc)
    credential_failure = (
        exc.response.status_code == 401
        or core_code in {"UNAUTHENTICATED", "INSUFFICIENT_SCOPE"}
    )
    if observer is not None and credential_failure:
        observer.observe_core_http_error(
            status_code=exc.response.status_code,
            upstream_code=core_code,
            request_id=request_id,
        )
    logger.warning(
        "core_http_error method=%s path=%s status=%s upstream_code=%s request_id=%s "
        "retryable=%s credential_failure=%s",
        exc.request.method,
        exc.request.url.path,
        exc.response.status_code,
        core_code or "UNKNOWN",
        request_id,
        retryable,
        credential_failure,
    )
    fallback: tuple[ErrorCode, int, str]
    if exc.response.status_code == 401:
        fallback = (cast(ErrorCode, "INTERNAL"), 503, "智能体服务凭证暂不可用")
    elif exc.response.status_code == 403:
        fallback = (cast(ErrorCode, "FORBIDDEN"), 403, "当前智能体无权执行此操作")
    elif exc.response.status_code == 400:
        fallback = (cast(ErrorCode, "INVALID_REQUEST"), 400, "Core 拒绝了请求参数")
    elif exc.response.status_code == 404:
        fallback = (cast(ErrorCode, "INVALID_REQUEST"), 404, "Core 资源不存在")
    elif exc.response.status_code == 409:
        fallback = (cast(ErrorCode, "POLICY_VIOLATION"), 409, "Core 当前状态不允许该操作")
    elif exc.response.status_code == 422:
        fallback = (cast(ErrorCode, "INVALID_REQUEST"), 422, "Core 无法处理该请求")
    elif exc.response.status_code == 429:
        fallback = (cast(ErrorCode, "RATE_LIMITED"), 429, "Core 请求频率受限，请稍后重试")
    elif exc.response.status_code in {502, 503}:
        fallback = (cast(ErrorCode, "INTERNAL"), 503, "Core 服务暂不可用")
    elif exc.response.status_code == 504:
        fallback = (cast(ErrorCode, "TOOL_UPSTREAM_TIMEOUT"), 504, "Core 请求超时")
    else:
        fallback = (cast(ErrorCode, "INTERNAL"), 502, "Core 返回了非预期错误")
    code, status_code, message = _UPSTREAM_ERR_MAP.get(core_code, fallback)
    return error_response(
        code,
        message,
        status_code,
        details={
            "upstream_code": core_code or None,
            "request_id": request_id,
            "retryable": retryable,
            "credential_refresh_required": (
                core_code == "UNAUTHENTICATED" or exc.response.status_code == 401
            ),
        },
        trace_id=trace_id,
    )


def core_http_error_event(
    exc: httpx.HTTPError,
    *,
    observer: CoreCredentialObserver | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """WebSocket form of the same safe Core error contract used by REST."""

    response = _map_upstream_error(exc, observer=observer, trace_id=trace_id)
    payload = json.loads(bytes(response.body))
    return payload if isinstance(payload, dict) else {
        "code": "INTERNAL",
        "message": "Core 返回了非预期错误",
        "details": None,
        "trace_id": trace_id or new_trace_id(),
    }


def core_http_tool_error(
    exc: httpx.HTTPError,
    *,
    observer: CoreCredentialObserver | None = None,
) -> dict[str, Any]:
    """Function-callback form; never includes Core's raw body or URL."""

    response = _map_upstream_error(exc, observer=observer)
    payload = json.loads(bytes(response.body))
    details = payload.get("details") if isinstance(payload, dict) else None
    safe_details = details if isinstance(details, dict) else {}
    return {
        "status": "error",
        "error_code": payload.get("code", "INTERNAL"),
        "message": payload.get("message", "Core 返回了非预期错误"),
        "upstream_code": safe_details.get("upstream_code"),
        "request_id": safe_details.get("request_id"),
        "retryable": safe_details.get("retryable", False),
        "credential_refresh_required": safe_details.get(
            "credential_refresh_required", False
        ),
    }


def market_data_error_details(exc: MarketDataError) -> dict[str, str | bool | None]:
    return {
        "upstream_code": exc.code,
        "request_id": exc.request_id,
        "retryable": exc.retryable,
        "credential_refresh_required": exc.credential_refresh_required,
    }


def market_data_error_mapping(exc: MarketDataError) -> tuple[ErrorCode, str, int]:
    if exc.code in {"INVALID_REQUEST", "INVALID_SYMBOL", "UNSUPPORTED_SYMBOL"}:
        return "INVALID_REQUEST", "行情查询参数无效", 400
    if exc.code == "MARKET_DATA_RATE_LIMITED":
        return "RATE_LIMITED", "行情服务请求频率受限，请稍后重试", 429
    if exc.code == "MARKET_DATA_TIMEOUT":
        return "TOOL_UPSTREAM_TIMEOUT", "行情服务请求超时", 504
    if exc.code == "MARKET_DATA_UNAVAILABLE":
        return "INTERNAL", "行情服务暂不可用", 503
    if exc.code in {"UNAUTHENTICATED", "FORBIDDEN", "INSUFFICIENT_SCOPE"}:
        return "INTERNAL", "智能体服务凭证暂不可用", 503
    return "INTERNAL", "行情服务返回了非预期错误", 502


def market_data_error_response(
    exc: MarketDataError,
    *,
    observer: CoreCredentialObserver | None = None,
    trace_id: str | None = None,
) -> JSONResponse:
    if observer is not None:
        observer.observe_market_data_error(exc)
    code, message, status_code = market_data_error_mapping(exc)
    return error_response(
        code,
        message,
        status_code,
        details=market_data_error_details(exc),
        trace_id=trace_id,
    )


def market_data_error_event(
    exc: MarketDataError,
    *,
    observer: CoreCredentialObserver | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    if observer is not None:
        observer.observe_market_data_error(exc)
    code, message, _status_code = market_data_error_mapping(exc)
    return {
        "code": code,
        "message": message,
        "details": market_data_error_details(exc),
        "trace_id": trace_id or new_trace_id(),
    }


def market_data_tool_error(
    exc: MarketDataError,
    *,
    observer: CoreCredentialObserver | None = None,
) -> dict[str, Any]:
    if observer is not None:
        observer.observe_market_data_error(exc)
    code, message, _status_code = market_data_error_mapping(exc)
    return {
        "status": "error",
        "error_code": code,
        "message": message,
        **market_data_error_details(exc),
    }
