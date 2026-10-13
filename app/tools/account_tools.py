"""账户类工具 —— get_account_summary / get_balances。

定义账户概览与代币余额查询 Function Calling 规格，连 core 后台接口。
"""

from __future__ import annotations

from app.tools.registry import Tool

ACCOUNT_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="get_trading_readiness",
        display_title="检查交易准备状态",
        display_params=(),
        description=(
            "仅在用户明确询问旧平台托管交易状态时，查询 Trading Readiness、Venue Margin、"
            "Agent Trading Account、Mandate 与 Strategy Binding；不得在创建用户钱包直签交易意图前调用"
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="get_account_summary",
        display_title="查询账户概览",
        display_params=(),
        resource="account_summary",
        description="查询当前账户的资产综合概览，包含现金余额、可用余额及预留资金",
        parameters={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_balances",
        display_title="查询余额",
        display_params=("asset",),
        resource="balances",
        description=(
            "查询 BSC 现货代币余额（USDT 等稳定币；原生 BNB 与 Meme 仅在 Core 投影返回时才有）。"
            "不是永续仓位；合约持仓请用 get_positions"
        ),
        parameters={
            "type": "object",
            "properties": {
                "asset": {
                    "type": "string",
                    "description": "资产代币符号筛选（可选，如 USDT, BTC, ETH），为空则返回所有代币",
                }
            },
        },
    ),
    Tool(
        name="agentos_get_balances",
        display_title="查询余额",
        display_params=("asset",),
        external=True,
        description="测试网真链路：查询账户资产代币余额",
        parameters={
            "type": "object",
            "properties": {
                "asset": {"type": "string", "description": "资产代币符号筛选（可选）"}
            },
        },
    ),
)
