"""PostgreSQL 历史对话存储（SQLAlchemy 2.x async Core）。

表结构与索引照 app/news/pg_store.py 的范式：模块级 MetaData + sa.Table +
create_schema，无 Alembic。keyset 分页写成 OR 形式而非行值比较——行值比较
在各方言下支持度不一，而本仓库的 PG store 测试全部跑 sqlite+aiosqlite。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.history.models import ConversationMessage, ConversationSummary
from app.history.store import (
    decode_cursor,
    encode_cursor,
    fallback_title,
    merged_modality,
)

logger = logging.getLogger(__name__)

metadata = sa.MetaData()

conversations_table = sa.Table(
    "agent_conversations",
    metadata,
    sa.Column("session_id", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(64), nullable=False),
    # 非空：建行时即写首句截断值，读列表无需 JOIN 消息表算降级标题。
    sa.Column("title", sa.String(200), nullable=False),
    # 本期恒为 text。列现在就建——create_all 不补列，二期语音再加就得手写
    # ALTER TABLE 并承担存量回填风险。
    sa.Column("modality", sa.String(16), nullable=False, server_default="text"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("message_count", sa.Integer, nullable=False, server_default="0"),
    sa.Index("ix_conversations_user_time", "user_id", "last_message_at", "session_id"),
    sa.Index("ix_conversations_last_activity", "last_message_at"),
)

messages_table = sa.Table(
    "agent_conversation_messages",
    metadata,
    # with_variant 不是可选项：sqlite 只有 INTEGER PRIMARY KEY 才是 rowid
    # 别名、才会自增，BIGINT 主键会直接 NOT NULL 失败。测试跑 sqlite，
    # 不加这层变体的话 PG 实现在测试里一条都写不进去。
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    sa.Column("session_id", sa.String(64), nullable=False),
    sa.Column("role", sa.String(16), nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Index("ix_messages_session_time", "session_id", "created_at", "id"),
)


def ensure_utc(value: datetime) -> datetime:
    """回读时间统一成带时区。

    PG 驱动给 tz-aware，SQLite 驱动给 naive；不归一化后续时间比较直接抛
    TypeError（与 app/news/pg_store.py、app/strategy/equity_pg_store.py 同一个坑）。
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _row_to_summary(row: Any) -> ConversationSummary:
    return ConversationSummary(
        session_id=row.session_id,
        user_id=row.user_id,
        title=row.title,
        modality=row.modality,
        created_at=ensure_utc(row.created_at),
        last_message_at=ensure_utc(row.last_message_at),
        message_count=row.message_count,
    )


def _row_to_message(row: Any) -> ConversationMessage:
    return ConversationMessage(
        id=row.id,
        session_id=row.session_id,
        role=row.role,
        content=row.content,
        created_at=ensure_utc(row.created_at),
    )


class PostgresConversationStore:
    """生产实现。库为空时由 container 回退到 InMemoryConversationStore。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_schema(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def record_turn(
        self,
        session_id: str,
        user_id: str,
        user_text: str,
        assistant_text: str,
        now: datetime,
        *,
        modality: str = "text",
    ) -> bool:
        async with self._engine.begin() as conn:
            existing = (
                await conn.execute(
                    sa.select(
                        conversations_table.c.session_id, conversations_table.c.modality
                    ).where(conversations_table.c.session_id == session_id)
                )
            ).first()
            if existing is None:
                await conn.execute(
                    conversations_table.insert().values(
                        session_id=session_id,
                        user_id=user_id,
                        title=fallback_title(user_text),
                        modality=modality,
                        created_at=now,
                        last_message_at=now,
                        message_count=2,
                    )
                )
            else:
                # 刻意不带 title：续写若跟着改标题，第二轮的首句会把已生成的
                # 语义标题冲掉，用户会看到标题莫名其妙变来变去。
                await conn.execute(
                    conversations_table.update()
                    .where(conversations_table.c.session_id == session_id)
                    .values(
                        last_message_at=now,
                        message_count=conversations_table.c.message_count + 2,
                        modality=merged_modality(existing.modality, modality),
                    )
                )
            await conn.execute(
                messages_table.insert(),
                [
                    {
                        "session_id": session_id,
                        "role": "user",
                        "content": user_text,
                        "created_at": now,
                    },
                    {
                        "session_id": session_id,
                        "role": "assistant",
                        "content": assistant_text,
                        "created_at": now,
                    },
                ],
            )
        return existing is None

    async def get_conversation(self, session_id: str) -> ConversationSummary | None:
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    sa.select(conversations_table).where(
                        conversations_table.c.session_id == session_id
                    )
                )
            ).first()
        return _row_to_summary(row) if row is not None else None

    async def list_conversations(
        self, user_id: str, limit: int, cursor: str | None
    ) -> tuple[list[ConversationSummary], str | None]:
        stmt = sa.select(conversations_table).where(conversations_table.c.user_id == user_id)
        if cursor:
            when, ident = decode_cursor(cursor)
            stmt = stmt.where(
                sa.or_(
                    conversations_table.c.last_message_at < when,
                    sa.and_(
                        conversations_table.c.last_message_at == when,
                        conversations_table.c.session_id < ident,
                    ),
                )
            )
        stmt = stmt.order_by(
            conversations_table.c.last_message_at.desc(),
            conversations_table.c.session_id.desc(),
        ).limit(limit + 1)  # 多取一条用于判断还有没有下一页

        async with self._engine.begin() as conn:
            rows = list((await conn.execute(stmt)).all())
        items = [_row_to_summary(row) for row in rows[:limit]]
        next_cursor = (
            encode_cursor(items[-1].last_message_at, items[-1].session_id)
            if len(rows) > limit and items
            else None
        )
        return items, next_cursor

    async def list_messages(
        self, session_id: str, limit: int, cursor: str | None
    ) -> tuple[list[ConversationMessage], str | None]:
        stmt = sa.select(messages_table).where(messages_table.c.session_id == session_id)
        if cursor:
            when, ident = decode_cursor(cursor)
            # 回放是正序，游标语义是「取更晚的」，与列表接口方向相反。
            stmt = stmt.where(
                sa.or_(
                    messages_table.c.created_at > when,
                    sa.and_(
                        messages_table.c.created_at == when,
                        messages_table.c.id > int(ident),
                    ),
                )
            )
        stmt = stmt.order_by(messages_table.c.created_at, messages_table.c.id).limit(limit + 1)

        async with self._engine.begin() as conn:
            rows = list((await conn.execute(stmt)).all())
        items = [_row_to_message(row) for row in rows[:limit]]
        next_cursor = (
            encode_cursor(items[-1].created_at, str(items[-1].id))
            if len(rows) > limit and items
            else None
        )
        return items, next_cursor

    async def recent_turns(self, session_id: str, max_turns: int) -> list[tuple[str, str]]:
        # 取 max_turns 的 2 倍再配对：一轮是 user + assistant 两条。多取一些
        # 容错落单消息（写入中途失败会留下没有答案的问题）。
        stmt = (
            sa.select(messages_table)
            .where(messages_table.c.session_id == session_id)
            .order_by(messages_table.c.created_at.desc(), messages_table.c.id.desc())
            .limit(max_turns * 2 + 2)
        )
        async with self._engine.begin() as conn:
            rows = list((await conn.execute(stmt)).all())
        rows.reverse()

        turns: list[tuple[str, str]] = []
        pending: str | None = None
        for row in rows:
            if row.role == "user":
                pending = row.content
            elif pending is not None:
                # 空答案不进上下文：faithful 地存下来是为了回放，但告诉模型
                # 「你上一轮沉默了」会让它下一轮跟着道歉或重复。
                if row.content.strip():
                    turns.append((pending, row.content))
                pending = None
        return turns[-max_turns:]

    async def set_title(self, session_id: str, title: str, *, only_if: str | None = None) -> None:
        stmt = conversations_table.update().where(conversations_table.c.session_id == session_id)
        if only_if is not None:
            # 比较后再写，收在一条 UPDATE 里：先 SELECT 再 UPDATE 的话，两者
            # 之间用户仍可能改名，守卫就白加了。
            stmt = stmt.where(conversations_table.c.title == only_if)
        async with self._engine.begin() as conn:
            # 会话可能已被清理，或守卫不匹配（用户改过名）。两种情况都是影响
            # 0 行，不报错。
            await conn.execute(stmt.values(title=title))

    async def delete_conversation(self, session_id: str) -> bool:
        async with self._engine.begin() as conn:
            # 先删消息：留下孤儿消息行等于攒一堆永远查不到的数据。
            await conn.execute(
                messages_table.delete().where(messages_table.c.session_id == session_id)
            )
            result = await conn.execute(
                conversations_table.delete().where(conversations_table.c.session_id == session_id)
            )
        return bool(result.rowcount)

    async def has_message(
        self, session_id: str, role: str, content: str, created_at: datetime
    ) -> bool:
        stmt = (
            sa.select(messages_table.c.id)
            .where(
                messages_table.c.session_id == session_id,
                messages_table.c.role == role,
                messages_table.c.content == content,
                messages_table.c.created_at == created_at,
            )
            .limit(1)
        )
        async with self._engine.begin() as conn:
            return (await conn.execute(stmt)).first() is not None

    async def purge_before(self, cutoff: datetime) -> int:
        """按谓词直接删，不走 id 列表 + IN (...)。

        单条语句的绑定参数有上限（PG 65535，sqlite 更低）。稳态每轮只删几条
        所以看着安全，但保留期一旦从大调小、或服务中断后恢复，待删行数会远超
        上限直接硬失败，再被清理循环的 except 吞成「等下一轮」——保留期从此
        静默失效，表无界增长。子查询谓词删除与待删行数无关。
        """
        async with self._engine.begin() as conn:
            # 先删消息：留下孤儿消息行等于攒了一堆永远查不到的数据。
            doomed = sa.select(conversations_table.c.session_id).where(
                conversations_table.c.last_message_at < cutoff
            )
            await conn.execute(
                messages_table.delete().where(messages_table.c.session_id.in_(doomed))
            )
            result = await conn.execute(
                conversations_table.delete().where(conversations_table.c.last_message_at < cutoff)
            )
        return result.rowcount or 0
