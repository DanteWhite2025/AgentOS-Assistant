"""意图路由 —— 判断用户意图（查询/交易/闲聊/风险咨询），分派到合适的 agent/流程。

当前状态：MVP 单 agent，路由由 AgentRunner + LLM 的工具选择隐式完成，未独立。
扩展点：多 agent（avatar/strategy/monitor/fund_manager）分派时在此显式路由。
"""

from __future__ import annotations
