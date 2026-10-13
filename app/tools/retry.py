"""退避重试的共享实现。

这套语义（指数退避 + 抖动 + 认 Retry-After）原本只长在 `CoreClient` 内部的
私有方法里。Tavily 需要一模一样的行为，抽出来复用而不是复制一份——两处各自
演化的重试策略最后一定会漂移，而重试参数是出问题时最难排查的那类配置。

抖动的作用是打散并发客户端的重试时刻：一批请求同时被 429 打回，如果都按同一
条退避曲线重试，第二波会再次同时到达，把上游按在地上。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

Sleep = Callable[[float], Awaitable[None]]
Jitter = Callable[[float, float], float]

# 第 n 次重试前等 0.25 * 2**n 秒：0.25 / 0.5 / 1.0 …
_BASE_DELAY_SECONDS = 0.25
# 抖动上限，同时也是抖动占退避时长的比例上限，取两者较小值。
_MAX_JITTER_SECONDS = 0.25


def retry_after_seconds(value: str | None) -> float | None:
    """解析 Retry-After 响应头。

    RFC 9110 允许两种写法：延迟秒数，或一个 HTTP-date。无法解析时返回 None
    交给调用方用默认退避，绝不因为一个畸形响应头就抛异常打断重试。
    """

    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def backoff_delay(
    attempt: int,
    retry_after: str | None = None,
    *,
    max_delay: float | None = None,
    jitter: Jitter = random.uniform,
) -> float:
    """算出第 ``attempt`` 次重试前该等多久（attempt 从 0 开始）。

    上游明确给了 Retry-After 时以它为准（取它与默认退避的较大值）——那是上游
    在告诉我们它需要多少恢复时间，比我们自己猜的曲线更准。

    但在用户正在等回答的交互链路上要给它封顶：一个 ``Retry-After: 120`` 会让
    人对着输入框干等两分钟，而这条链路上没有任何 turn 级超时能兜住。调用方用
    ``max_delay`` 声明自己能接受的最长等待；不传则不封顶（与抽取前的
    CoreClient 行为一致）。
    """

    base_delay = _BASE_DELAY_SECONDS * (2**attempt)
    delay = max(base_delay, retry_after_seconds(retry_after) or 0.0)
    if max_delay is not None:
        delay = min(delay, max_delay)
    return delay + jitter(0.0, min(_MAX_JITTER_SECONDS, delay * 0.25))


async def backoff_sleep(
    attempt: int,
    retry_after: str | None = None,
    *,
    max_delay: float | None = None,
    sleep: Sleep = asyncio.sleep,
    jitter: Jitter = random.uniform,
) -> None:
    """按退避曲线等待。``sleep``/``jitter`` 可注入，让测试不必真的睡。"""

    await sleep(backoff_delay(attempt, retry_after, max_delay=max_delay, jitter=jitter))
