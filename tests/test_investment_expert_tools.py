"""投研证据抓取工具单测（T3）。

关键约定：
被 LLM 直接调用的工具函数不能对上游错误抛异常，必须捕获后返回
{"error": ...} dict，否则会让整个 turn/websocket 崩溃。
"""

from __future__ import annotations

from app.services.investment_expert_client import FakeInvestmentExpertClient
from app.tools.executor import ToolExecutor
from app.tools.investment_expert_tools import (
    INVESTMENT_EXPERT_TOOLS,
    ask_investment_expert,
)

# ---- ask_investment_expert 单元测试 ----


class _StubEvidenceClient:
    def __init__(self, *, evidence: dict | None = None, error: Exception | None = None) -> None:
        self._evidence = evidence or {"source": "MARKET_EVIDENCE"}
        self._error = error
        self.received: tuple[str, tuple, str | None] | None = None

    async def collect_evidence(self, question, *, symbols=(), chat_id=None):
        self.received = (question, tuple(symbols), chat_id)
        if self._error is not None:
            raise self._error
        return self._evidence


async def test_ask_investment_expert_returns_evidence_bundle() -> None:
    client = _StubEvidenceClient(evidence={"source": "MARKET_EVIDENCE", "market": {}})
    result = await ask_investment_expert("分析一下英伟达财报", client=client)

    assert result["source"] == "MARKET_EVIDENCE"


async def test_ask_investment_expert_rejects_empty_message() -> None:
    result = await ask_investment_expert("  ", client=FakeInvestmentExpertClient())
    assert "error" in result


async def test_ask_investment_expert_returns_error_dict_when_client_fails() -> None:
    client = _StubEvidenceClient(error=RuntimeError("证据抓取上游不可用"))
    result = await ask_investment_expert("分析一下英伟达财报", client=client)

    assert "error" in result


# ---- INVESTMENT_EXPERT_TOOLS 注册定义 ----


def test_investment_expert_tools_defines_ask_investment_expert() -> None:
    assert len(INVESTMENT_EXPERT_TOOLS) == 1
    tool = INVESTMENT_EXPERT_TOOLS[0]
    assert tool.name == "ask_investment_expert"
    assert tool.external is True
    assert tool.parameters["required"] == ["message"]
    assert "mode" not in tool.parameters["properties"]
    assert "symbols" in tool.parameters["properties"]
    assert tool.parameters["properties"]["symbols"]["maxItems"] == 8


# ---- ToolExecutor dispatch 集成测试 ----


def _stub_executor(*, investment_expert_client) -> tuple:
    from unittest.mock import AsyncMock

    from app.tools.builtin import build_default_registry
    from app.tools.core_client import CoreClient

    registry = build_default_registry()
    core = AsyncMock(spec=CoreClient)
    executor = ToolExecutor(registry, core, investment_expert_client=investment_expert_client)
    return executor, registry


async def test_executor_dispatches_ask_investment_expert() -> None:
    executor, registry = _stub_executor(investment_expert_client=FakeInvestmentExpertClient())

    result = await executor.execute(
        "ask_investment_expert",
        "u1",
        {"message": "分析一下英伟达财报", "symbols": ["BTC"]},
        list(registry.names()),
    )

    assert result["source"] == "MARKET_EVIDENCE"


async def test_executor_ask_investment_expert_works_without_agentos_client() -> None:
    executor, registry = _stub_executor(investment_expert_client=FakeInvestmentExpertClient())
    assert executor._agentos is None

    result = await executor.execute(
        "ask_investment_expert",
        "u1",
        {"message": "BTC 走势如何"},
        list(registry.names()),
    )

    assert result["source"] == "MARKET_EVIDENCE"


async def test_executor_ask_investment_expert_ignores_legacy_mode_argument() -> None:
    """mode（fast/expert）随证据化改造下线：模型出于历史习惯仍可能带上它，
    静默忽略而不是返回 error——为一个已废弃的参数拒绝整轮调用得不偿失。"""
    executor, registry = _stub_executor(investment_expert_client=FakeInvestmentExpertClient())

    result = await executor.execute(
        "ask_investment_expert",
        "u1",
        {"message": "BTC 走势如何", "mode": "超级深度"},
        list(registry.names()),
    )

    assert result["source"] == "MARKET_EVIDENCE"
    assert "error" not in result


async def test_executor_rejects_oversized_symbols_list() -> None:
    executor, registry = _stub_executor(investment_expert_client=FakeInvestmentExpertClient())

    result = await executor.execute(
        "ask_investment_expert",
        "u1",
        {"message": "多资产复盘", "symbols": [f"SYM{i}" for i in range(9)]},
        list(registry.names()),
    )

    assert "error" in result


async def test_executor_rejects_non_string_symbols() -> None:
    executor, registry = _stub_executor(investment_expert_client=FakeInvestmentExpertClient())

    result = await executor.execute(
        "ask_investment_expert",
        "u1",
        {"message": "BTC 走势如何", "symbols": ["BTC", 123]},
        list(registry.names()),
    )

    assert "error" in result


async def test_executor_ask_investment_expert_reports_when_unconfigured() -> None:
    executor, registry = _stub_executor(investment_expert_client=None)

    result = await executor.execute(
        "ask_investment_expert",
        "u1",
        {"message": "BTC 走势如何"},
        list(registry.names()),
    )

    assert "error" in result


async def test_executor_passes_symbols_through_to_client() -> None:
    client = _StubEvidenceClient()
    executor, registry = _stub_executor(investment_expert_client=client)

    await executor.execute(
        "ask_investment_expert",
        "u1",
        {"message": "BTC 和 ETH 怎么样", "symbols": ["BTC", "ETH"]},
        list(registry.names()),
    )

    assert client.received == ("BTC 和 ETH 怎么样", ("BTC", "ETH"), None)
