"""固定窗口限流。进程内 / Redis 双实现。

只给「高开销端点」用：一次 x_search 实时抓取 60~95s 且按 token 计费，裸暴露
等于给外部一个烧钱开关。多 worker 下必须走 Redis，否则每个进程各放一份额度。

刻意做成固定窗口而不是滑动窗口：实现只有几行、语义好解释，用来兜成本上限足够。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class RateLimiter(Protocol):
    async def allow(self, key: str, limit: int, window_seconds: int) -> bool: ...


class InMemoryRateLimiter:
    """单进程固定窗口计数。"""

    def __init__(self) -> None:
        self._counters: dict[tuple[str, int], int] = {}

    async def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        if limit <= 0:  # 0 或负数视为不限流
            return True
        bucket = int(time.time()) // window_seconds
        slot = (key, bucket)
        # 只丢弃历史窗口的计数：当前窗口内的其他 key 必须留着，
        # 否则「全局配额」和「单调用方配额」两个 key 会互相把计数冲掉。
        current = {slot_: count for slot_, count in self._counters.items() if slot_[1] == bucket}
        count = current.get(slot, 0) + 1
        self._counters = {**current, slot: count}
        return count <= limit


class RedisRateLimiter:
    """Redis INCR + EXPIRE 固定窗口，额度跨 worker 共享。"""

    def __init__(self, redis: Redis, prefix: str = "grok:rl:") -> None:
        self._redis = redis
        self._prefix = prefix

    async def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        if limit <= 0:
            return True
        bucket = int(time.time()) // window_seconds
        redis_key = f"{self._prefix}{key}:{bucket}"
        try:
            count = await self._redis.incr(redis_key)
            if count == 1:
                await self._redis.expire(redis_key, window_seconds)
        except Exception:
            # Redis 挂了不该把接口一起挂掉；放行并告警（成本上限暂时失守，可观测）
            logger.warning("限流计数失败，本次按放行处理 key=%s", key, exc_info=True)
            return True
        return int(count) <= limit
