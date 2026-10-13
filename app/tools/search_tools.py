"""联网搜索工具 —— Tavily Search API（httpx 直连，不引额外 SDK）。

用户问行情以外的「为什么涨了」「项目背景」「最新新闻」等问题时，
Agent 调用此工具获取互联网实时信息，再基于搜索结果回答。

安全红线：
1. 搜索为纯只读操作，不涉及交易账户或资金。
2. API Key 统一由环境变量 AGENT_TAVILY_API_KEY 注入，不硬编码。
3. 结果截取摘要返回，避免超大 payload 消耗 LLM token 预算。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.tools.registry import Tool

logger = logging.getLogger(__name__)

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"

# ---- 最大结果条数 & 单条摘要截取长度 ----
_MAX_RESULTS = 5
_MAX_CONTENT_LENGTH = 500


def _http_error_payload(status_code: int, retry_after: str | None = None) -> dict[str, Any]:
    """把 Tavily 的 HTTP 失败分类成「能否重试」。

    上游响应体绝不进 error 文案——它会原样回灌给模型，而 Tavily 的 401 响应
    里就带着我们发过去的 api_key。只用状态码和 Retry-After 头说话。
    """

    if status_code == 429:
        return {
            "error": "搜索服务达到调用频率上限",
            "error_code": "SEARCH_RATE_LIMITED",
            "retryable": True,
            # 限流时上游最清楚该等多久，透出去交给 TavilySearchClient 的退避用。
            "retry_after": retry_after,
        }
    if status_code >= 500:
        return {
            "error": "搜索服务暂时不可用",
            "error_code": "SEARCH_UPSTREAM_ERROR",
            "retryable": True,
        }
    if status_code in (401, 403):
        # 密钥失效是配置问题，重试多少次都一样，留给运维从日志发现。
        return {
            "error": "搜索服务鉴权失败",
            "error_code": "SEARCH_UNAUTHORIZED",
            "retryable": False,
        }
    return {
        "error": f"搜索请求被拒绝（HTTP {status_code}）",
        "error_code": "SEARCH_INVALID_REQUEST",
        "retryable": False,
    }


async def tavily_search(
    query: str,
    *,
    api_key: str,
    max_results: int = _MAX_RESULTS,
    search_depth: str = "basic",
    topic: str = "general",
    time_range: str | None = None,
    timeout: float = 5.0,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """调用 Tavily Search API 并返回裁剪后的结果。

    不依赖 tavily-python SDK，直接 POST JSON，与项目 httpx 风格一致。
    传入 `client` 时复用调用方管理的长连接（容器统一关闭）；不传时退回
    每次调用新建一个临时客户端，行为与之前一致，供未接入连接池的调用方使用。
    """
    query = query.strip()
    if not query:
        raise ValueError("搜索关键词不能为空")
    if not api_key:
        raise ValueError("TAVILY_API_KEY 未配置")

    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": min(max_results, 10),
        "search_depth": search_depth,
        "topic": topic,
        "include_answer": True,
    }
    if time_range:
        payload["time_range"] = time_range

    try:
        if client is not None:
            # 显式传 timeout：调用方（TavilySearchClient）传的 timeout_seconds
            # 必须真正生效，不能被 client 构造时的默认 timeout 悄悄覆盖。
            response = await client.post(_TAVILY_SEARCH_URL, json=payload, timeout=timeout)
        else:
            async with httpx.AsyncClient(timeout=timeout) as ephemeral_client:
                response = await ephemeral_client.post(_TAVILY_SEARCH_URL, json=payload)
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"Tavily HTTP error {e.response.status_code}: {e.response.text}")
        return _http_error_payload(
            e.response.status_code, e.response.headers.get("Retry-After")
        )
    except httpx.TimeoutException as e:
        # 单列超时：5s 是全链路最紧的预算，热点时段最常见的失败就是它，
        # 上层要凭 error_code 决定退避重试而不是直接降级。
        logger.error(f"Tavily timeout: {e}")
        return {
            "error": "搜索请求超时",
            "error_code": "SEARCH_TIMEOUT",
            "retryable": True,
        }
    except httpx.RequestError as e:
        logger.error(f"Tavily request error: {e}")
        return {
            "error": "搜索服务网络异常",
            "error_code": "SEARCH_NETWORK_ERROR",
            "retryable": True,
        }

    results = []
    for item in (data.get("results") or [])[:max_results]:
        content = str(item.get("content") or "")
        if len(content) > _MAX_CONTENT_LENGTH:
            content = content[:_MAX_CONTENT_LENGTH] + "…"
        results.append(
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "content": content,
            }
        )

    return {
        "source": "TAVILY_SEARCH",
        "query": query,
        "answer": str(data.get("answer") or ""),
        "results": results,
    }


# ---- Tool 注册定义 ----

SEARCH_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="web_search",
        display_title="联网搜索",
        display_params=("query", "topic", "time_range"),
        external=True,
        description=(
            "联网搜索实时信息。当用户询问项目背景、最新新闻、价格原因分析、"
            "行业动态等无法仅凭行情数据回答的问题时必须调用。"
            "不得用于查询交易账户、余额或订单信息。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词。如果是特定地区（如中国）的八卦或新闻，必须使用原文（如中文）而不是强行翻译成英文。",
                },
                "topic": {
                    "type": "string",
                    "enum": ["general", "news"],
                    "description": "搜索主题。若查找最新新闻、近期事件、近期爆料，建议选 news；普通搜索选 general。",
                },
                "time_range": {
                    "type": "string",
                    "enum": ["day", "week", "month", "year"],
                    "description": "时间范围。查找最新「瓜」或突发新闻可设为 week 或 month。",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
)
