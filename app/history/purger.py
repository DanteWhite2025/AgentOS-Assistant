"""历史对话保留期清理（后台常驻循环）。

循环形态照 app/strategy/sampler.py：单轮失败只记日志、不中断后续轮次；
CancelledError 重抛，容器关停才停得干净。

**这一层最危险的失效模式是静默失效**：某一轮硬失败被 except 吞成「等下一
轮」，而下一轮同样失败——保留期从此永远不生效，表无界增长，而日志里只有
几条 warning。news 侧已经踩过一次（见 app/news/pg_store.py 的 purge_before
注释：先读 id 再 IN (ids)，行数超过绑定参数上限就硬失败）。所以删除一律走
子查询谓词，与待删行数无关。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from app.services.round_lock import RoundLock, lock_ttl_for

logger = logging.getLogger(__name__)


class _Purgeable(Protocol):
    async def purge_before(self, cutoff: datetime) -> int: ...


class ConversationPurger:
    """按保留期删除过期历史对话。

    判据是 last_message_at 而非 created_at —— 很久以前开、但一直在聊的会话
    不该被清掉（存储层用例锁了这条）。
    """

    def __init__(
        self,
        store: _Purgeable,
        round_lock: RoundLock,
        *,
        retention_days: int,
        now: Callable[[], datetime],
        interval_seconds: float = 3600,
    ) -> None:
        self._store = store
        self._lock = round_lock
        self._retention = timedelta(days=retention_days)
        self._now = now
        self._interval_seconds = interval_seconds

    async def purge_once(self) -> int:
        """跑一轮清理，返回删除的会话数。没拿到轮次锁则跳过并返回 0。"""

        if not await self._lock.acquire(lock_ttl_for(self._interval_seconds)):
            # 多副本下只有一个实例该干活；其余跳过不是错误。
            return 0
        cutoff = self._now() - self._retention
        removed = await self._store.purge_before(cutoff)
        if removed:
            logger.info("历史对话清理完成 removed=%d cutoff=%s", removed, cutoff.isoformat())
        return removed

    async def run_forever(self, interval_seconds: float) -> None:
        """后台常驻循环。单轮失败只记日志，不中断后续清理。"""

        self._interval_seconds = interval_seconds
        while True:
            try:
                await self.purge_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 只记日志。但要清楚这条 warning 意味着「本轮保留期没生效」，
                # 连续出现就是表在无界增长，不是无害噪音。
                logger.warning("历史对话清理单轮失败，等待下一轮", exc_info=True)
            await asyncio.sleep(interval_seconds)
