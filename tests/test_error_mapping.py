"""Core 上游错误映射 —— retryable 判定。

`retryable` 会作为 details 下发给客户端，也是上游决定「静默重试还是降级」的
唯一依据。旧实现完全信任 Core 响应体里的 `error.retryable`，Core 返回 429 或
503 但没写这个字段时，我们就把它当成不可重试，白白放弃一次退避重试。
"""

from __future__ import annotations

import httpx
import pytest

from app.api.errors import _safe_upstream_error


def _exc(status_code: int, payload: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://core.example/v1/resource")
    response = httpx.Response(status_code, json=payload or {}, request=request)
    return httpx.HTTPStatusError("upstream failed", request=request, response=response)


@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
def test_transport_level_statuses_are_retryable_without_body_flag(status_code: int) -> None:
    _code, _request_id, retryable = _safe_upstream_error(_exc(status_code))
    assert retryable is True


@pytest.mark.parametrize("status_code", [400, 401, 403, 404, 409, 422])
def test_client_errors_stay_non_retryable(status_code: int) -> None:
    """4xx（除 429）是请求本身的问题，重试多少次都一样。"""
    _code, _request_id, retryable = _safe_upstream_error(_exc(status_code))
    assert retryable is False


def test_explicit_body_flag_still_wins() -> None:
    """Core 明确标注 retryable 时以它为准——状态码兜底只是补充，不是覆盖。"""
    _code, _request_id, retryable = _safe_upstream_error(
        _exc(409, {"error": {"code": "INVALID_STATE_TRANSITION", "retryable": True}})
    )
    assert retryable is True


@pytest.mark.parametrize("status_code", [429, 503])
def test_explicit_false_is_not_overridden_by_status_fallback(status_code: int) -> None:
    """Core 在可重试状态码上显式说了 retryable: false，那是「别再打了」的信号
    （配额永久耗尽、计划内维护），状态码兜底不能把它翻回 true。"""
    _code, _request_id, retryable = _safe_upstream_error(
        _exc(status_code, {"error": {"code": "QUOTA_EXHAUSTED", "retryable": False}})
    )
    assert retryable is False


def test_malformed_body_does_not_break_classification() -> None:
    request = httpx.Request("GET", "https://core.example/v1/resource")
    response = httpx.Response(503, content=b"<html>gateway</html>", request=request)
    exc = httpx.HTTPStatusError("upstream failed", request=request, response=response)

    core_code, request_id, retryable = _safe_upstream_error(exc)

    assert core_code == ""
    assert request_id is None
    assert retryable is True
