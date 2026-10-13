"""PostgreSQL 新闻存储（SQLAlchemy 2.x async Core）。

tags 走关联表而非 JSONB 数组：仓库的 PG store 测试全部基于
sqlite+aiosqlite，用 `tags @> '["crypto"]'` 这种 PG 专有语法会让核心过滤
逻辑脱离测试范式。关联表下过滤是普通 JOIN + WHERE，两边都能跑。

pg_trgm 索引与扩展只在 PostgreSQL 上建（sqlite 不认 gin_trgm_ops），
所以 create_schema 里按 dialect 分支。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.news.models import NewsItem, NewsQuery, NewsTag
from app.news.store import (
    DEDUP_WINDOW,
    _has_cjk,
    decode_cursor,
    encode_cursor,
    refreshed_row_fields,
)

logger = logging.getLogger(__name__)

metadata = sa.MetaData()

news_table = sa.Table(
    "news",
    metadata,
    sa.Column("news_id", sa.String(64), primary_key=True),
    sa.Column("dedup_key", sa.Text, nullable=False),
    sa.Column("source", sa.String(32), nullable=False),
    sa.Column("source_name", sa.String(128), nullable=False, server_default=""),
    sa.Column("source_url", sa.Text, nullable=False, server_default=""),
    sa.Column("source_quality", sa.String(32), nullable=False, server_default=""),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("title_en", sa.Text, nullable=False, server_default=""),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("content_en", sa.Text, nullable=False, server_default=""),
    sa.Column("lang", sa.String(16), nullable=False, server_default=""),
    sa.Column("asset_type", sa.String(16), nullable=False, server_default=""),
    sa.Column("upstream_category", sa.String(64), nullable=False, server_default=""),
    sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("raw", sa.JSON, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_news_published", "published_at", "news_id"),
    sa.Index("ix_news_dedup", "dedup_key", "published_at"),
)

news_tags_table = sa.Table(
    "news_tags",
    metadata,
    sa.Column("news_id", sa.String(64), primary_key=True),
    sa.Column("tag", sa.String(32), primary_key=True),
    sa.Index("ix_news_tags_tag", "tag", "news_id"),
)

# ILIKE 里 % 与 _ 是通配符，用户输入必须转义，否则 q=% 会捞出全表
_LIKE_ESCAPE = "\\"


def _escape_like(value: str) -> str:
    escaped = value.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
    for wildcard in ("%", "_"):
        escaped = escaped.replace(wildcard, f"{_LIKE_ESCAPE}{wildcard}")
    return escaped


def ensure_utc(value: datetime) -> datetime:
    """回读时间统一成带时区。

    PG 驱动给 tz-aware，SQLite 驱动给 naive；不归一化后续时间比较直接抛
    TypeError（与 app/strategy/equity_pg_store.py 同一个坑）。
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _row_to_item(row: Any, tags: tuple[NewsTag, ...]) -> NewsItem:
    raw = row.raw
    if isinstance(raw, str):  # SQLite 把 JSON 列回读成字符串
        raw = json.loads(raw)
    return NewsItem(
        news_id=row.news_id,
        dedup_key=row.dedup_key,
        source=row.source,
        title=row.title,
        title_en=row.title_en,
        content=row.content,
        content_en=getattr(row, "content_en", "") or "",
        published_at=ensure_utc(row.published_at),
        source_name=row.source_name,
        source_url=row.source_url,
        source_quality=row.source_quality,
        lang=row.lang,
        asset_type=row.asset_type,
        upstream_category=row.upstream_category,
        tags=tags,
        raw=raw or {},
    )


class PostgresNewsStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_schema(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            if conn.dialect.name != "postgresql":
                return
            # create_all 只建缺失的**表**，已存在的表不会补列——新加的
            # title_en 在生产库上必须靠 ALTER 才会出现，否则每次写入都报
            # UndefinedColumn。IF NOT EXISTS 让这句可重复执行（每次启动都跑）。
            await conn.execute(
                sa.text("ALTER TABLE news ADD COLUMN IF NOT EXISTS title_en TEXT NOT NULL DEFAULT ''")
            )
            await conn.execute(
                sa.text("ALTER TABLE news ADD COLUMN IF NOT EXISTS content_en TEXT NOT NULL DEFAULT ''")
            )
            # 关键词检索靠 trgm 索引。注意 2 字中文关键词提取不出完整 trigram，
            # 仍会退化为顺序扫描 —— 由 /news 的默认时间窗兜住扫描范围。
            await conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            for column in ("title", "title_en", "content", "content_en"):
                await conn.execute(
                    sa.text(
                        f"CREATE INDEX IF NOT EXISTS ix_news_{column}_trgm "
                        f"ON news USING gin ({column} gin_trgm_ops)"
                    )
                )

    async def upsert_many(self, items: Sequence[NewsItem]) -> int:
        """写入并合并跨 route 重复。

        采集由 RoundLock 保证单写者，所以 SELECT-then-INSERT 是安全的，
        也就不必用 PG 专有的 on_conflict_do_nothing（那会破坏 sqlite 测试）。
        """
        if not items:
            return 0
        written = 0
        now = datetime.now(UTC)
        async with self._engine.begin() as conn:
            for item in items:
                target_id = await self._resolve_target(conn, item)
                if target_id is None:
                    await conn.execute(
                        news_table.insert().values(
                            news_id=item.news_id,
                            dedup_key=item.dedup_key,
                            source=item.source,
                            source_name=item.source_name,
                            source_url=item.source_url,
                            source_quality=item.source_quality,
                            title=item.title,
                            title_en=item.title_en,
                            content=item.content,
                            content_en=item.content_en,
                            lang=item.lang,
                            asset_type=item.asset_type,
                            upstream_category=item.upstream_category,
                            published_at=item.published_at,
                            raw=item.raw,
                            created_at=now,
                        )
                    )
                    target_id = item.news_id
                    written += 1
                else:
                    await self._refresh(conn, target_id, item)
                await self._merge_tags(conn, target_id, item.tags)
        return written

    async def _refresh(self, conn: Any, news_id: str, item: NewsItem) -> None:
        """把已存在行的正文与（如果这轮翻出来了）标题写回。

        写回规则与 InMemory 实现共用 refreshed_row_fields，两边不能分叉。
        tags 由 _merge_tags 单独处理（关联表，不在本行里）。
        """
        await conn.execute(
            news_table.update()
            .where(news_table.c.news_id == news_id)
            .values(**refreshed_row_fields(item))
        )

    async def _resolve_target(self, conn: Any, item: NewsItem) -> str | None:
        """返回已存在的目标行 id；None 表示需要新建。

        按 news_id 命中的路径不需要时间窗（同 news_id 永远是同一条新闻）；
        按 dedup_key 命中的路径必须限定在 DEDUP_WINDOW 内，否则标题被复用的
        新闻（如「美联储议息决定」）跨事件也会被误合并。
        """
        by_id = await conn.execute(
            sa.select(news_table.c.news_id).where(news_table.c.news_id == item.news_id)
        )
        existing = by_id.scalar_one_or_none()
        if existing is not None:
            return existing
        lower = item.published_at - DEDUP_WINDOW
        upper = item.published_at + DEDUP_WINDOW
        by_key = await conn.execute(
            sa.select(news_table.c.news_id, news_table.c.published_at)
            .where(
                news_table.c.dedup_key == item.dedup_key,
                news_table.c.published_at >= lower,
                news_table.c.published_at <= upper,
            )
            .order_by(news_table.c.published_at.asc())
            .limit(1)
        )
        row = by_key.first()
        if row is None:
            return None
        # 双重确认：回读时间走 ensure_utc 再核对真实差值，避免不同后端在
        # SQL 层比较 tz-aware/naive 时的边界语义差异导致误合并。
        if abs(ensure_utc(row.published_at) - item.published_at) > DEDUP_WINDOW:
            return None
        return row.news_id

    async def translated_ids(self, news_ids: Sequence[str]) -> set[str]:
        if not news_ids:
            return set()
        async with self._engine.begin() as conn:
            rows = await conn.execute(
                sa.select(
                    news_table.c.news_id,
                    news_table.c.title_en,
                    news_table.c.content,
                    news_table.c.content_en,
                ).where(
                    news_table.c.news_id.in_(list(news_ids)),
                    news_table.c.title_en != "",
                )
            )
            return {
                row[0]
                for row in rows
                if row[3] != "" or _has_cjk(row[2])
            }

    async def _merge_tags(
        self, conn: Any, news_id: str, tags: tuple[NewsTag, ...]
    ) -> None:
        if not tags:
            return
        existing = await conn.execute(
            sa.select(news_tags_table.c.tag).where(news_tags_table.c.news_id == news_id)
        )
        already = {row[0] for row in existing}
        fresh = [{"news_id": news_id, "tag": tag} for tag in tags if tag not in already]
        if fresh:
            await conn.execute(news_tags_table.insert(), fresh)

    async def query(self, q: NewsQuery) -> tuple[list[NewsItem], str | None]:
        stmt = sa.select(news_table)
        if q.tag is not None:
            stmt = stmt.join(
                news_tags_table,
                sa.and_(
                    news_tags_table.c.news_id == news_table.c.news_id,
                    news_tags_table.c.tag == q.tag,
                ),
            )
        if q.since is not None:
            stmt = stmt.where(news_table.c.published_at >= q.since)
        if q.q:
            pattern = f"%{_escape_like(q.q)}%"
            # title_en 一起搜：标题翻成中文后，英文关键词本来能命中的新闻
            # 会全部搜不到——检索能力不该随翻译一起消失。
            stmt = stmt.where(
                sa.or_(
                    news_table.c.title.ilike(pattern, escape=_LIKE_ESCAPE),
                    news_table.c.title_en.ilike(pattern, escape=_LIKE_ESCAPE),
                    news_table.c.content.ilike(pattern, escape=_LIKE_ESCAPE),
                    news_table.c.content_en.ilike(pattern, escape=_LIKE_ESCAPE),
                )
            )
        if q.cursor:
            when, news_id = decode_cursor(q.cursor)
            # 写成 OR 形式而不是行值比较：行值比较在各方言下支持度不一
            stmt = stmt.where(
                sa.or_(
                    news_table.c.published_at < when,
                    sa.and_(
                        news_table.c.published_at == when,
                        news_table.c.news_id < news_id,
                    ),
                )
            )
        stmt = stmt.order_by(
            news_table.c.published_at.desc(), news_table.c.news_id.desc()
        ).limit(q.limit + 1)  # 多取一条用于判断还有没有下一页

        async with self._engine.begin() as conn:
            rows = list((await conn.execute(stmt)).all())
            page_rows = rows[: q.limit]
            tag_map = await self._tags_for(conn, [row.news_id for row in page_rows])

        items = [_row_to_item(row, tag_map.get(row.news_id, ())) for row in page_rows]
        next_cursor = (
            encode_cursor(items[-1].published_at, items[-1].news_id)
            if len(rows) > q.limit and items
            else None
        )
        return items, next_cursor

    async def _tags_for(
        self, conn: Any, news_ids: Sequence[str]
    ) -> dict[str, tuple[NewsTag, ...]]:
        if not news_ids:
            return {}
        result = await conn.execute(
            sa.select(news_tags_table.c.news_id, news_tags_table.c.tag)
            .where(news_tags_table.c.news_id.in_(list(news_ids)))
            .order_by(news_tags_table.c.tag)
        )
        grouped: dict[str, tuple[NewsTag, ...]] = {}
        for news_id, tag in result:
            grouped[news_id] = (*grouped.get(news_id, ()), tag)
        return grouped

    async def purge_before(self, cutoff: datetime) -> int:
        """按谓词直接删，不走 id 列表 + IN (...)。

        旧实现先把过期 id 全读进内存再 `IN (ids)`：PG 单条语句绑定参数上限
        65535，sqlite 同理更低。稳态每轮只删几条所以安全，但只要保留期从大
        调小、或采集中断后恢复，下一轮要删的行数会远超上限，直接硬失败，
        被 collector.py 的 except 吞成「清理失败，等待下一轮」——保留期从此
        静默失效，表无界增长。改成关联子查询 + 谓词删除，绑定参数数量与
        待删行数无关。
        """
        async with self._engine.begin() as conn:
            # 先删关联表：留下孤儿 tag 行会让 tag 过滤 JOIN 到已删的新闻
            doomed_ids = sa.select(news_table.c.news_id).where(
                news_table.c.published_at < cutoff
            )
            await conn.execute(
                news_tags_table.delete().where(news_tags_table.c.news_id.in_(doomed_ids))
            )
            result = await conn.execute(
                news_table.delete().where(news_table.c.published_at < cutoff)
            )
        return result.rowcount or 0
