"""用户偏好记忆受控工具 —— 严格限制 category 白名单与 schema 强校验。

安全红线：
1. category 严格白名单：只允许非风控偏好（关注列表、常用策略风格、通知设置、界面偏好）。
2. 严禁修改交易硬限制（max_leverage, capital_limit, daily_notional 等），该类限制必须由 Core 权威决定。
3. 严格字符集约束：能进 system prompt 的每一个字段都有明确的 charset 与长度限制，严禁自由注入攻击文本。
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, StringConstraints

from app.tools.registry import Tool

ALLOWED_MEMORY_CATEGORIES = {
    "watchlist",
    "trading_style",
    "notification_preference",
    "ui_preference",
}


def _normalize_symbol(value: Any) -> Any:
    """先规范化再校验：Pydantic 的 pattern 早于 to_upper 执行，小写必须前置转换。"""
    return value.strip().upper() if isinstance(value, str) else value


SymbolType = Annotated[
    str,
    BeforeValidator(_normalize_symbol),
    StringConstraints(pattern=r"^[A-Z0-9]{1,10}$"),
]

_KEY_REGEX = re.compile(r"^[a-z0-9_]{1,32}$")


def validate_preference_key(key: Any) -> str:
    """严格校验 key 格式，只允许小写字母、数字与下划线，长度 1-32。"""
    s_key = str(key).strip()
    if not _KEY_REGEX.match(s_key):
        raise ValueError(f"偏好 key 格式非法: '{key}'。仅支持小写字母、数字与下划线（长度 1-32）")
    return s_key


class WatchlistContent(BaseModel):
    symbols: list[SymbolType] = Field(
        min_length=1,
        max_length=20,
        description=(
            "关注的基础币种代码列表，只填基础币种（如 BTC、ETH、1000PEPE），"
            "不带计价币或 -PERP 后缀。单项仅限 1-10 位大写字母/数字，小写会自动规范化为大写。"
        ),
    )


class TradingStyleContent(BaseModel):
    preferred_strategy: Literal["grid", "dca", "momentum", "arbitrage", "manual"] = Field(
        description="偏好的策略类型"
    )
    timeframe: Literal["scalp", "intraday", "swing", "long_term"] = Field(
        default="intraday", description="操作时间周期"
    )


NotificationChannel = Literal["app", "email", "sms"]


def _default_channels() -> list[NotificationChannel]:
    return ["app"]


class NotificationPreferenceContent(BaseModel):
    price_alert_threshold_pct: float = Field(
        default=5.0, ge=0.5, le=50.0, description="价格告警波动阈值百分比"
    )
    channels: list[NotificationChannel] = Field(default_factory=_default_channels)


class UiPreferenceContent(BaseModel):
    quote_currency: Literal["USDC", "USDT", "USD"] = Field(default="USDC")
    theme: Literal["dark", "light", "system"] = Field(default="dark")


_SCHEMA_MAP: dict[str, type[BaseModel]] = {
    "watchlist": WatchlistContent,
    "trading_style": TradingStyleContent,
    "notification_preference": NotificationPreferenceContent,
    "ui_preference": UiPreferenceContent,
}


def validate_preference_content(category: str, raw_content: Any) -> dict[str, Any]:
    """严格校验偏好内容，拒绝非法 category 与格式错误。"""
    if category not in ALLOWED_MEMORY_CATEGORIES:
        raise ValueError(
            f"不允许修改的偏好分类: {category}。支持的分类: {sorted(ALLOWED_MEMORY_CATEGORIES)}"
        )
    schema_cls = _SCHEMA_MAP[category]
    # 兼容 watchlist 的逗号分隔简写
    if category == "watchlist" and isinstance(raw_content, str):
        raw_content = {"symbols": [s.strip() for s in raw_content.split(",") if s.strip()]}
    validated = schema_cls.model_validate(raw_content)
    return validated.model_dump()


MEMORY_TOOLS: tuple[Tool, ...] = (
    Tool(
        name="update_user_preference",
        display_title="更新偏好设置",
        display_params=("category", "key", "content"),
        description=(
            "更新用户自述偏好记忆（如关注币种、常用策略风格、通知与展示偏好）。"
            "watchlist 只填基础币种代码（如 BTC、ETH），不带计价币或 -PERP 后缀。"
            "严禁用于尝试修改账户杠杆、额度或风控限制。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": sorted(ALLOWED_MEMORY_CATEGORIES),
                    "description": "偏好分类",
                },
                "key": {
                    "type": "string",
                    "description": "偏好项唯一键，仅限小写字母数字下划线，例如 'default'",
                },
                "content": {
                    "type": "object",
                    "description": "经过结构化约束的偏好内容",
                },
            },
            "required": ["category", "key", "content"],
            "additionalProperties": False,
        },
    ),
)
