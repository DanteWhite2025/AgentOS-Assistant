"""Followin MCP 薄客户端测试。

关键路径：SSE 解析、session 自愈（4xx → 重握手 → 重试一次）、
二次失败抛错。session TTL 上游未说明，自愈是唯一可靠兜底。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.news.followin_client import (
    FakeFollowinClient,
    FollowinClient,
    FollowinUpstreamError,
    parse_sse_json,
)

_URL = "https://mcp.followin.io/v2/mcp"
_FIXTURES = Path(__file__).parent.parent / "fixtures"


def _sse(payload: dict) -> str:
    return f"event: message\ndata: {json.dumps(payload)}\n\n"


def _sse_raw(data_text: str) -> str:
    """构造 data 行内容不是 JSON 序列化产物的原始 SSE 文本（用于畸形输入测试）。"""
    return f"event: message\ndata: {data_text}\n\n"


def _init_response() -> httpx.Response:
    body = _sse({
        "jsonrpc": "2.0", "id": 1,
        "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "followx-mcp"}},
    })
    return httpx.Response(200, text=body, headers={"mcp-session-id": "SESSION-A"})


def _tool_response(fixture: str) -> httpx.Response:
    inner = json.loads((_FIXTURES / fixture).read_text())
    body = _sse({
        "jsonrpc": "2.0", "id": 9,
        "result": {"content": [{"type": "text", "text": json.dumps(inner)}]},
    })
    return httpx.Response(200, text=body)


def test_parse_sse_json_extracts_data_line() -> None:
    assert parse_sse_json(_sse({"a": 1})) == {"a": 1}


def test_parse_sse_json_rejects_body_without_data() -> None:
    with pytest.raises(FollowinUpstreamError):
        parse_sse_json("event: ping\n\n")


async def test_fetch_news_handshakes_then_calls_tool() -> None:
    async with respx.mock(assert_all_called=True) as mock:
        route = mock.post(_URL)
        route.side_effect = [
            _init_response(),                 # initialize
            httpx.Response(202),              # notifications/initialized
            _tool_response("followin_media.json"),
        ]
        async with httpx.AsyncClient() as http:
            client = FollowinClient("key", http, mcp_url=_URL)
            data = await client.fetch_news(sources=["media"], limit=50)
    assert len(data["results"]["articles"]) == 2
    assert data["meta"]["status"] == "ok"


async def test_sends_api_key_and_dual_accept_header() -> None:
    async with respx.mock() as mock:
        route = mock.post(_URL)
        route.side_effect = [
            _init_response(), httpx.Response(202), _tool_response("followin_media.json"),
        ]
        async with httpx.AsyncClient() as http:
            await FollowinClient("secret-key", http, mcp_url=_URL).fetch_news(limit=1)
        request = route.calls[0].request
    assert request.headers["x-api-key"] == "secret-key"
    accept = request.headers["accept"]
    assert "application/json" in accept and "text/event-stream" in accept


async def test_stale_session_is_rehandshaked_and_retried_once() -> None:
    async with respx.mock() as mock:
        route = mock.post(_URL)
        route.side_effect = [
            _init_response(), httpx.Response(202),
            httpx.Response(404, text="session not found"),   # session 过期
            _init_response(), httpx.Response(202),           # 自愈重握手
            _tool_response("followin_trending.json"),        # 重试成功
        ]
        async with httpx.AsyncClient() as http:
            data = await FollowinClient("key", http, mcp_url=_URL).fetch_news(limit=50)
    assert len(data["results"]["articles"]) == 2


async def test_second_failure_raises_upstream_error() -> None:
    async with respx.mock() as mock:
        route = mock.post(_URL)
        route.side_effect = [
            _init_response(), httpx.Response(202),
            httpx.Response(404), _init_response(), httpx.Response(202), httpx.Response(404),
        ]
        async with httpx.AsyncClient() as http:
            with pytest.raises(FollowinUpstreamError) as excinfo:
                await FollowinClient("key", http, mcp_url=_URL).fetch_news(limit=1)
    assert excinfo.value.status_code == 404


async def test_timeout_is_flagged() -> None:
    async with respx.mock() as mock:
        mock.post(_URL).side_effect = httpx.ConnectTimeout("timed out")
        async with httpx.AsyncClient() as http:
            with pytest.raises(FollowinUpstreamError) as excinfo:
                await FollowinClient("key", http, mcp_url=_URL).fetch_news(limit=1)
    assert excinfo.value.is_timeout is True


async def test_tool_error_result_raises() -> None:
    body = _sse({"jsonrpc": "2.0", "id": 9, "error": {"code": -32602, "message": "bad args"}})
    async with respx.mock() as mock:
        route = mock.post(_URL)
        route.side_effect = [_init_response(), httpx.Response(202), httpx.Response(200, text=body)]
        async with httpx.AsyncClient() as http:
            with pytest.raises(FollowinUpstreamError):
                await FollowinClient("key", http, mcp_url=_URL).fetch_news(limit=1)


async def test_fake_client_returns_both_route_shapes() -> None:
    fake = FakeFollowinClient()
    search = await fake.fetch_news(sources=["media"], limit=5)
    trending = await fake.fetch_news(limit=5)
    assert search["results"]["articles"][0]["source_url"]
    # trending route 没有 source_url —— Fake 必须复刻这个差异，否则 normalizer 测不到
    assert "source_url" not in trending["results"]["articles"][0]


def test_parse_sse_json_rejects_malformed_json_in_data_line() -> None:
    """data 行存在但内容不是合法 JSON（如网络截断）——不该让裸 JSONDecodeError 逃逸。"""
    with pytest.raises(FollowinUpstreamError):
        parse_sse_json("event: message\ndata: not-json-at-all\n\n")


def test_parse_sse_json_rejects_json_array() -> None:
    """data 行是合法 JSON 但不是对象（是数组）——同样不是我们要的形状。"""
    with pytest.raises(FollowinUpstreamError):
        parse_sse_json(_sse_raw("[1, 2, 3]"))


async def test_fetch_news_raises_when_tool_text_is_malformed_json() -> None:
    """result.content[0].text 不是合法 JSON（上游返回了人类可读的错误提示文本）。"""
    body = _sse({
        "jsonrpc": "2.0", "id": 9,
        "result": {"content": [{"type": "text", "text": "抱歉，工具执行失败，请稍后重试"}]},
    })
    async with respx.mock() as mock:
        route = mock.post(_URL)
        route.side_effect = [_init_response(), httpx.Response(202), httpx.Response(200, text=body)]
        async with httpx.AsyncClient() as http:
            with pytest.raises(FollowinUpstreamError):
                await FollowinClient("key", http, mcp_url=_URL).fetch_news(limit=1)


async def test_fetch_news_raises_when_tool_text_parses_to_non_dict() -> None:
    """text 解析出来是合法 JSON，但不是 dict（是数组）。"""
    body = _sse({
        "jsonrpc": "2.0", "id": 9,
        "result": {"content": [{"type": "text", "text": "[1, 2]"}]},
    })
    async with respx.mock() as mock:
        route = mock.post(_URL)
        route.side_effect = [_init_response(), httpx.Response(202), httpx.Response(200, text=body)]
        async with httpx.AsyncClient() as http:
            with pytest.raises(FollowinUpstreamError):
                await FollowinClient("key", http, mcp_url=_URL).fetch_news(limit=1)


async def test_unwrap_tool_result_raises_when_content_is_empty() -> None:
    """result.content 是空列表——同样应该稳定抛 FollowinUpstreamError，而不是别的异常。"""
    body = _sse({"jsonrpc": "2.0", "id": 9, "result": {"content": []}})
    async with respx.mock() as mock:
        route = mock.post(_URL)
        route.side_effect = [_init_response(), httpx.Response(202), httpx.Response(200, text=body)]
        async with httpx.AsyncClient() as http:
            with pytest.raises(FollowinUpstreamError):
                await FollowinClient("key", http, mcp_url=_URL).fetch_news(limit=1)


async def test_unwrap_tool_result_raises_when_no_text_part() -> None:
    """result.content 里没有 type=="text" 的项——同样应该稳定抛 FollowinUpstreamError。"""
    body = _sse({
        "jsonrpc": "2.0", "id": 9,
        "result": {"content": [{"type": "image", "data": "base64..."}]},
    })
    async with respx.mock() as mock:
        route = mock.post(_URL)
        route.side_effect = [_init_response(), httpx.Response(202), httpx.Response(200, text=body)]
        async with httpx.AsyncClient() as http:
            with pytest.raises(FollowinUpstreamError):
                await FollowinClient("key", http, mcp_url=_URL).fetch_news(limit=1)


# 修复 4：mcp-session-id 从未被断言过——变异测试证明删掉整个 session 分支后
# test_followin_client.py 原有 15 个测试仍全绿。respx 不校验请求头，
# 生产后果不是降级而是全停：每次 tools/call 被上游拒 → 自愈重握手 → 再拒 →
# FollowinUpstreamError，新闻永远采不进来。
async def test_tool_call_request_carries_handshake_session_id() -> None:
    async with respx.mock() as mock:
        route = mock.post(_URL)
        route.side_effect = [
            _init_response(), httpx.Response(202), _tool_response("followin_media.json"),
        ]
        async with httpx.AsyncClient() as http:
            await FollowinClient("key", http, mcp_url=_URL).fetch_news(limit=1)
    # calls[0]=initialize（此时还没有 session，不带头）
    # calls[1]=notifications/initialized，calls[2]=tools/call —— 这两个才该带
    # 握手响应头里返回的 mcp-session-id（fixture 里是 SESSION-A）
    assert "mcp-session-id" not in route.calls[0].request.headers
    assert route.calls[1].request.headers["mcp-session-id"] == "SESSION-A"
    assert route.calls[2].request.headers["mcp-session-id"] == "SESSION-A"


# 修复 5：Followin 上游（followx-mcp 2.0.0）已改为无状态模式，initialize
# 响应不再下发 mcp-session-id 头。旧实现把这个头当硬前置条件，缺了就直接
# FollowinUpstreamError——三条 route 全挂，新闻库悄悄停更（真机 8/28 之后
# 再没有新数据，直到这次真机探测才发现）。无 session id 时必须继续按无状态
# 模式工作，而不是报错。
async def _init_response_without_session_id() -> httpx.Response:
    body = _sse({
        "jsonrpc": "2.0", "id": 1,
        "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "followx-mcp"}},
    })
    return httpx.Response(200, text=body)  # 无 mcp-session-id 头


async def test_handshake_without_session_id_falls_back_to_stateless_mode() -> None:
    async with respx.mock() as mock:
        route = mock.post(_URL)
        route.side_effect = [
            await _init_response_without_session_id(),
            httpx.Response(202),
            _tool_response("followin_media.json"),
        ]
        async with httpx.AsyncClient() as http:
            data = await FollowinClient("key", http, mcp_url=_URL).fetch_news(limit=50)
    assert len(data["results"]["articles"]) == 2
    # 无 session id 时不该发出这个头（发了空字符串同样是错的）
    assert "mcp-session-id" not in route.calls[1].request.headers
    assert "mcp-session-id" not in route.calls[2].request.headers


async def test_stateless_mode_does_not_rehandshake_on_every_call() -> None:
    """握手判定必须是"是否已握手"，不能是"是否拿到了 session_id"——否则无状态
    模式下 self._session_id 永远是 None，每次 fetch_news 都会重新握手一遍。"""
    async with respx.mock() as mock:
        route = mock.post(_URL)
        route.side_effect = [
            await _init_response_without_session_id(),
            httpx.Response(202),
            _tool_response("followin_media.json"),
            _tool_response("followin_trending.json"),
        ]
        async with httpx.AsyncClient() as http:
            client = FollowinClient("key", http, mcp_url=_URL)
            await client.fetch_news(limit=50)
            await client.fetch_news(limit=50)
    # 只握手一次（initialize + notifications/initialized）+ 两次 tools/call = 4 次调用
    assert len(route.calls) == 4


async def test_5xx_does_not_trigger_rehandshake() -> None:
    """4xx 触发自愈重握手（已有测试覆盖），5xx 不该——这条边界此前只有实现，没有测试锁住。"""
    async with respx.mock() as mock:
        route = mock.post(_URL)
        route.side_effect = [
            _init_response(), httpx.Response(202),
            httpx.Response(500, text="internal error"),
        ]
        async with httpx.AsyncClient() as http:
            with pytest.raises(FollowinUpstreamError) as excinfo:
                await FollowinClient("key", http, mcp_url=_URL).fetch_news(limit=1)
    assert excinfo.value.status_code == 500
    # 只有 3 次调用（初始化 + 通知 + 一次 tools/call）；5xx 不该触发第二次握手/重试
    assert len(route.calls) == 3
