"""行情类工具 —— market_tickers 与单 symbol 兼容入口。

定义行情查询 Function Calling 规格，连核心后台 yesup-core 接口。
"""

from __future__ import annotations

from app.tools.registry import Tool

MARKET_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="search_market_catalog",
        display_title="查询可交易标的",
        display_params=("query", "market_type"),
        external=True,
        description=(
            "查询 Core 权威可交易标的目录。交易规划必须先调用；结果中的 instrument_id、"
            "market_type、资产、场所能力和精度才是后续 Draft 的唯一可信来源。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "market_type": {"type": "string", "enum": ["SPOT", "PERP"]},
                "query": {"type": "string", "description": "用户提到的资产或交易对"},
                "cursor": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="market_tickers",
        display_title="查询行情",
        display_params=("symbols",),
        resource="market_data_tickers",
        description=(
            "批量查询 Core Market Catalog 已支持标的的 Hyperliquid Mainnet 只读 PERP 行情。"
            "实时价格、24小时涨跌或市场趋势必须调用，不得使用模型记忆猜测。"
            "结果是行情数据，不是订单、成交、余额或结算证据；stale=true 时必须说明延迟。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "symbols": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 50,
                    "items": {"type": "string"},
                    "description": "从 Core Catalog 取得的 PERP venue_symbol 列表",
                }
            },
            "required": ["symbols"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_market_price",
        display_title="查询实时价格",
        display_params=("symbol",),
        resource="market_data_ticker",
        description=(
            "用户查询实时价格、当前价格、24小时涨跌或市场趋势时必须调用；"
            "不允许使用模型训练记忆猜测价格。symbol 必须来自 Core Catalog；"
            "返回 Hyperliquid Mainnet PERP 行情，不是现货报价；stale=true 时必须说明延迟。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Core Catalog 返回的 PERP venue_symbol",
                }
            },
            "required": ["symbol"],
            "additionalProperties": False,
        },
    ),
)
