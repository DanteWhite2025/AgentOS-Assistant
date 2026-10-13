"""限流与监控轮次锁测试（fakeredis，无需真实环境）。"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.rate_limit import InMemoryRateLimiter, RedisRateLimiter
from app.services.round_lock import (
    RedisRoundLock,
    SingleProcessRoundLock,
    lock_ttl_for,
)


def _fake_redis() -> Any:
    from fakeredis import aioredis as fake_aioredis

    return fake_aioredis.FakeRedis()


@pytest.fixture(params=["memory", "redis"])
def limiter(request: pytest.FixtureRequest) -> Any:
    if request.param == "memory":
        return InMemoryRateLimiter()
    return RedisRateLimiter(_fake_redis())


async def test_allows_up_to_limit_then_blocks(limiter: Any) -> None:
    verdicts = [await limiter.allow("k", 3, 60) for _ in range(5)]
    assert verdicts == [True, True, True, False, False]


async def test_keys_have_independent_quota(limiter: Any) -> None:
    assert await limiter.allow("a", 1, 60) is True
    assert await limiter.allow("a", 1, 60) is False
    assert await limiter.allow("b", 1, 60) is True


@pytest.mark.parametrize("limit", [0, -1])
async def test_non_positive_limit_disables_limiting(limiter: Any, limit: int) -> None:
    assert all([await limiter.allow("k", limit, 60) for _ in range(10)])


async def test_redis_limiter_sets_window_expiry() -> None:
    """必须给计数 key 设 TTL，否则窗口永不重置、额度用完再不恢复。"""
    redis = _fake_redis()
    limiter = RedisRateLimiter(redis)
    await limiter.allow("k", 5, 77)
    keys = [k.decode() if isinstance(k, bytes) else k for k in await redis.keys("grok:rl:*")]
    assert len(keys) == 1
    assert 0 < await redis.ttl(keys[0]) <= 77


async def test_redis_limiter_fails_open() -> None:
    """Redis 挂了宁可放行也不把接口一起挂掉（成本上限暂时失守，但有告警）。"""

    class _Broken:
        async def incr(self, key: str) -> int:
            raise ConnectionError("redis down")

    assert await RedisRateLimiter(_Broken()).allow("k", 1, 60) is True


async def test_memory_limiter_keeps_multiple_keys_in_same_window() -> None:
    """全局配额与单调用方配额是两个 key，同窗口内不能互相把计数冲掉。"""
    limiter = InMemoryRateLimiter()
    for _ in range(2):
        assert await limiter.allow("global", 2, 60) is True
        assert await limiter.allow("caller", 2, 60) is True
    # 两个 key 各自都已用满 2 次
    assert await limiter.allow("global", 2, 60) is False
    assert await limiter.allow("caller", 2, 60) is False


async def test_memory_limiter_drops_expired_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """窗口滚动后历史计数被丢弃，长跑进程不会无界增长，额度也会恢复。"""
    limiter = InMemoryRateLimiter()
    monkeypatch.setattr("app.services.rate_limit.time.time", lambda: 1000.0)
    for index in range(20):
        await limiter.allow(f"key-{index}", 1, 60)
    assert len(limiter._counters) == 20
    assert await limiter.allow("key-0", 1, 60) is False  # 当前窗口已用尽

    monkeypatch.setattr("app.services.rate_limit.time.time", lambda: 1000.0 + 60)
    assert await limiter.allow("key-0", 1, 60) is True  # 新窗口额度恢复
    assert len(limiter._counters) == 1  # 历史窗口的 20 个 key 被清掉


# ---- 监控轮次锁 ----


def test_lock_ttl_leaves_margin_below_interval() -> None:
    """TTL 必须小于间隔：否则下一轮开始时锁还没过期，监控被自己饿死。"""
    assert lock_ttl_for(900) == 870
    assert lock_ttl_for(60) == 30
    assert lock_ttl_for(10) == 10  # 下限保护，不会变成 0 或负数


async def test_single_process_lock_always_acquires() -> None:
    lock = SingleProcessRoundLock()
    assert await lock.acquire(60) is True
    assert await lock.acquire(60) is True


async def test_redis_lock_only_one_holder_per_round() -> None:
    """模拟多 worker：同一轮里只有一个进程能抢到，其余跳过抓取。"""
    redis = _fake_redis()
    workers = [RedisRoundLock(redis) for _ in range(4)]
    verdicts = [await worker.acquire(60) for worker in workers]
    assert verdicts.count(True) == 1
    assert verdicts.count(False) == 3


async def test_redis_lock_reacquirable_after_expiry() -> None:
    redis = _fake_redis()
    lock = RedisRoundLock(redis)
    assert await lock.acquire(60) is True
    await redis.delete("grok:monitor:round")  # 等价于 TTL 到期
    assert await lock.acquire(60) is True


async def test_redis_lock_fails_open() -> None:
    """Redis 不可用时放行：最坏成本翻倍，但热点数据不会整体停更。"""

    class _Broken:
        async def set(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("redis down")

    assert await RedisRoundLock(_Broken()).acquire(60) is True
