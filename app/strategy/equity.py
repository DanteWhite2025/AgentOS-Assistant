"""策略净值快照领域模型 + 前端时间窗口解析。

上游 AgentOS 只提供**当前时点**的余额/仓位，没有历史净值接口，因此历史曲线由
Runtime 侧周期性采样落盘（见 `app.strategy.sampler`）。1 Agent = 1 Core 账户，
策略是账户内的逻辑容器，所以策略级净值是 Agent 账户权益按 allocation 权重的
**逻辑分摊**，不是独立托管的资金。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from app.strategy.decimals import to_decimal

# 前端大屏当前只需要这三档；新增档位必须同时更新前端与本表
_RANGES: Final[dict[str, timedelta]] = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
}
DEFAULT_RANGE: Final = "24h"
SUPPORTED_RANGES: Final = tuple(_RANGES)


def parse_range(text: str) -> timedelta:
    """把 `1h`/`6h`/`24h` 解析成时间窗口；其它取值一律拒绝而非静默兜底。"""
    key = str(text).strip().lower()
    if key not in _RANGES:
        raise ValueError(f"range 必须是 {SUPPORTED_RANGES} 之一，收到 {text!r}")
    return _RANGES[key]


@dataclass(frozen=True)
class EquitySnapshot:
    """某策略在某时刻的净值快照。taken_at 必须带时区，否则窗口比较会错位。"""

    strategy_id: str
    user_id: str
    agent_id: str
    taken_at: datetime
    equity: Decimal
    realized_pnl: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("strategy_id", "user_id", "agent_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} 不能为空")
        if self.taken_at.tzinfo is None:
            raise ValueError("taken_at 必须是带时区的时间")
        object.__setattr__(self, "equity", to_decimal(self.equity, "equity"))
        if self.realized_pnl is not None:
            object.__setattr__(
                self, "realized_pnl", to_decimal(self.realized_pnl, "realized_pnl")
            )
