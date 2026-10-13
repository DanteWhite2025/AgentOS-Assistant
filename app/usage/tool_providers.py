"""工具名 → 上游 provider 的归属映射。

未登记的工具归 "unknown"：新增工具漏配时会在报表里露出来，比默默归到某个
provider 更安全（那会把成本算到错的供应商头上）。
tests/usage/test_usage_tool_providers.py 有一条测试遍历注册表，漏配会直接变红。
"""

from __future__ import annotations

#: 走 Core 的工具。Core 定性为内部服务，单价暂记 0（spec D2），
#: 但流水照采，将来配上单价即可回算。
_CORE_TOOLS = frozenset(
    {
        "get_market_price",
        "market_tickers",
        "search_market_catalog",
        "get_balances",
        "get_positions",
        "get_orders",
        "get_account_summary",
        "get_risk_summary",
        "get_trading_readiness",
        "get_transaction_history",
        "create_trade_draft",
        "create_perp_trade_draft",
        "create_spot_swap_draft",
        "agentos_get_balances",
        "agentos_get_positions",
        "agentos_get_orders",
        "agentos_create_trade_draft",
    }
)

#: 不产生外部付费调用的工具（本地 PG、本地证据组装）。
_INTERNAL_TOOLS = frozenset({"update_user_preference", "ask_investment_expert"})

#: 按次计费的第三方。
_THIRD_PARTY_TOOLS = {"web_search": "tavily"}

UNKNOWN_PROVIDER = "unknown"


def provider_for_tool(tool_name: str) -> str:
    if tool_name in _THIRD_PARTY_TOOLS:
        return _THIRD_PARTY_TOOLS[tool_name]
    if tool_name in _CORE_TOOLS:
        return "core"
    if tool_name in _INTERNAL_TOOLS:
        return "internal"
    return UNKNOWN_PROVIDER
