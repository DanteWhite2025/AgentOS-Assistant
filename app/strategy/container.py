"""策略容器领域模型 —— Runtime 侧逻辑配额沙箱（不可变 + 构造即校验）。

1 Agent = 1 Core 账户；策略在 Agent 账户内做逻辑资金分配与风控预检。金额一律 Decimal。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Final

_RUNTIME_ENVS: Final = ("live", "paper", "backtest")


class StrategyState(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    TERMINATED = "TERMINATED"
    EXPIRED = "EXPIRED"


class IllegalStateTransition(Exception):
    """策略状态迁移非法（对应端点 409）。"""


def _money(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} 不能是布尔值")
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} 不是合法数值: {value!r}") from exc
    return dec


@dataclass(frozen=True)
class StrategyContainer:
    strategy_id: str
    user_id: str
    agent_id: str
    name: str
    template_id: str
    runtime_env: str
    allocation: Decimal
    capital_capacity: Decimal
    strategy_capacity: Decimal
    max_leverage: Decimal
    created_at: datetime
    updated_at: datetime
    allowed_assets: tuple[str, ...] | None = None
    expires_at: datetime | None = None
    # 止盈/止损：0~1 比例（0.15 = 15%）。None = 不设，绝不兜底成 0（0 意味着立刻平仓）
    take_profit_pct: Decimal | None = None
    stop_loss_pct: Decimal | None = None
    state: StrategyState = StrategyState.ACTIVE
    version: int = 1

    def __post_init__(self) -> None:
        for name in ("strategy_id", "user_id", "agent_id", "name", "template_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} 不能为空")
        if self.runtime_env not in _RUNTIME_ENVS:
            raise ValueError(f"runtime_env 必须是 {_RUNTIME_ENVS}，收到 {self.runtime_env!r}")

        # Validate and normalize state field
        if not isinstance(self.state, StrategyState):
            try:
                object.__setattr__(self, "state", StrategyState(self.state))
            except ValueError as exc:
                raise ValueError(f"state 必须是有效的 StrategyState，收到 {self.state!r}") from exc

        for name in ("allocation", "capital_capacity", "strategy_capacity", "max_leverage"):
            object.__setattr__(self, name, _money(getattr(self, name), name))
        if self.max_leverage <= 0:
            raise ValueError("max_leverage 必须 > 0")
        if self.allocation < 0:
            raise ValueError("allocation 不能为负")
        if self.allocation > self.capital_capacity:
            raise ValueError("allocation 不能超过 capital_capacity（本金容量）")
        if self.capital_capacity > self.strategy_capacity:
            raise ValueError("capital_capacity 不能超过 strategy_capacity（策略容量）")

        self._validate_risk_params()

    def _validate_risk_params(self) -> None:
        """止盈/止损为 0~1 比例。两者都必须 > 0；止损额外不得超过 100%（本金亏光到头）。"""
        for name in ("take_profit_pct", "stop_loss_pct"):
            raw = getattr(self, name)
            if raw is None:
                continue
            value = _money(raw, name)
            if value <= 0:
                raise ValueError(f"{name} 必须 > 0（不设则留空，0 意味着立刻平仓）")
            object.__setattr__(self, name, value)
        if self.stop_loss_pct is not None and self.stop_loss_pct > 1:
            raise ValueError("stop_loss_pct 不能超过 1（100%）")

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def _evolve(self, now: datetime, **changes: Any) -> StrategyContainer:
        return replace(self, updated_at=now, version=self.version + 1, **changes)

    def pause(self, now: datetime) -> StrategyContainer:
        if self.state is not StrategyState.ACTIVE:
            raise IllegalStateTransition(f"仅 ACTIVE 可暂停，当前 {self.state}")
        return self._evolve(now, state=StrategyState.PAUSED)

    def resume(self, now: datetime) -> StrategyContainer:
        if self.state is not StrategyState.PAUSED:
            raise IllegalStateTransition(f"仅 PAUSED 可恢复，当前 {self.state}")
        if self.is_expired(now):
            raise IllegalStateTransition("策略已过期，不能恢复")
        return self._evolve(now, state=StrategyState.ACTIVE)

    def terminate(self, now: datetime) -> StrategyContainer:
        if self.state in (StrategyState.TERMINATED, StrategyState.EXPIRED):
            raise IllegalStateTransition(f"终态不可再终止，当前 {self.state}")
        return self._evolve(now, state=StrategyState.TERMINATED)

    def expire(self, now: datetime) -> StrategyContainer:
        if self.state in (StrategyState.TERMINATED, StrategyState.EXPIRED):
            raise IllegalStateTransition(f"终态不可再过期，当前 {self.state}")
        return self._evolve(now, state=StrategyState.EXPIRED)

    def with_allocation(self, value: Any, now: datetime) -> StrategyContainer:
        new_alloc = _money(value, "allocation")
        if new_alloc < 0 or new_alloc > self.capital_capacity:
            raise ValueError("allocation 必须在 [0, capital_capacity] 内")
        return self._evolve(now, allocation=new_alloc)
