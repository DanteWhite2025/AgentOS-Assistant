"""退避重试共享实现的单测。

这套语义原本是 CoreClient 的私有方法，抽出来给 Tavily 复用（见 PR「Tavily
退避重试」）。抽取时行为必须逐条对齐，所以这里把曲线、Retry-After 解析和
抖动边界都钉死。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from app.tools.retry import backoff_delay, backoff_sleep, retry_after_seconds


def _no_jitter(_low: float, _high: float) -> float:
    return 0.0


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(0, 0.25), (1, 0.5), (2, 1.0), (3, 2.0)],
)
def test_backoff_is_exponential(attempt: int, expected: float) -> None:
    assert backoff_delay(attempt, jitter=_no_jitter) == expected


def test_retry_after_seconds_parses_numeric_form() -> None:
    assert retry_after_seconds("12") == 12.0


def test_retry_after_seconds_parses_http_date_form() -> None:
    future = datetime.now(UTC) + timedelta(seconds=30)
    parsed = retry_after_seconds(format_datetime(future))
    assert parsed is not None
    # HTTP-date 精度到秒，加上测试执行耗时，给一个宽松区间。
    assert 25.0 <= parsed <= 31.0


def test_retry_after_in_the_past_clamps_to_zero() -> None:
    past = datetime.now(UTC) - timedelta(seconds=60)
    assert retry_after_seconds(format_datetime(past)) == 0.0


@pytest.mark.parametrize("value", [None, "", "not-a-date", ";;;"])
def test_malformed_retry_after_falls_back_to_default_backoff(value: str | None) -> None:
    """畸形响应头不能让重试崩掉——退回默认曲线即可。"""
    assert retry_after_seconds(value) is None
    assert backoff_delay(0, value, jitter=_no_jitter) == 0.25


def test_retry_after_wins_when_longer_than_default() -> None:
    """上游说要等 5 秒就等 5 秒——它比我们更清楚自己的恢复时间。"""
    assert backoff_delay(0, "5", jitter=_no_jitter) == 5.0


def test_default_backoff_wins_when_retry_after_is_shorter() -> None:
    """取两者较大值，避免上游给了一个过于乐观的值导致我们立刻再打过去。"""
    assert backoff_delay(3, "0.1", jitter=_no_jitter) == 2.0


def test_jitter_is_capped_at_a_quarter_second() -> None:
    """抖动上限固定 0.25s：退避很长时抖动不该跟着线性放大。"""
    captured: list[tuple[float, float]] = []

    def _capture(low: float, high: float) -> float:
        captured.append((low, high))
        return high

    backoff_delay(6, jitter=_capture)  # 基础退避 16s

    assert captured == [(0.0, 0.25)]


def test_jitter_scales_down_for_short_delays() -> None:
    """退避很短时抖动按比例缩小，否则 0.25s 的等待要叠 0.25s 抖动。"""
    captured: list[tuple[float, float]] = []

    def _capture(low: float, high: float) -> float:
        captured.append((low, high))
        return 0.0

    backoff_delay(0, jitter=_capture)

    assert captured == [(0.0, 0.0625)]


async def test_backoff_sleep_uses_injected_sleep() -> None:
    slept: list[float] = []

    async def _sleep(delay: float) -> None:
        slept.append(delay)

    await backoff_sleep(1, sleep=_sleep, jitter=_no_jitter)

    assert slept == [0.5]


# ---- Retry-After 上限。上游给的值不能无条件照收：一个 "Retry-After: 120"
# 会让用户对着输入框干等两分钟，而这条链路上没有任何 turn 级超时能兜住。 ----


def test_retry_after_is_capped_by_max_delay() -> None:
    assert backoff_delay(0, "120", max_delay=5.0, jitter=_no_jitter) == 5.0


def test_max_delay_also_caps_the_exponential_curve() -> None:
    """退避曲线本身涨到上限之上时同样要截断。"""
    assert backoff_delay(6, max_delay=5.0, jitter=_no_jitter) == 5.0


def test_max_delay_does_not_inflate_short_waits() -> None:
    """上限是天花板不是地板——正常的短退避不受影响。"""
    assert backoff_delay(0, max_delay=5.0, jitter=_no_jitter) == 0.25


def test_no_cap_by_default_preserves_original_behavior() -> None:
    """不传 max_delay 时行为与抽取前的 CoreClient 完全一致。"""
    assert backoff_delay(0, "120", jitter=_no_jitter) == 120.0


async def test_backoff_sleep_forwards_max_delay() -> None:
    slept: list[float] = []

    async def _sleep(delay: float) -> None:
        slept.append(delay)

    await backoff_sleep(0, "120", max_delay=5.0, sleep=_sleep, jitter=_no_jitter)

    assert slept == [5.0]
