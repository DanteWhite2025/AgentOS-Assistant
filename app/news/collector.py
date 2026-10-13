"""新闻采集循环。

上游没有分页（limit 上限 50，实测 media 源 2 条/分 → 50 条约 25 分钟耗尽），
所以间隔必须远小于 25 分钟；默认 300s 留 5x 余量。同理**历史无法回填** ——
首轮只能拿到最近约 25 分钟的内容，靠 warmup() 的带 query 检索补一点纵深。

单个 route 失败只跳过它自己：一个源挂了不该让其余源的数据跟着停更
（沿用原 grok_monitor.refresh_once 的做法，那个模式本身是对的）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol

from app.news.models import NewsItem
from app.news.normalizer import rows_from, to_news_item
from app.news.store import NewsStore
from app.news.title_translator import needs_zh_translation
from app.services.round_lock import RoundLock, SingleProcessRoundLock, lock_ttl_for

logger = logging.getLogger(__name__)

# 三条 route。trending 走空 query（上游据此返回它自己的热榜，供 recommend tag）。
ROUTES: Final[tuple[tuple[str, dict[str, Any]], ...]] = (
    ("media", {"sources": ["media"]}),
    ("research", {"sources": ["research"]}),
    ("trending", {}),
)

# 冷启动预热用的检索词。search route 有 relevance 排序，能捞到比时间窗更早的内容。
WARMUP_QUERIES: Final[tuple[str, ...]] = (
    "Fed CPI interest rate",
    "AI chips NVDA OpenAI",
    "bitcoin ethereum ETF",
    "US stocks earnings guidance",
    "Gold XAU Fed rate cut",
    "Bitcoin Ethereum ETF inflow",
    "NVIDIA AI US stocks earnings",
)

# 上游 news 工具的 limit 硬约束：[1, 50]。超限在构造时直接拒绝，不做静默截断
# —— 截断会掩盖配置错误（配了 200 却以为生效了，实际悄悄跑成 50）。
_LIMIT_MIN: Final = 1
_LIMIT_MAX: Final = 50

# 预热是一次性操作，不是周期性轮次，不适用 lock_ttl_for（那是给 run_forever
# 按间隔算 TTL 的）。这里给一个足够盖住 WARMUP_QUERIES × 2 语言串行调用的固定
# TTL（当前 7 组查询 × 2 = 14 次调用；原 4 组查询时是 300 秒，按比例上调）。
_WARMUP_LOCK_TTL_SECONDS: Final = 600


class _NewsFetcher(Protocol):
    async def fetch_news(self, **arguments: Any) -> dict[str, Any]: ...


class _TitleTranslator(Protocol):
    async def translate(self, titles: Sequence[str]) -> dict[str, str]: ...


class NewsCollector:
    def __init__(
        self,
        client: _NewsFetcher,
        store: NewsStore,
        *,
        limit: int = 50,
        round_lock: RoundLock | None = None,
        retention_days: int = 90,
        translator: _TitleTranslator | None = None,
    ) -> None:
        if not (_LIMIT_MIN <= limit <= _LIMIT_MAX):
            raise ValueError(
                f"limit 必须在 [{_LIMIT_MIN}, {_LIMIT_MAX}] 区间内"
                f"（上游 news 工具硬上限 {_LIMIT_MAX}），收到 {limit}"
            )
        self.client = client
        self.store = store
        self.limit = limit
        self.round_lock: RoundLock = round_lock or SingleProcessRoundLock()
        self.retention_days = retention_days
        # None = 不翻译（未配置 LLM 的 fake 模式）。翻译是增强项，缺了照常采集。
        self.translator = translator

    async def _collect(self, source: str, arguments: dict[str, Any]) -> int:
        payload = await self.client.fetch_news(**{**arguments, "limit": self.limit})
        items: list[NewsItem] = []
        for row in rows_from(payload):
            item = to_news_item(row, source=source)
            if item is not None:
                items.append(item)
        items = await self._with_zh_titles(items)
        written = await self.store.upsert_many(items)
        logger.debug("news 采集 route=%s 解析=%d 新增=%d", source, len(items), written)
        return written

    async def _with_zh_titles(self, items: list[NewsItem]) -> list[NewsItem]:
        """把英文标题与正文换成中文，原文分别存进 title_en 与 content_en。

        顺序上必须在 upsert 之前：进了库再翻就得额外读一遍、改一遍，
        而这里本来就握着整批对象。

        翻译失败的条目原样返回，照常入库。
        """
        if self.translator is None:
            return items
        candidates = [
            item
            for item in items
            if needs_zh_translation(title=item.title, content=item.content)
        ]
        if not candidates:
            return items
        # 已经有译文的不再送翻译。trending route 每轮回同样的 50 条，
        # 少了这道闸门，同一条新闻一天要翻 288 次。
        done = await self.store.translated_ids([item.news_id for item in candidates])
        pending = [item for item in candidates if item.news_id not in done]
        if not pending:
            return items
        try:
            if hasattr(self.translator, "translate_items"):
                zh_map = await self.translator.translate_items(pending)
            else:
                zh_titles = await self.translator.translate([item.title for item in pending])
                zh_map = {
                    item.news_id: (zh_titles[item.title], "")
                    for item in pending
                    if item.title in zh_titles
                }
        except asyncio.CancelledError:
            raise
        except Exception:
            # 翻译层自己已经兜过一次底，这里是第二道：它若真抛出来，
            # 本轮采集也不能跟着失败。
            logger.warning("news 内容翻译异常，本轮保留原文", exc_info=True)
            return items
        if not zh_map:
            return items
        renamed: dict[str, NewsItem] = {}
        for item in pending:
            if item.news_id not in zh_map:
                continue
            zh_title, zh_summary = zh_map[item.news_id]
            updates: dict[str, Any] = {}
            if zh_title and zh_title != item.title:
                updates["title"] = zh_title
                updates["title_en"] = item.title
            if zh_summary and zh_summary != item.content:
                updates["content"] = zh_summary
                updates["content_en"] = item.content
            if updates:
                renamed[item.news_id] = item.model_copy(update=updates)
        logger.info("news 中文化翻译 待翻=%d 成功=%d", len(pending), len(renamed))
        return [renamed.get(item.news_id, item) for item in items]

    async def refresh_once(self) -> int:
        """跑一轮全部 route，返回新增条数。单 route 失败只跳过它自己。

        单 route 失败只记 warning——不该因为一个源挂了就把日志刷成 error。
        但三条 route 全灭是另一回事：意味着新闻数据本轮完全没有更新，且
        大概率下一轮还是全灭（真机事故正是这样连续静默了一周才被发现）。
        这个信号必须单独提级为 error，不能淹没在三条 warning 里。
        """
        written = 0
        failed_routes: list[str] = []
        for source, arguments in ROUTES:
            try:
                written += await self._collect(source, arguments)
            except asyncio.CancelledError:
                raise
            except Exception:
                failed_routes.append(source)
                logger.warning("news 采集 route=%s 本轮失败，继续下一个", source, exc_info=True)
        if failed_routes and len(failed_routes) == len(ROUTES):
            logger.error(
                "news 采集本轮全部 %d 个 route 失败（%s），新闻数据本轮未更新",
                len(ROUTES), ", ".join(failed_routes),
            )
        await self._purge_expired()
        logger.info("news 采集本轮新增 %d 条", written)
        return written

    async def _purge_expired(self) -> None:
        if self.retention_days <= 0:
            return
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        try:
            removed = await self.store.purge_before(cutoff)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("news 过期清理失败，等待下一轮", exc_info=True)
            return
        if removed:
            logger.info("news 清理过期数据 %d 条（保留 %d 天）", removed, self.retention_days)

    async def warmup(self) -> int:
        """一次性冷启动预热。上游无历史回填，这里靠 relevance 排序多捞一点。

        也要过 round_lock：refresh_once 抢锁，warmup 此前没有——拒锁时仍会
        发出 14 次上游调用（WARMUP_QUERIES × 2 语言），N 个副本部署 = 14N
        次调用；滚动重启期间若 AGENT_NEWS_WARMUP_ON_START 仍是 true，每个新副本都会再触发一轮，
        叠上未澄清的上游配额，首次部署就可能打爆配额。
        """
        if not await self.round_lock.acquire(_WARMUP_LOCK_TTL_SECONDS):
            logger.info("news 预热本轮锁被其他实例持有，跳过")
            return 0
        written = 0
        for query in WARMUP_QUERIES:
            for source_lang in ("zh-cn", "en"):
                try:
                    written += await self._collect(
                        "media", {"query": query, "source_lang": source_lang}
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("news 预热 query=%s 失败，跳过", query, exc_info=True)
        logger.info("news 冷启动预热新增 %d 条", written)
        return written

    async def run_forever(self, interval_seconds: float) -> None:
        """常驻循环。单轮失败只记日志，不中断后续轮次。"""
        ttl = lock_ttl_for(interval_seconds)
        logger.info(
            "news 采集已启动，间隔 %.0fs，每轮 %d 次上游调用",
            interval_seconds,
            len(ROUTES),
        )
        while True:
            try:
                if await self.round_lock.acquire(ttl):
                    await self.refresh_once()
                else:
                    logger.debug("news 采集本轮锁被其他实例持有，跳过")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("news 采集单轮失败，等待下一轮", exc_info=True)
            await asyncio.sleep(interval_seconds)
