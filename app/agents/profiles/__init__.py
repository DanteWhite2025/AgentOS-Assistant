"""各 agent 身份配置。新增/调整 agent 只改配置，不加子目录/类。"""

from __future__ import annotations

from app.agents.runner import AgentProfile

# 只读工具集（对应接口文档工具清单）
_READ_TOOLS = (
    "get_account_summary",
    "get_balances",
    "get_positions",
    "get_orders",
    "market_tickers",
    "get_market_price",
    "get_risk_summary",
)

AVATAR = AgentProfile(
    name="avatar",
    model="gpt-realtime-2.1",
    system_prompt="你是 YesUP 的数字人，负责表达与解释，不直接下单。",
    tools=_READ_TOOLS,
)

STRATEGY = AgentProfile(
    name="strategy",
    model="openai:gpt-5",  # 文本推理，异步；provider 可插拔（Pydantic AI）
    system_prompt="你负责市场解释与策略信号，只能生成 TradeProposal，不能直接下单。",
    tools=_READ_TOOLS + ("create_trade_draft",),
)

# 占位（MVP 未接入运行）：启用需事件源/组合数据（见架构 README 5.3）。
MONITOR = AgentProfile(
    name="monitor",
    model="openai:gpt-5",
    system_prompt="你负责价格/仓位/资金费率/清算距离监控，第一阶段只告警，不自动平仓。",
    tools=_READ_TOOLS,
)
FUND_MANAGER = AgentProfile(
    name="fund_manager",
    model="openai:gpt-5",
    system_prompt="你负责组合分析与再平衡计划，第一阶段只分析，不做无人值守资金管理。",
    tools=_READ_TOOLS,
)

# 运行时启用的 profile（monitor/fund_manager 待接入事件源/组合数据后加入）
PROFILES: dict[str, AgentProfile] = {p.name: p for p in (AVATAR, STRATEGY)}
