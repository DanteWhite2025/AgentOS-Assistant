"""策略引擎 —— 下单草案的 agent 侧预检。

只做 agent 侧 mandate 校验（额度/资产/市场/杠杆/有效期）+ 二次确认标记；
真实风控/权限/确认/幂等仍由 yesup-core 完成。只读工具不过此引擎（白名单即可）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.policies.mandate import AgentMandate


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""
    requires_confirmation: bool = False


class PolicyViolationError(Exception):
    """下单草案违反 Agent Mandate（对应端点 403 POLICY_VIOLATION）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class PolicyEngine:
    def __init__(self, mandate: AgentMandate) -> None:
        self._mandate = mandate

    def check_trade_draft(self, args: dict[str, Any], now: datetime) -> PolicyDecision:
        mandate = self._mandate

        if mandate.expires_at is not None and now >= mandate.expires_at:
            return PolicyDecision(False, "Agent Mandate 已过期")

        symbol = args.get("symbol")
        if mandate.allowed_assets is not None and symbol not in mandate.allowed_assets:
            return PolicyDecision(False, f"资产 {symbol} 不在授权范围")

        market = args.get("market")
        if (
            mandate.allowed_markets is not None
            and market is not None
            and market not in mandate.allowed_markets
        ):
            return PolicyDecision(False, f"市场 {market} 不在授权范围")

        notional = _to_decimal(args.get("notional"))
        if notional is None:
            notional = _to_decimal(args.get("notional_usdc"))
        if notional is None:
            qty = _to_decimal(args.get("qty"))
            price = _to_decimal(args.get("price"))
            if qty is not None and price is not None:
                notional = qty * price
        if mandate.max_notional is not None and notional is not None and notional > mandate.max_notional:
            return PolicyDecision(False, f"下单名义 {notional} 超过额度 {mandate.max_notional}")

        leverage = _to_decimal(args.get("leverage"))
        if mandate.max_leverage is not None and leverage is not None and leverage > mandate.max_leverage:
            return PolicyDecision(False, f"杠杆 {leverage} 超过上限 {mandate.max_leverage}")

        return PolicyDecision(True, "", requires_confirmation=mandate.require_confirmation)
