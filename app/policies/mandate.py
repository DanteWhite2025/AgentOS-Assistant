"""Agent Mandate —— agent 的授权边界（额度/资产/市场/杠杆/有效期）。

对应 README 5.4 / 9.3：每个 Agent Mandate 有额度、资产、市场、有效期。
这是 agent 侧的预检边界；真实风控/权限/确认/幂等仍由 yesup-core 完成（红线不变）。
金额一律 Decimal（禁 float）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class AgentMandate:
    max_notional: Decimal | None = None  # 单次下单名义上限
    max_leverage: Decimal | None = None  # 杠杆上限
    allowed_assets: tuple[str, ...] | None = None  # None = 不限
    allowed_markets: tuple[str, ...] | None = None  # None = 不限
    expires_at: datetime | None = None  # None = 不过期
    require_confirmation: bool = True  # 写工具默认需用户二次确认（core 侧）
    # 策略级止盈/止损，0~1 比例。随 mandate 透传给下游做持仓管理；
    # PolicyEngine 不据此拒单（它们是退出条件，不是下单边界）。
    take_profit_pct: Decimal | None = None
    stop_loss_pct: Decimal | None = None


def default_mandate() -> AgentMandate:
    """MVP 默认 mandate（保守额度）。生产按用户/会话下发。"""
    return AgentMandate(
        max_notional=Decimal(10000),
        max_leverage=Decimal(5),
        allowed_assets=None,
        allowed_markets=None,
        expires_at=None,
        require_confirmation=True,
    )
