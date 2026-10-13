"""组合与持仓类工具 —— get_positions / get_orders / get_risk_summary / get_transaction_history。

定义持仓、订单、风险及流水查询 Function Calling 规格，对接 core 后台接口。
"""

from __future__ import annotations

from app.tools.registry import Tool

PORTFOLIO_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="get_positions",
        display_title="查询持仓",
        display_params=("symbol",),
        resource="positions",
        description=(
            "查询 Hyperliquid 永续合约持仓（开仓均价、数量、未实现盈亏）。"
            "不是 BSC 现货；BNB/USDT/Meme 等现货请用 get_balances"
        ),
        parameters={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "标的代码筛选（可选，如 ETH），为空则返回所有持仓",
                }
            },
        },
    ),
    Tool(
        name="get_orders",
        display_title="查询委托订单",
        display_params=("symbol", "status"),
        resource="orders",
        description="查询当前账户的未结委托挂单及历史订单",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "标的代码筛选（可选）"},
                "status": {
                    "type": "string",
                    "description": "订单状态筛选（可选，如 open, filled, cancelled）",
                },
            },
        },
    ),
    Tool(
        name="get_risk_summary",
        display_title="查询风险状况",
        display_params=(),
        resource="risk_summary",
        description="查询账户风险健康度评估，包含杠杆上限、已用风控额度与最大额度",
        parameters={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_transaction_history",
        display_title="查询交易流水",
        display_params=(),
        resource="transaction_history",
        description="查询账户历史资金变动明细与交易流水记录",
        parameters={
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "返回的记录最大条数（可选，默认 20）",
                }
            },
        },
    ),
    Tool(
        name="agentos_get_positions",
        display_title="查询持仓",
        display_params=("symbol",),
        external=True,
        description="测试网真链路：查询当前账户持仓明细",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "标的代码筛选（可选）"}
            },
        },
    ),
    Tool(
        name="agentos_get_orders",
        display_title="查询委托订单",
        display_params=("symbol",),
        external=True,
        description="测试网真链路：查询当前账户委托订单",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "标的代码筛选（可选）"}
            },
        },
    ),
)

