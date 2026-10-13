"""AgentOS 真链路工具的白名单登记 + executor 隔离（防误走 core）。"""

from __future__ import annotations

import httpx
import pytest

from app.policies.engine import PolicyEngine
from app.policies.mandate import default_mandate
from app.tools.agentos.client import AgentOsStagingClient
from app.tools.builtin import (
    AGENTOS_READONLY_TOOLS,
    AGENTOS_WRITE_TOOLS,
    build_default_registry,
)
from app.tools.core_client import FakeCoreClient
from app.tools.executor import ToolExecutor, UnknownToolError


def test_agentos_tools_registered_as_external():
    reg = build_default_registry()
    for name in AGENTOS_READONLY_TOOLS + AGENTOS_WRITE_TOOLS:
        tool = reg.get(name)
        assert tool is not None, f"{name} 未登记"
        assert tool.external is True, f"{name} 应标记 external"
    # 读/写标记正确
    assert reg.get("agentos_get_balances").write is False
    assert reg.get("agentos_create_trade_draft").write is True


def test_agentos_tools_in_scope_allowed():
    reg = build_default_registry()
    scope = list(reg.names())
    assert reg.is_allowed("agentos_create_trade_draft", scope) is True
    # 不在 scope 则拒绝
    assert reg.is_allowed("agentos_create_trade_draft", ["get_positions"]) is False
    # 未登记的工具也拒绝
    assert reg.is_allowed("no_such_tool", scope) is False


def test_market_price_tool_has_strict_real_time_schema() -> None:
    tool = build_default_registry().get("get_market_price")
    assert tool is not None
    assert tool.parameters == {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "Core Catalog 返回的 PERP venue_symbol",
            }
        },
        "required": ["symbol"],
        "additionalProperties": False,
    }
    assert "必须调用" in tool.description
    assert "PERP" in tool.description
    assert "不是现货" in tool.description

    batch = build_default_registry().get("market_tickers")
    assert batch is not None
    assert batch.parameters["properties"]["symbols"]["maxItems"] == 50
    assert batch.parameters["additionalProperties"] is False


@pytest.mark.parametrize("name", list(AGENTOS_READONLY_TOOLS + AGENTOS_WRITE_TOOLS))
async def test_executor_rejects_external_tools(name):
    """external 工具必须由 AgentOsStagingClient 执行，executor 遇到就抛，绝不误走 core。"""
    reg = build_default_registry()
    executor = ToolExecutor(reg, FakeCoreClient())
    scope = list(reg.names())
    with pytest.raises(UnknownToolError, match="external"):
        await executor.execute(name, "u1", {}, scope)


async def test_executor_still_runs_core_readonly_tool():
    """对照组：非 external 的只读工具仍正常走 core，证明隔离只挡 external。"""
    reg = build_default_registry()
    executor = ToolExecutor(reg, FakeCoreClient())
    out = await executor.execute("get_positions", "u1", {}, list(reg.names()))
    assert isinstance(out, dict)


async def test_search_market_catalog_cleans_the_llm_ticker_alias() -> None:
    """线上实测：LLM 直接用 ETHPERP 调这个工具，原样透传给 Core 查不到标的。"""

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200, json={"data": [], "meta": {"stale": False}, "request_id": "req_catalog"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = ToolExecutor(
            build_default_registry(),
            FakeCoreClient(),
            PolicyEngine(default_mandate()),
            agentos_client=AgentOsStagingClient("https://core.example", "agent-token", client),
            expected_agent_id="agent-test-001",
        )
        await executor.execute(
            "search_market_catalog",
            "u1",
            {"query": "ETHPERP", "market_type": "PERP"},
            ["search_market_catalog"],
        )

    assert captured[0].url.params["query"] == "ETH"


async def test_search_market_catalog_allows_an_empty_query() -> None:
    """空 query 表示「列全部」，不能被清洗逻辑拒掉。"""

    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200, json={"data": [], "meta": {"stale": False}, "request_id": "req_catalog"}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        executor = ToolExecutor(
            build_default_registry(),
            FakeCoreClient(),
            PolicyEngine(default_mandate()),
            agentos_client=AgentOsStagingClient("https://core.example", "agent-token", client),
            expected_agent_id="agent-test-001",
        )
        await executor.execute(
            "search_market_catalog", "u1", {}, ["search_market_catalog"]
        )

    assert captured[0].url.params.get("query", "") == ""
