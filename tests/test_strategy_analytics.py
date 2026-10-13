"""策略收益指标纯计算 —— 无 IO，全部走定死的净值序列。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.strategy.analytics import (
    ZERO_METRICS,
    build_curve,
    compute_metrics,
    merge_by_timestamp,
)
from app.strategy.equity import EquitySnapshot, parse_range

_T0 = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _snap(minutes: int, equity: str, realized: str | None = None, sid: str = "s1") -> EquitySnapshot:
    return EquitySnapshot(
        strategy_id=sid,
        user_id="u1",
        agent_id="a1",
        taken_at=_T0 + timedelta(minutes=minutes),
        equity=Decimal(equity),
        realized_pnl=Decimal(realized) if realized is not None else None,
    )


# --- parse_range ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "hours"), [("1h", 1), ("6h", 6), ("24h", 24), ("24H", 24), (" 6h ", 6)]
)
def test_parse_range_supported(text, hours):
    assert parse_range(text) == timedelta(hours=hours)


@pytest.mark.parametrize("text", ["", "7d", "3h", "1m", "abc", "1"])
def test_parse_range_rejects_unsupported(text):
    with pytest.raises(ValueError, match="range"):
        parse_range(text)


# --- compute_metrics -----------------------------------------------------


def test_metrics_empty_series_is_zero():
    assert compute_metrics([]) == ZERO_METRICS


def test_metrics_single_point_has_no_pnl():
    m = compute_metrics([_snap(0, "22000")])
    assert m.total_pnl == Decimal(0)
    assert m.total_pnl_pct == Decimal(0)
    assert m.win_rate == Decimal(0)
    assert m.max_drawdown == Decimal(0)


def test_metrics_total_pnl_and_pct_from_first_to_last():
    # 22000 → 22805.50：+805.50，+3.66%
    m = compute_metrics([_snap(0, "22000"), _snap(10, "22400"), _snap(20, "22805.50")])
    assert m.total_pnl == Decimal("805.50")
    assert m.total_pnl_pct.quantize(Decimal("0.01")) == Decimal("3.66")


def test_metrics_max_drawdown_uses_running_peak():
    # 峰值 24000 → 谷底 21000：回撤 3000/24000 = 0.125
    m = compute_metrics(
        [_snap(0, "20000"), _snap(10, "24000"), _snap(20, "21000"), _snap(30, "23000")]
    )
    assert m.max_drawdown == Decimal("0.125")


def test_metrics_drawdown_zero_on_monotonic_rise():
    assert compute_metrics([_snap(0, "100"), _snap(1, "110"), _snap(2, "120")]).max_drawdown == 0


def test_metrics_win_rate_counts_positive_periods():
    # 增量：+100, -50, +100 → 2/3
    m = compute_metrics([_snap(0, "1000"), _snap(1, "1100"), _snap(2, "1050"), _snap(3, "1150")])
    assert m.win_rate.quantize(Decimal("0.001")) == Decimal("0.667")


def test_metrics_win_rate_ignores_flat_periods():
    # 增量：0, +10, 0, -10 → 有效周期 2，胜 1 → 0.5
    m = compute_metrics(
        [_snap(0, "100"), _snap(1, "100"), _snap(2, "110"), _snap(3, "110"), _snap(4, "100")]
    )
    assert m.win_rate == Decimal("0.5")


def test_metrics_win_rate_prefers_realized_pnl_when_present():
    # 净值一路涨，但已实现盈亏 3 个周期里只有 1 个为正 → 胜率跟着已实现盈亏走
    series = [
        _snap(0, "1000", "0"),
        _snap(1, "1100", "50"),
        _snap(2, "1200", "20"),
        _snap(3, "1300", "10"),
    ]
    assert compute_metrics(series).win_rate.quantize(Decimal("0.001")) == Decimal("0.333")


def test_metrics_falls_back_to_equity_when_realized_partially_missing():
    series = [_snap(0, "1000", "0"), _snap(1, "1100"), _snap(2, "1200", "20")]
    assert compute_metrics(series).win_rate == Decimal(1)


def test_metrics_zero_baseline_yields_zero_pct_not_crash():
    m = compute_metrics([_snap(0, "0"), _snap(1, "500")])
    assert m.total_pnl == Decimal(500)
    assert m.total_pnl_pct == Decimal(0)


def test_metrics_sorts_unordered_input():
    m = compute_metrics([_snap(20, "1200"), _snap(0, "1000"), _snap(10, "1100")])
    assert m.total_pnl == Decimal(200)


# --- build_curve ---------------------------------------------------------


def test_build_curve_shape_matches_frontend_contract():
    points = build_curve([_snap(0, "22000"), _snap(10, "24000"), _snap(20, "23712")])
    assert [p.timestamp for p in points] == [
        int((_T0 + timedelta(minutes=m)).timestamp()) for m in (0, 10, 20)
    ]
    assert points[0].pnl == Decimal(0)
    assert points[0].drawdown_pct == Decimal(0)
    assert points[2].equity == Decimal(23712)
    assert points[2].pnl == Decimal(1712)
    # 峰值 24000 → 23712，回撤 1.2%
    assert points[2].drawdown_pct.quantize(Decimal("0.01")) == Decimal("1.20")


def test_build_curve_empty():
    assert build_curve([]) == []


# --- merge_by_timestamp --------------------------------------------------


def test_merge_sums_strategies_sharing_a_timestamp():
    merged = merge_by_timestamp(
        [
            _snap(0, "1000", "10", sid="s1"),
            _snap(0, "2000", "20", sid="s2"),
            _snap(10, "1100", "15", sid="s1"),
            _snap(10, "2100", "25", sid="s2"),
        ]
    )
    assert [s.equity for s in merged] == [Decimal(3000), Decimal(3200)]
    assert [s.realized_pnl for s in merged] == [Decimal(30), Decimal(40)]


def test_merge_keeps_realized_none_when_all_missing():
    merged = merge_by_timestamp([_snap(0, "1000", sid="s1"), _snap(0, "2000", sid="s2")])
    assert len(merged) == 1
    assert merged[0].realized_pnl is None


def test_merge_empty():
    assert merge_by_timestamp([]) == []
