"""默认工具装配。只读工具映射到 core 资源；create_trade_draft 为受控写工具。

AGENTOS_* 一组是真测试网 Agent API 能力（第二步接入）：登记进白名单用于
scope 校验/能力声明，但标记 external=True —— 由 AgentOsStagingClient / TradeDraftFlow
执行，**不经 core executor**（token 派生身份，与 core 的 user_id 语义不同）。
"""

from __future__ import annotations

from app.tools.account_tools import ACCOUNT_TOOLS
from app.tools.investment_expert_tools import INVESTMENT_EXPERT_TOOLS
from app.tools.market_tools import MARKET_TOOLS
from app.tools.memory_tools import MEMORY_TOOLS
from app.tools.portfolio_tools import PORTFOLIO_TOOLS
from app.tools.registry import ToolRegistry
from app.tools.search_tools import SEARCH_TOOLS
from app.tools.trade_tools import TRADE_TOOLS

READONLY_TOOLS: dict[str, str] = {
    "get_account_summary": "account_summary",
    "get_balances": "balances",
    "get_positions": "positions",
    "get_orders": "orders",
    "get_risk_summary": "risk_summary",
    "get_transaction_history": "transaction_history",
}

AGENTOS_READONLY_TOOLS: tuple[str, ...] = (
    "agentos_get_balances",
    "agentos_get_positions",
    "agentos_get_orders",
)
AGENTOS_WRITE_TOOLS: tuple[str, ...] = (
    "agentos_create_trade_draft",
    "create_perp_trade_draft",
    "create_spot_swap_draft",
)


def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool_group in (
        ACCOUNT_TOOLS, MARKET_TOOLS, PORTFOLIO_TOOLS, TRADE_TOOLS, MEMORY_TOOLS, SEARCH_TOOLS,
        INVESTMENT_EXPERT_TOOLS,
    ):
        for tool in tool_group:
            registry = registry.with_tool(tool)
    return registry
