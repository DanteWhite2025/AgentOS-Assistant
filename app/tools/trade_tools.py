"""交易类工具 —— 只创建待用户确认的交易意图。

红线：Agent 只创建 PENDING_USER Intent，**不执行、不签名、不广播**；
用户确认后由 Mobile 钱包直签并提交。
"""

from __future__ import annotations

from app.tools.registry import Tool

TRADE_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="create_trade_draft",
        display_title="创建交易草案",
        display_params=(
            "symbol",
            "side",
            "order_type",
            "qty",
            "notional_usdc",
            "price",
            "leverage",
            "max_slippage",
            "reduce_only",
        ),
        write=True,
        description=(
            "创建 PERP 待用户确认交易意图的唯一入口。必须提供 symbol 以便通过 Core Catalog 自动解析；"
            "只有本工具成功返回 trade_intent_id 和 PENDING_USER，才证明交易意图已创建。"
            "本工具不依赖旧 Trading Readiness、Mandate 或 Strategy Binding，且不确认、签名、广播或执行。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "可供交易的资产名，例如 BTC"},
                "side": {
                    "type": "string",
                    "enum": ["buy", "sell"],
                    "description": "交易方向：buy（买入）或 sell（卖出）",
                },
                "order_type": {
                    "type": "string",
                    "enum": ["limit"],
                    "description": "订单类型：当前仅允许 limit",
                },
                "qty": {"type": "string", "description": "交易数量；与 notional_usdc 二选一"},
                "notional_usdc": {
                    "type": "string",
                    "description": "约多少 USDC 的名义金额；与 qty 二选一",
                },
                "price": {"type": "string", "description": "目标限价（必填）"},
                "leverage": {"type": "string", "description": "不得超过 Core Catalog 标的限制"},
                "max_slippage": {"type": "string", "description": "Decimal 比例"},
                "reduce_only": {"type": "boolean"},
            },
            "required": ["symbol", "side", "order_type", "price", "leverage"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="agentos_create_trade_draft",
        display_title="创建交易草案",
        display_params=(
            "symbol",
            "side",
            "order_type",
            "qty",
            "notional_usdc",
            "price",
            "leverage",
            "max_slippage",
            "reduce_only",
        ),
        write=True,
        external=True,
        description="兼容入口：创建 Core PERP 待确认草案；绝不确认或执行",
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "可供交易的资产名，例如 BTC"},
                "side": {
                    "type": "string",
                    "enum": ["buy", "sell"],
                    "description": "交易方向",
                },
                "order_type": {
                    "type": "string",
                    "enum": ["limit"],
                    "description": "订单类型：当前仅允许 limit",
                },
                "qty": {"type": "string", "description": "交易数量；与 notional_usdc 二选一"},
                "notional_usdc": {
                    "type": "string",
                    "description": "约多少 USDC 的名义金额；与 qty 二选一",
                },
                "price": {"type": "string", "description": "目标限价（必填）"},
                "leverage": {"type": "string"},
                "max_slippage": {"type": "string"},
                "reduce_only": {"type": "boolean"},
            },
            "required": ["symbol", "side", "order_type", "price", "leverage"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="create_perp_trade_draft",
        display_title="创建永续合约草案",
        display_params=(
            "symbol",
            "side",
            "order_type",
            "qty",
            "notional_usdc",
            "price",
            "leverage",
            "max_slippage",
            "reduce_only",
        ),
        write=True,
        external=True,
        description=(
            "按 symbol 创建 Hyperliquid Testnet PERP LIMIT Draft。"
            "只创建待用户确认的 Draft，永不确认、签名或执行。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "side": {"type": "string", "enum": ["buy", "sell"]},
                "order_type": {"type": "string", "enum": ["limit"]},
                "qty": {"type": "string"},
                "notional_usdc": {"type": "string"},
                "price": {"type": "string"},
                "leverage": {"type": "string"},
                "max_slippage": {"type": "string"},
                "reduce_only": {"type": "boolean"},
            },
            "required": ["symbol", "side", "order_type", "price", "leverage"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="create_spot_swap_draft",
        display_title="创建现货兑换草案",
        display_params=("sell_asset", "buy_asset", "amount", "max_slippage"),
        write=True,
        external=True,
        description=(
            "创建 BSC Testnet SPOT exact-input Swap 待确认草案。"
            "参数指定卖出币种、买入币种、卖出数量与滑点限制。"
            "Token 地址、Router、精度和 Route 均由 Core 决定；只创建 Draft，永不确认、签名、广播或直接成交。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "sell_asset": {
                    "type": "string",
                    "description": "卖出资产/币种名称，例如 BNB",
                },
                "buy_asset": {
                    "type": "string",
                    "description": "买入资产/币种名称，例如 USDT",
                },
                "amount": {
                    "type": "string",
                    "description": "卖出数量（人类可读 Decimal，例如 0.01）",
                },
                "max_slippage": {
                    "type": "string",
                    "description": "最大允许滑点（Decimal 比例，例如 0.005 表示 0.5%）",
                },
            },
            "required": ["sell_asset", "buy_asset", "amount", "max_slippage"],
            "additionalProperties": False,
        },
    ),
)
