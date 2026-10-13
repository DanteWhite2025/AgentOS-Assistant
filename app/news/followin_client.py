"""Followin MCP 薄客户端（streamable HTTP + x-api-key）。

不用官方 `mcp` SDK：它目前只是 pydantic-ai 的传递依赖，未在 pyproject 声明，
在传递依赖上建生产采集路径不安全；而我们只用 tools/call 一个方法，
SDK 的 ClientSession 又是 async context manager，塞进常驻循环里 lifecycle
和仓库「httpx 客户端由容器创建、aclose() 统一关闭」的范式冲突。

实测要点：
- Accept 必须同时带 application/json 与 text/event-stream，少一个即失败
- 响应是 SSE 文本（event: message\\ndata: {...}），但内容一次给完，不必流式读
- 业务 JSON 被两层包裹：result.content[0].text 里是一个 JSON 字符串
- 鉴权是 x-api-key header，不是 Authorization: Bearer
- session id 在响应头 mcp-session-id；session TTL 上游未说明 →
  4xx 时清 session、重握手、只重试一次（不做无限重试/指数退避）
- 2026-09-04 真机排障发现：上游（followx-mcp 2.0.0）已经改成无状态模式，
  initialize 响应可能不再下发 mcp-session-id。这不是错误——握手仍要走，
  只是拿不到 id 时后续请求不带这个头继续工作。判定"是否已握手"必须用独立
  的 _handshaked 标志位，不能看 _session_id 是否为空，否则无状态模式下
  每次 fetch_news 都会重新握手一遍。旧实现把"没有 session id"当硬错误
  直接抛异常，三条 route 全挂——新闻库因此静默停更了一周，直到真机反馈
  才发现（refresh_once 单 route 失败只记 warning，全灭也看不出来）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION: Final = "2025-06-18"
_ACCEPT: Final = "application/json, text/event-stream"
_DEFAULT_URL: Final = "https://mcp.followin.io/v2/mcp"


class FollowinUpstreamError(RuntimeError):
    """Followin 上游调用失败。带足信息供采集器决定跳过还是告警。"""

    def __init__(
        self, message: str, *, status_code: int | None = None, is_timeout: bool = False
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.is_timeout = is_timeout


def parse_sse_json(body: str) -> dict[str, Any]:
    """从 SSE 文本里取出第一条 data 行的 JSON。"""
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        raw = line[len("data:"):].strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FollowinUpstreamError(f"Followin 响应 data 行不是合法 JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise FollowinUpstreamError("Followin 响应的 data 不是 JSON 对象")
        return payload
    raise FollowinUpstreamError("Followin 响应中没有 data 行")


def _unwrap_tool_result(envelope: dict[str, Any]) -> dict[str, Any]:
    """剥掉 MCP 的两层包裹，拿到业务 JSON。"""
    if "error" in envelope:
        raise FollowinUpstreamError(f"Followin 工具返回错误: {envelope['error']}")
    contents = (envelope.get("result") or {}).get("content") or []
    for part in contents:
        if isinstance(part, dict) and part.get("type") == "text":
            try:
                parsed = json.loads(part.get("text") or "{}")
            except json.JSONDecodeError as exc:
                raise FollowinUpstreamError(
                    f"Followin 工具响应的 text 不是合法 JSON: {exc}"
                ) from exc
            if isinstance(parsed, dict):
                return parsed
    raise FollowinUpstreamError("Followin 工具响应里没有可解析的 text 内容")


class FollowinClient:
    """真实模式。http_client 由容器注入，本类不自建也不关闭。"""

    def __init__(
        self, api_key: str, http_client: httpx.AsyncClient, mcp_url: str = _DEFAULT_URL
    ) -> None:
        self.api_key = api_key
        self.http_client = http_client
        self.mcp_url = mcp_url
        self._session_id: str | None = None
        # 是否已握手过——不能用 _session_id is None 判断：无状态模式下上游
        # 不下发 session id，_session_id 会一直是 None，靠它判断会导致每次
        # fetch_news 都重新握手一遍。
        self._handshaked = False

    def _headers(self) -> dict[str, str]:
        headers = {
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": _ACCEPT,
        }
        if self._session_id:
            headers["mcp-session-id"] = self._session_id
        return headers

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            return await self.http_client.post(
                self.mcp_url, headers=self._headers(), json=payload
            )
        except httpx.TimeoutException as exc:
            raise FollowinUpstreamError(f"Followin 调用超时: {exc}", is_timeout=True) from exc
        except httpx.HTTPError as exc:
            raise FollowinUpstreamError(f"Followin 调用失败: {exc}") from exc

    async def _handshake(self) -> None:
        """建立新 session。清掉旧的，避免带着失效 id 去握手。

        上游可能不下发 mcp-session-id（无状态模式）——这不是错误，后续请求
        照常工作，只是不带这个头。只有 initialize 本身失败（4xx/5xx）才算
        握手失败。
        """
        self._session_id = None
        self._handshaked = False
        resp = await self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agentos-agent-runtime", "version": "0.1"},
            },
        })
        if resp.status_code >= 400:
            raise FollowinUpstreamError(
                f"Followin 握手失败: HTTP {resp.status_code}", status_code=resp.status_code
            )
        self._session_id = resp.headers.get("mcp-session-id") or None
        self._handshaked = True
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> httpx.Response:
        return await self._post({
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })

    async def fetch_news(self, **arguments: Any) -> dict[str, Any]:
        """调用 news 工具。session 失效则重握手并只重试一次。"""
        if not self._handshaked:
            await self._handshake()

        resp = await self._call_tool("news", arguments)
        if 400 <= resp.status_code < 500:
            # session 失效的典型表现。只重试一次：上游真挂了就别继续锤它。
            logger.info("Followin session 可能已失效（HTTP %d），重握手后重试一次", resp.status_code)
            await self._handshake()
            resp = await self._call_tool("news", arguments)

        if resp.status_code >= 400:
            raise FollowinUpstreamError(
                f"Followin news 调用失败: HTTP {resp.status_code}", status_code=resp.status_code
            )
        return _unwrap_tool_result(parse_sse_json(resp.text))


# 以下两份内联样本与 tests/fixtures/followin_media.json / followin_trending.json
# 同源（均取自真机探测样本），但刻意在此内联而不去读 tests/ 目录：
#
#   pyproject.toml 里 [tool.hatch.build.targets.wheel] packages = ["app"]，
#   tests/ 不随 wheel 分发。FakeFollowinClient 恰恰是「未配置
#   AGENT_FOLLOWIN_API_KEY 时的降级路径」，可能在部署环境被触发 ——
#   若走文件读取，届时会 FileNotFoundError。生产代码不得依赖 tests 目录，
#   所以这里改为模块级常量，样本内容与 tests/fixtures 保持一致（精简版）。
_FAKE_SEARCH_RESULT: Final[dict[str, Any]] = {
    "results": {
        "articles": [
            {
                "category": "market",
                "content": (
                    "BTC 隔夜大涨，突破 6.9 万美元关口。"
                    "<p><b>看涨观点</b>：机构资金持续流入。</p>"
                ),
                "provenance": "feeds",
                "published_ts": 1787190519312,
                "source_lang": "zh",
                "source_name": "cointelegraph",
                "source_quality": "media",
                "source_url": "https://cointelegraph.com/markets/bitcoin-price-reaches-725k",
                "title": "Bitcoin price reaches 72.5k as inflows accelerate",
            },
        ],
        "social": [
            {
                # title 故意留空——后续任务要靠这条测试「无标题行被丢弃」。
                "category": "",
                "content": "Maya protocol exploit drains about 11M via six-flaw chain.",
                "kol_info": {"name": "muur_posts@telegram"},
                "provenance": "tg_kol_feeds",
                "published_ts": 1787242029793,
                "source_lang": "en",
                "source_name": "muur_posts@telegram",
                "source_quality": "social",
                "source_url": "https://twitter.com/CryptoKaleo/status/2090469542128742910",
                "title": "",
            },
        ],
    },
    "meta": {
        "mode": "search",
        "quota": {"consumed": 0, "used": 0, "limit": 100, "remaining": 100},
        "request_id": "07b924562f6c2f865d631384759e05ac",
        "status": "ok",
        "total": 2,
        "verbosity": "standard",
    },
}

_FAKE_TRENDING_RESULT: Final[dict[str, Any]] = {
    "results": {
        "articles": [
            {
                "asset_type": "crypto",
                "asset_types": ["BTC"],
                "category": "majorcoin",
                "content": "发生了什么？\nBTC 突破 71500，创 6 月以来新高。",
                "provenance": "followin_trending",
                "published_ts": 1787190519312,
                "source_quality": "media",
                "title": "BTC continued its upward trend, breaking through 71500",
            },
        ],
    },
    "meta": {
        "mode": "search",
        "quota": {"consumed": 0, "used": 0, "limit": 100, "remaining": 100},
        "status": "ok",
        "total": 1,
        "verbosity": "standard",
    },
}


class FakeFollowinClient:
    """Fake 模式（无 API Key）。返回内联样本，不读任何文件。

    必须复刻 route 差异：trending route 无 source_url / source_lang / source_name，
    search route 有 —— 手编 mock 编不出这个，而它正是 normalizer 最易错的地方。
    """

    async def fetch_news(self, **arguments: Any) -> dict[str, Any]:
        has_scope = bool(arguments.get("sources") or arguments.get("query"))
        return _FAKE_SEARCH_RESULT if has_scope else _FAKE_TRENDING_RESULT
