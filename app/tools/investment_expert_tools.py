"""投研证据抓取工具（T3）。

用户提出需要专业行情/宏观归因分析的问题时，Agent 调用此工具抓取一份行情快照
与新闻证据包，再由外层主脑基于证据自己写出深度分析结论——本工具不做分析、
不调 LLM，只负责取证。见 docs/plans/2026-09-02-investment-expert-evidence/。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Protocol

from app.tools.registry import Tool

logger = logging.getLogger(__name__)

_MAX_SYMBOLS = 8


class _EvidenceClient(Protocol):
    async def collect_evidence(
        self, question: str, *, symbols: Sequence[str] = (), chat_id: str | None = None
    ) -> dict[str, Any]: ...


async def ask_investment_expert(
    message: str,
    *,
    client: _EvidenceClient,
    symbols: Sequence[str] = (),
    chat_id: str | None = None,
) -> dict[str, Any]:
    """抓取行情与新闻证据包，失败一律降级为 error dict，绝不抛异常。"""

    message = message.strip()
    if not message:
        return {"error": "ask_investment_expert 必须提供分析请求内容"}

    try:
        return await client.collect_evidence(message, symbols=symbols, chat_id=chat_id)
    except Exception as exc:
        logger.warning("投研证据抓取调用失败: %s", exc)
        return {"error": str(exc)}


# ---- Tool 注册定义 ----

INVESTMENT_EXPERT_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="ask_investment_expert",
        display_title="抓取行情与新闻证据",
        display_params=("message", "symbols"),
        external=True,
        description=(
            "抓取行情与新闻证据包，供你自己做深度归因分析。本工具不返回任何结论，"
            "只返回：① 指定加密标的的 Core 实时行情快照（最新价、24h 涨跌幅、24h 高低点、"
            "资金费率、持仓量、成交额）；② 近期相关新闻要点。"
            "当用户问「为什么涨/跌」「宏观复盘」「多标的对比」等需要具体数据支撑的问题时，"
            "一次性调用本工具并把全部标的一起传入，然后基于返回的数据自己写出分析结论。"
            "不得用于查询个人交易账户、余额或订单信息。"
            "【禁止误用】用户单纯询问某个标的的当前价格、实时价格、现价或行情报价时严禁调用本工具，"
            "必须且仅使用 get_market_price 或 market_tickers；本工具仅用于多维度深度研判与宏观复盘。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "用户的完整问题原文，保留全部查询意图，用于检索相关新闻。",
                },
                "symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": _MAX_SYMBOLS,
                    "description": (
                        "需要行情快照的加密标的基础符号（如 BTC、ETH、SOL）。"
                        "黄金、原油、美股、美债等非加密标的不要填入，"
                        "这些标的的信息只会出现在新闻部分。"
                    ),
                },
            },
            "required": ["message"],
            "additionalProperties": False,
        },
    ),
)

# 兼容别名
MINARA_TOOLS = INVESTMENT_EXPERT_TOOLS
