"""策略收益指标与净值曲线 —— 纯函数，输入净值快照序列，无 IO。

指标口径（前端大屏按此展示，改口径必须同步前端）：
- total_pnl      窗口内 末净值 - 首净值，金额
- total_pnl_pct  total_pnl / 首净值 * 100，**百分数**（3.66 表示 3.66%）
- win_rate       盈利周期数 / 有盈亏的周期数，**比例** 0~1；持平周期不计入分母。
                 全部快照都带 realized_pnl 时按已实现盈亏增量判定，否则回落到净值增量。
- max_drawdown   窗口内 max((历史峰值 - 当前净值) / 历史峰值)，**比例** 0~1
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Final

from app.strategy.equity import EquitySnapshot

_ZERO: Final = Decimal(0)
_HUNDRED: Final = Decimal(100)


@dataclass(frozen=True)
class StrategyMetrics:
    total_pnl: Decimal
    total_pnl_pct: Decimal
    win_rate: Decimal
    max_drawdown: Decimal


ZERO_METRICS: Final = StrategyMetrics(_ZERO, _ZERO, _ZERO, _ZERO)


@dataclass(frozen=True)
class CurvePoint:
    """曲线上的一个点。drawdown_pct 是**百分数**（1.2 表示回撤 1.2%）。"""

    timestamp: int
    equity: Decimal
    pnl: Decimal
    drawdown_pct: Decimal


def _sorted(snapshots: Iterable[EquitySnapshot]) -> list[EquitySnapshot]:
    return sorted(snapshots, key=lambda s: s.taken_at)


def _deltas(series: Sequence[EquitySnapshot]) -> list[Decimal]:
    """逐周期盈亏增量。全部快照都有已实现盈亏时优先用它，否则用净值增量。"""
    if all(s.realized_pnl is not None for s in series):
        values = [s.realized_pnl or _ZERO for s in series]
    else:
        values = [s.equity for s in series]
    return [curr - prev for prev, curr in pairwise(values)]


def _max_drawdown(series: Sequence[EquitySnapshot]) -> Decimal:
    peak = series[0].equity
    worst = _ZERO
    for snapshot in series:
        peak = max(peak, snapshot.equity)
        if peak > 0:
            worst = max(worst, (peak - snapshot.equity) / peak)
    return worst


def compute_metrics(snapshots: Iterable[EquitySnapshot]) -> StrategyMetrics:
    series = _sorted(snapshots)
    if len(series) < 2:
        return ZERO_METRICS

    baseline = series[0].equity
    total_pnl = series[-1].equity - baseline
    # 基线为 0 时收益率无意义（除零），给 0 而不是无穷大
    total_pnl_pct = (total_pnl / baseline * _HUNDRED) if baseline != 0 else _ZERO

    deltas = [d for d in _deltas(series) if d != 0]
    win_rate = (
        Decimal(sum(1 for d in deltas if d > 0)) / Decimal(len(deltas)) if deltas else _ZERO
    )

    return StrategyMetrics(
        total_pnl=total_pnl,
        total_pnl_pct=total_pnl_pct,
        win_rate=win_rate,
        max_drawdown=_max_drawdown(series),
    )


def build_curve(snapshots: Iterable[EquitySnapshot]) -> list[CurvePoint]:
    """净值序列 → 前端曲线点：pnl 相对窗口首点，drawdown_pct 相对历史峰值。"""
    series = _sorted(snapshots)
    if not series:
        return []

    baseline = series[0].equity
    peak = baseline
    points: list[CurvePoint] = []
    for snapshot in series:
        peak = max(peak, snapshot.equity)
        drawdown = ((peak - snapshot.equity) / peak * _HUNDRED) if peak > 0 else _ZERO
        points.append(
            CurvePoint(
                timestamp=int(snapshot.taken_at.timestamp()),
                equity=snapshot.equity,
                pnl=snapshot.equity - baseline,
                drawdown_pct=drawdown,
            )
        )
    return points


def merge_by_timestamp(snapshots: Iterable[EquitySnapshot]) -> list[EquitySnapshot]:
    """多策略合并成 Agent 级序列：同一采样时刻的净值/已实现盈亏求和。

    采样器对同一 Agent 的所有策略用同一个 `taken_at` 落盘，因此按时间戳分组即可对齐。
    """
    merged: dict[object, EquitySnapshot] = {}
    for snapshot in _sorted(snapshots):
        existing = merged.get(snapshot.taken_at)
        if existing is None:
            merged[snapshot.taken_at] = snapshot
            continue
        realized = existing.realized_pnl
        if snapshot.realized_pnl is not None:
            realized = (realized or _ZERO) + snapshot.realized_pnl
        merged[snapshot.taken_at] = EquitySnapshot(
            strategy_id="*",
            user_id=existing.user_id,
            agent_id=existing.agent_id,
            taken_at=existing.taken_at,
            equity=existing.equity + snapshot.equity,
            realized_pnl=realized,
        )
    return list(merged.values())
