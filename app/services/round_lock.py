"""监控轮次锁 —— 保证一个间隔内全集群只拉一次 xAI。

多 worker / 多副本下每个进程都跑 GrokMonitor，不加锁就是每轮 N 次计费调用。
用 Redis `SET NX EX` 做「每轮领导者选举」：抢到锁的那个进程负责本轮抓取，其余
直接跳过。锁 TTL 略小于间隔，保证下一轮开始时锁已过期、不会把监控饿死。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# 锁 TTL 相对间隔留出的余量：太小会两个 worker 撞进同一轮，太大会饿死下一轮
_TTL_MARGIN_SECONDS = 30
_MIN_TTL_SECONDS = 10


def lock_ttl_for(interval_seconds: float) -> int:
    """按间隔算锁 TTL：interval - 30s，下限 10s。"""
    return max(_MIN_TTL_SECONDS, int(interval_seconds) - _TTL_MARGIN_SECONDS)


class RoundLock(Protocol):
    async def acquire(self, ttl_seconds: int) -> bool: ...


class SingleProcessRoundLock:
    """单进程：永远拿得到锁。没配 Redis 时用，行为与加锁前完全一致。"""

    async def acquire(self, ttl_seconds: int) -> bool:
        return True


class RedisRoundLock:
    """Redis SET NX EX。抢到返回 True，已被别的 worker 持有返回 False。"""

    def __init__(self, redis: Redis, key: str = "grok:monitor:round") -> None:
        self._redis = redis
        self._key = key

    async def acquire(self, ttl_seconds: int) -> bool:
        try:
            acquired = await self._redis.set(self._key, "1", ex=ttl_seconds, nx=True)
        except Exception:
            # Redis 不可用时宁可放行：监控继续跑（最坏成本翻倍），
            # 而不是让热点数据整体停更。
            logger.warning("获取监控轮次锁失败，本轮按放行处理", exc_info=True)
            return True
        return bool(acquired)
