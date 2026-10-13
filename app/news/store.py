"""NewsStore 协议、游标编解码与进程内实现（Fake / 测试用）。"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Final, Protocol

from app.news.models import NewsItem, NewsQuery

# dedup_key 合并窗口：标题会被复用（如「美联储议息决定」），只有 published_at 相差
# 不超过这个窗口才视为同一事件；窗口锚定在来入项自己的 published_at，不锚 now()，
# 保证回填/重跑历史数据时行为确定。pg_store.py 从这里导入，两处保持同一个值。
DEDUP_WINDOW: Final[timedelta] = timedelta(hours=24)


def encode_cursor(published_at: datetime, news_id: str) -> str:
    """游标 = base64(ISO 时间|news_id)。不透明化，避免调用方拿它当 offset 用。"""
    raw = f"{published_at.isoformat()}|{news_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """解游标。非法一律抛 ValueError，由路由层转 400。"""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("游标格式不合法") from exc
    when, _, news_id = raw.partition("|")
    if not news_id:
        raise ValueError("游标缺少 news_id")
    return datetime.fromisoformat(when), news_id


class NewsStore(Protocol):
    async def upsert_many(self, items: Sequence[NewsItem]) -> int: ...

    async def query(self, q: NewsQuery) -> tuple[list[NewsItem], str | None]: ...

    async def purge_before(self, cutoff: datetime) -> int: ...

    async def translated_ids(self, news_ids: Sequence[str]) -> set[str]:
        """这批 id 里哪些已经有中文标题了（`title_en` 非空）。

        存在的唯一理由是成本：trending route 每 5 分钟回同样的 50 条，
        采集侧据此跳过已翻译过的条目。少了这道闸门，同一条新闻每天要送
        288 次翻译。

        判据刻意是「有没有译文」而不是「有没有入库」：上线时表里已经躺着
        一批英文标题的存量条目，按「已入库就跳过」的话它们永远等不到翻译，
        推荐流前几页会一直是英文。
        """
        ...


_CJK_RE: Final = re.compile(r"[\u4e00-\u9fff]")


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def refreshed_row_fields(incoming: NewsItem) -> dict[str, str]:
    """命中已存在行时要写回的行内字段。两个存储实现共用这一份规则。

    - `dedup_key` 跟随新一轮
    - `title` / `title_en` 只在这一轮真的带来了译文时才写（避免把已翻译的中文冲回英文原文）
    - `content` / `content_en` 同样：如果这轮带来了正文译文（`content_en` 非空），或 incoming 本身就是中文，
      才写回；若这轮是跳过翻译的英文原件，绝不能写回覆盖已有行里的中文译文。

    tags 不在这里：它在 PG 实现里是独立的关联表，由 _merge_tags 处理。
    """
    fields = {"dedup_key": incoming.dedup_key}
    if incoming.title_en:
        fields["title"] = incoming.title
        fields["title_en"] = incoming.title_en
    if incoming.content_en:
        fields["content"] = incoming.content
        fields["content_en"] = incoming.content_en
    elif _has_cjk(incoming.content):
        fields["content"] = incoming.content
    return fields


def _matches(item: NewsItem, q: NewsQuery) -> bool:
    if q.tag is not None and q.tag not in item.tags:
        return False
    if q.since is not None and item.published_at < q.since:
        return False
    if q.q:
        needle = q.q.casefold()
        # title_en 与 content_en 也要参与：翻成中文后，用英文关键词搜索本来能搜到的
        # 新闻会全部搜不到——检索能力不该随翻译一起消失。
        haystacks = (item.title, item.title_en, item.content, item.content_en)
        if not any(needle in text.casefold() for text in haystacks):
            return False
    return True


class InMemoryNewsStore:
    """进程内实现。Fake 模式与单测用；生产一律 PostgresNewsStore。"""

    def __init__(self) -> None:
        self._items: dict[str, NewsItem] = {}

    def _find_by_dedup_key(self, dedup_key: str, published_at: datetime) -> NewsItem | None:
        return next(
            (
                item
                for item in self._items.values()
                if item.dedup_key == dedup_key
                and abs(item.published_at - published_at) <= DEDUP_WINDOW
            ),
            None,
        )

    async def upsert_many(self, items: Sequence[NewsItem]) -> int:
        written = 0
        for item in items:
            existing = self._items.get(item.news_id) or self._find_by_dedup_key(
                item.dedup_key, item.published_at
            )
            if existing is not None:
                # 整体换新，不原地改
                merged = tuple(dict.fromkeys((*existing.tags, *item.tags)))
                self._items = {
                    **self._items,
                    existing.news_id: existing.model_copy(
                        update={**refreshed_row_fields(item), "tags": merged}
                    ),
                }
                continue
            self._items = {**self._items, item.news_id: item}
            written += 1
        return written

    async def query(self, q: NewsQuery) -> tuple[list[NewsItem], str | None]:
        rows = sorted(
            (item for item in self._items.values() if _matches(item, q)),
            key=lambda item: (item.published_at, item.news_id),
            reverse=True,
        )
        if q.cursor:
            when, news_id = decode_cursor(q.cursor)
            rows = [
                item for item in rows
                if (item.published_at, item.news_id) < (when, news_id)
            ]
        page = rows[: q.limit]
        next_cursor = (
            encode_cursor(page[-1].published_at, page[-1].news_id)
            if len(rows) > q.limit and page
            else None
        )
        return page, next_cursor

    async def translated_ids(self, news_ids: Sequence[str]) -> set[str]:
        return {
            news_id
            for news_id in news_ids
            if (item := self._items.get(news_id)) is not None
            and item.title_en
            and (item.content_en or _has_cjk(item.content))
        }

    async def purge_before(self, cutoff: datetime) -> int:
        kept = {
            news_id: item
            for news_id, item in self._items.items()
            if item.published_at >= cutoff
        }
        removed = len(self._items) - len(kept)
        self._items = kept
        return removed
