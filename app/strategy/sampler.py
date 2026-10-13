"""净值采样器 —— 周期性把 AgentOS 账户权益按策略额度分摊后落盘成快照。

上游只有当前时点数据，历史曲线只能靠这里持续采样攒出来。

限制（MVP）：AgentOsStagingClient 是**单 token 身份**客户端，一次采样只能看到该
token 名下的账户；策略按 (user_id, agent_id) 分组各自分摊。多租户采样需要按身份
分别持有客户端，属后续工作。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Protocol

from app.strategy.container import StrategyContainer, StrategyState
from app.strategy.decimals import MONEY_QUANT
from app.strategy.equity import EquitySnapshot
from app.strategy.equity_store import EquityStore
from app.strategy.store import StrategyStore
from app.tools.agentos.trade_flow import discover_trading_account

logger = logging.getLogger(__name__)

# Only an explicitly named venue-equity field may feed strategy equity.  The
# current Core account-summary contract contains ``balance``, ``positions`` and
# ``orders``; its balance is the Core operational Ledger and must never be
# re-labelled as Hyperliquid venue equity.
_EQUITY_KEYS: Final = ("venue_equity", "venue_equity_usd")
_REALIZED_KEYS: Final = ("venue_realized_pnl",)
_ZERO: Final = Decimal(0)


class EquityUnavailable(Exception):
    """Core has no authoritative Venue Equity field; never synthesize one."""

    code = "EQUITY_UNAVAILABLE"
    status = "NOT_AVAILABLE"
    source = "CORE_ACCOUNT_SUMMARY"
    reason = "AUTHORITATIVE_VENUE_EQUITY_MISSING"

    def __init__(self) -> None:
        super().__init__(self.reason)

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "status": self.status,
            "source": self.source,
            "reason": self.reason,
        }


class _AccountSource(Protocol):
    async def get_agent_trading_accounts(self) -> dict[str, Any]: ...

    async def get_account_summary(self, unified_account_id: str) -> dict[str, Any]: ...


def _unwrap(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _pick(source: dict[str, Any], keys: Sequence[str]) -> Decimal | None:
    for key in keys:
        if key in source:
            return _as_decimal(source[key])
    return None


def extract_equity(payload: Any) -> Decimal:
    """Read only an explicit Venue Equity field; never infer from Ledger data."""
    data = _unwrap(payload)
    found = _pick(data, _EQUITY_KEYS)
    if found is None:
        raise EquityUnavailable
    return found


def extract_realized_pnl(payload: Any) -> Decimal | None:
    """Only accept an explicitly venue-scoped realized PnL field."""
    data = _unwrap(payload)
    return _pick(data, _REALIZED_KEYS)


def _split(total: Decimal, weights: Sequence[Decimal]) -> list[Decimal]:
    """按权重拆分总额，量化到 8 位小数，余数补给最后一份（不丢钱）。

    前置条件由 `allocate_equity` 保证：weights 非空且每项 > 0。
    """
    weight_sum = sum(weights, _ZERO)
    parts = [(total * w / weight_sum).quantize(MONEY_QUANT) for w in weights]
    parts[-1] = total - sum(parts[:-1], _ZERO)
    return parts


def allocate_equity(
    agent_equity: Decimal, strategies: Sequence[StrategyContainer]
) -> dict[str, Decimal]:
    """把 Agent 账户权益按 ACTIVE 策略的 allocation 权重分摊到各策略。

    这是**逻辑分摊**（1 Agent = 1 Core 账户），不代表策略持有独立资金。
    没有 ACTIVE 策略或额度之和为 0 时返回空字典 —— 没有额度就没有净值可言。
    """
    active = [s for s in strategies if s.state is StrategyState.ACTIVE and s.allocation > 0]
    if not active:
        return {}
    parts = _split(agent_equity, [s.allocation for s in active])
    return {s.strategy_id: part for s, part in zip(active, parts, strict=True)}


class EquitySampler:
    def __init__(
        self,
        agentos_client: _AccountSource,
        strategy_store: StrategyStore,
        equity_store: EquityStore,
    ) -> None:
        self._client = agentos_client
        self._strategies = strategy_store
        self._equity = equity_store
        self._equity_unavailable_logged = False

    async def sample_once(self, now: datetime | None = None) -> list[EquitySnapshot]:
        """采一轮：发现账户 → 读权益 → 按策略分摊 → 落盘。返回写入的快照。"""
        taken_at = now or datetime.now(UTC)
        unified, _venue = await discover_trading_account(self._client)  # type: ignore[arg-type]
        summary = await self._client.get_account_summary(unified)

        equity = extract_equity(summary)
        realized = extract_realized_pnl(summary)

        snapshots: list[EquitySnapshot] = []
        for (user_id, agent_id), owned in _group_by_owner(
            await self._strategies.list_all()
        ).items():
            equity_parts = allocate_equity(equity, owned)
            realized_parts = (
                allocate_equity(realized, owned) if realized is not None else {}
            )
            snapshots += [
                EquitySnapshot(
                    strategy_id=strategy_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    taken_at=taken_at,
                    equity=part,
                    realized_pnl=realized_parts.get(strategy_id),
                )
                for strategy_id, part in equity_parts.items()
            ]

        await self._equity.record_many(snapshots)
        return snapshots

    async def run_forever(self, interval_seconds: float) -> None:
        """后台常驻循环。单轮失败只记日志，不中断后续采样。"""
        while True:
            try:
                written = await self.sample_once()
                self._equity_unavailable_logged = False
                logger.debug("equity_sampler wrote %d snapshots", len(written))
            except asyncio.CancelledError:
                raise
            except EquityUnavailable as exc:
                fields = {"event": "equity_sampler_unavailable", **exc.to_dict()}
                if self._equity_unavailable_logged:
                    logger.debug("equity_sampler source data unavailable", extra=fields)
                else:
                    logger.info("equity_sampler source data unavailable", extra=fields)
                    self._equity_unavailable_logged = True
            except Exception:
                logger.warning("equity_sampler 单轮采样失败，等待下一轮", exc_info=True)
            await asyncio.sleep(interval_seconds)


def _group_by_owner(
    strategies: Iterable[StrategyContainer],
) -> dict[tuple[str, str], list[StrategyContainer]]:
    grouped: dict[tuple[str, str], list[StrategyContainer]] = {}
    for strategy in strategies:
        grouped.setdefault((strategy.user_id, strategy.agent_id), []).append(strategy)
    return grouped
