"""联网搜索工具单测 —— 离线 Mock，不真实调用 Tavily API。"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.tools.executor import ToolExecutor
from app.tools.search_tools import SEARCH_TOOLS, tavily_search

# ---- tavily_search 单元测试 ----


async def test_tavily_search_returns_trimmed_results():
    mock_response = {
        "answer": "BNB is a cryptocurrency.",
        "results": [
            {
                "title": "BNB Overview",
                "url": "https://example.com/bnb",
                "content": "BNB is the native token of BNB Chain." + "x" * 600,
            },
            {
                "title": "BNB Price",
                "url": "https://example.com/bnb-price",
                "content": "BNB is trading at $600.",
            },
        ],
    }

    with respx.mock:
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json=mock_response)
        )
        result = await tavily_search("BNB", api_key="tvly-test-key")

    assert result["source"] == "TAVILY_SEARCH"
    assert result["query"] == "BNB"
    assert result["answer"] == "BNB is a cryptocurrency."
    assert len(result["results"]) == 2
    assert result["results"][0]["title"] == "BNB Overview"
    # Content should be truncated to 500 chars + "…"
    assert len(result["results"][0]["content"]) == 501
    assert result["results"][0]["content"].endswith("…")
    # Short content should not be truncated
    assert result["results"][1]["content"] == "BNB is trading at $600."


async def test_tavily_search_rejects_empty_query():
    with pytest.raises(ValueError, match="搜索关键词不能为空"):
        await tavily_search("  ", api_key="tvly-test-key")


async def test_tavily_search_rejects_missing_api_key():
    with pytest.raises(ValueError, match="TAVILY_API_KEY 未配置"):
        await tavily_search("BNB", api_key="")


async def test_tavily_search_handles_empty_results():
    with respx.mock:
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json={"answer": "", "results": []})
        )
        result = await tavily_search("nonexistent", api_key="tvly-test-key")

    assert result["results"] == []
    assert result["answer"] == ""


async def test_tavily_search_raises_on_http_error():
    with respx.mock:
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(401, json={"error": "unauthorized"})
        )
        result = await tavily_search("BNB", api_key="tvly-bad-key")
        assert "error" in result
        assert result["error_code"] == "SEARCH_UNAUTHORIZED"
        assert result["retryable"] is False


# ---- SEARCH_TOOLS 注册定义 ----


def test_search_tools_defines_web_search():
    assert len(SEARCH_TOOLS) == 1
    tool = SEARCH_TOOLS[0]
    assert tool.name == "web_search"
    assert tool.external is True
    assert tool.parameters["required"] == ["query"]


# ---- ToolExecutor dispatch 集成测试 ----


def _stub_executor(*, tavily_api_key: str = "tvly-test") -> tuple:
    """构建带 web_search 的最小 executor（无 agentos_client）。"""
    from unittest.mock import AsyncMock

    from app.tools.builtin import build_default_registry
    from app.tools.core_client import CoreClient

    registry = build_default_registry()
    core = AsyncMock(spec=CoreClient)
    executor = ToolExecutor(
        registry,
        core,
        tavily_api_key=tavily_api_key,
    )
    return executor, registry


async def test_executor_dispatches_web_search():
    executor, registry = _stub_executor()
    mock_response = {
        "answer": "BTC hit $100k.",
        "results": [
            {
                "title": "Bitcoin Breaks 100k",
                "url": "https://example.com/btc",
                "content": "Bitcoin has surged to $100,000.",
            }
        ],
    }

    with respx.mock:
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json=mock_response)
        )
        result = await executor.execute(
            "web_search",
            "u1",
            {"query": "why did BTC go up"},
            list(registry.names()),
        )

    assert result["source"] == "TAVILY_SEARCH"
    assert result["query"] == "why did BTC go up"
    assert len(result["results"]) == 1


async def test_executor_web_search_rejects_empty_query():
    executor, registry = _stub_executor()
    with pytest.raises(ValueError, match="搜索关键词"):
        await executor.execute(
            "web_search",
            "u1",
            {"query": ""},
            list(registry.names()),
        )


async def test_executor_web_search_rejects_missing_api_key():
    executor, registry = _stub_executor(tavily_api_key="")
    with pytest.raises(ValueError, match="联网搜索未配置"):
        await executor.execute(
            "web_search",
            "u1",
            {"query": "BNB news"},
            list(registry.names()),
        )


async def test_executor_web_search_works_without_agentos_client():
    """web_search is external=True but should NOT require agentos_client."""
    executor, registry = _stub_executor()
    # Verify executor._agentos is None
    assert executor._agentos is None

    with respx.mock:
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json={"answer": "ok", "results": []})
        )
        result = await executor.execute(
            "web_search",
            "u1",
            {"query": "test"},
            list(registry.names()),
        )

    assert result["source"] == "TAVILY_SEARCH"


async def test_executor_web_search_normalizes_query_before_calling_tavily():
    executor, registry = _stub_executor()

    with respx.mock:
        route = respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json={"answer": "ok", "results": []})
        )
        await executor.execute(
            "web_search",
            "u1",
            {"query": "帮我查一下最近黄金价格"},
            list(registry.names()),
        )

    sent_payload = route.calls.last.request.content
    import json as _json

    assert _json.loads(sent_payload)["query"] == "最近黄金价格"


async def test_executor_web_search_prefers_local_news_over_tavily():
    from datetime import UTC, datetime, timedelta
    from unittest.mock import AsyncMock

    from app.news.models import NewsItem
    from app.news.store import InMemoryNewsStore
    from app.tools.builtin import build_default_registry
    from app.tools.core_client import CoreClient

    news_store = InMemoryNewsStore()
    await news_store.upsert_many(
        [
            NewsItem(
                news_id="n1",
                dedup_key="黄金价格创新高",
                source="media",
                title="黄金价格创新高",
                content="正文",
                source_url="https://example.com/n1",
                published_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        ]
    )
    registry = build_default_registry()
    core = AsyncMock(spec=CoreClient)
    executor = ToolExecutor(
        registry,
        core,
        tavily_api_key="tvly-test",
        news_store=news_store,
    )

    with respx.mock:
        route = respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json={"answer": "ok", "results": []})
        )
        result = await executor.execute(
            "web_search",
            "u1",
            {"query": "黄金最新价格"},
            list(registry.names()),
        )

    assert route.call_count == 0
    assert result["source"] == "LOCAL_NEWS_STORE"
    assert result["results"][0]["title"] == "黄金价格创新高"


async def test_executor_web_search_falls_back_to_tavily_when_local_store_errors():
    """本地库只是 Tavily 的前置降本方案；DB 挂了不该拖垮整个 web_search。"""
    from unittest.mock import AsyncMock

    from app.tools.builtin import build_default_registry
    from app.tools.core_client import CoreClient

    class _BrokenNewsStore:
        async def query(self, q):
            raise RuntimeError("postgres connection timeout")

    registry = build_default_registry()
    core = AsyncMock(spec=CoreClient)
    executor = ToolExecutor(
        registry,
        core,
        tavily_api_key="tvly-test",
        news_store=_BrokenNewsStore(),
    )

    with respx.mock:
        route = respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json={"answer": "ok", "results": []})
        )
        result = await executor.execute(
            "web_search",
            "u1",
            {"query": "黄金最新价格"},
            list(registry.names()),
        )

    assert route.call_count == 1
    assert result["source"] == "TAVILY_SEARCH"


async def test_executor_web_search_falls_back_to_tavily_when_local_news_misses():
    from unittest.mock import AsyncMock

    from app.news.store import InMemoryNewsStore
    from app.tools.builtin import build_default_registry
    from app.tools.core_client import CoreClient

    news_store = InMemoryNewsStore()  # empty local store, must fall through
    registry = build_default_registry()
    core = AsyncMock(spec=CoreClient)
    executor = ToolExecutor(
        registry,
        core,
        tavily_api_key="tvly-test",
        news_store=news_store,
    )

    with respx.mock:
        route = respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json={"answer": "ok", "results": []})
        )
        result = await executor.execute(
            "web_search",
            "u1",
            {"query": "黄金最新价格"},
            list(registry.names()),
        )

    assert route.call_count == 1
    assert result["source"] == "TAVILY_SEARCH"


# ---- 搜索失败的结构化分级。旧实现把 429 配额耗尽、401 密钥失效、5xx 上游故障
# 和网络超时全压成一句 "Search API failed with status {code}"，上层无法判断
# 该重试、该降级还是该报错。 ----


@pytest.mark.parametrize(
    ("status_code", "expected_code", "expected_retryable"),
    [
        (429, "SEARCH_RATE_LIMITED", True),
        (500, "SEARCH_UPSTREAM_ERROR", True),
        (503, "SEARCH_UPSTREAM_ERROR", True),
        (401, "SEARCH_UNAUTHORIZED", False),
        (403, "SEARCH_UNAUTHORIZED", False),
        (400, "SEARCH_INVALID_REQUEST", False),
    ],
)
async def test_tavily_http_errors_are_classified(status_code, expected_code, expected_retryable):
    with respx.mock:
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(status_code, json={"error": "x"})
        )
        result = await tavily_search("BTC", api_key="tvly-test-key")

    assert result["error_code"] == expected_code
    assert result["retryable"] is expected_retryable
    assert result["error"], "人类可读的 error 字段要保留，回灌给模型时用得上"


async def test_tavily_timeout_is_classified_as_retryable():
    with respx.mock:
        respx.post("https://api.tavily.com/search").mock(
            side_effect=httpx.TimeoutException("timed out")
        )
        result = await tavily_search("BTC", api_key="tvly-test-key")

    assert result["error_code"] == "SEARCH_TIMEOUT"
    assert result["retryable"] is True


async def test_tavily_network_error_is_classified_as_retryable():
    with respx.mock:
        respx.post("https://api.tavily.com/search").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        result = await tavily_search("BTC", api_key="tvly-test-key")

    assert result["error_code"] == "SEARCH_NETWORK_ERROR"
    assert result["retryable"] is True


async def test_tavily_error_never_leaks_api_key():
    """error 字段会被回灌给模型，密钥绝不能出现在里面。"""
    with respx.mock:
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(401, json={"error": "invalid api_key tvly-secret-123"})
        )
        result = await tavily_search("BTC", api_key="tvly-secret-123")

    assert "tvly-secret-123" not in result["error"]
