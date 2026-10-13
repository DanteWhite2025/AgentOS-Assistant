"""PostgreSQL 长期记忆存储（SQLAlchemy 2.x async Core）。

配置 AGENT_DATABASE_URL 即自动启用，持久化用户画像/偏好与情景记忆。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from app.memory.store import EpisodeItem, MemoryItem

metadata = sa.MetaData()

# 在 PostgreSQL 下使用 JSONB 支持高效索引与查询，在 SQLite 下回退为 JSON
json_type = sa.JSON().with_variant(JSONB, "postgresql")

user_memories_table = sa.Table(
    "user_memories",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(64), nullable=False, index=True),
    sa.Column("category", sa.String(32), nullable=False, index=True),
    sa.Column("key", sa.String(64), nullable=False),
    sa.Column("content", json_type, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint("user_id", "category", "key", name="uq_user_memory_key"),
)

memory_episodes_table = sa.Table(
    "memory_episodes",
    metadata,
    sa.Column("episode_id", sa.String(64), primary_key=True),
    sa.Column("user_id", sa.String(64), nullable=False, index=True),
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("details", json_type, nullable=True),
    sa.Column("metadata_json", json_type, nullable=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
)


class PostgresMemoryStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_schema(self) -> None:
        """建表（MVP/测试环境；生产推荐 Alembic 迁移）。"""
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def remember_profile(
        self,
        user_id: str,
        category: str,
        key: str,
        content: dict[str, Any] | Any,
    ) -> MemoryItem:
        now = datetime.now(UTC)
        wrapped_content = content if isinstance(content, dict) else {"value": content}
        new_id = uuid.uuid4().hex

        # Postgres UPSERT 支持
        dialect_name = self._engine.dialect.name
        async with self._engine.begin() as conn:
            if dialect_name == "postgresql":
                stmt = pg_insert(user_memories_table).values(
                    id=new_id,
                    user_id=user_id,
                    category=category,
                    key=key,
                    content=wrapped_content,
                    created_at=now,
                    updated_at=now,
                )
                upsert_stmt = stmt.on_conflict_do_update(
                    constraint="uq_user_memory_key",
                    set_={
                        "content": wrapped_content,
                        "updated_at": now,
                    },
                )
                await conn.execute(upsert_stmt)
            else:
                # SQLite / 通用回退逻辑
                sel = sa.select(user_memories_table).where(
                    user_memories_table.c.user_id == user_id,
                    user_memories_table.c.category == category,
                    user_memories_table.c.key == key,
                )
                row = (await conn.execute(sel)).fetchone()
                if row:
                    upd = (
                        sa.update(user_memories_table)
                        .where(
                            user_memories_table.c.user_id == user_id,
                            user_memories_table.c.category == category,
                            user_memories_table.c.key == key,
                        )
                        .values(content=wrapped_content, updated_at=now)
                    )
                    await conn.execute(upd)
                else:
                    ins = sa.insert(user_memories_table).values(
                        id=new_id,
                        user_id=user_id,
                        category=category,
                        key=key,
                        content=wrapped_content,
                        created_at=now,
                        updated_at=now,
                    )
                    await conn.execute(ins)

        # 读取最终写入结果
        async with self._engine.connect() as conn:
            query = sa.select(user_memories_table).where(
                user_memories_table.c.user_id == user_id,
                user_memories_table.c.category == category,
                user_memories_table.c.key == key,
            )
            row = (await conn.execute(query)).fetchone()
            if not row:
                return MemoryItem(
                    user_id=user_id,
                    category=category,
                    key=key,
                    content=wrapped_content,
                    created_at=now,
                    updated_at=now,
                )
            return MemoryItem(
                user_id=row.user_id,
                category=row.category,
                key=row.key,
                content=dict(row.content) if isinstance(row.content, dict) else row.content,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )

    async def get_profile(
        self,
        user_id: str,
        category: str | None = None,
    ) -> list[MemoryItem]:
        async with self._engine.connect() as conn:
            stmt = sa.select(user_memories_table).where(user_memories_table.c.user_id == user_id)
            if category is not None:
                stmt = stmt.where(user_memories_table.c.category == category)
            stmt = stmt.order_by(user_memories_table.c.updated_at.desc())
            rows = (await conn.execute(stmt)).fetchall()

        return [
            MemoryItem(
                user_id=row.user_id,
                category=row.category,
                key=row.key,
                content=dict(row.content) if isinstance(row.content, dict) else row.content,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]

    async def forget_profile(
        self,
        user_id: str,
        category: str,
        key: str,
    ) -> bool:
        async with self._engine.begin() as conn:
            stmt = sa.delete(user_memories_table).where(
                user_memories_table.c.user_id == user_id,
                user_memories_table.c.category == category,
                user_memories_table.c.key == key,
            )
            result = await conn.execute(stmt)
            return bool(result.rowcount and result.rowcount > 0)

    async def remember_episode(
        self,
        user_id: str,
        summary: str,
        details: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EpisodeItem:
        now = datetime.now(UTC)
        episode_id = uuid.uuid4().hex
        async with self._engine.begin() as conn:
            stmt = sa.insert(memory_episodes_table).values(
                episode_id=episode_id,
                user_id=user_id,
                summary=summary,
                details=details or {},
                metadata_json=metadata or {},
                created_at=now,
            )
            await conn.execute(stmt)

        return EpisodeItem(
            episode_id=episode_id,
            user_id=user_id,
            summary=summary,
            details=details or {},
            metadata=metadata or {},
            created_at=now,
        )

    async def recall_episodes(
        self,
        user_id: str,
        query: str | None = None,
        limit: int = 5,
    ) -> list[EpisodeItem]:
        import re

        async with self._engine.connect() as conn:
            stmt = sa.select(memory_episodes_table).where(
                memory_episodes_table.c.user_id == user_id
            )
            if query:
                clean_query = query.strip()
                # 提取英文/数字词汇以及连续中文片段中的所有 2-gram 关键词
                tokens = re.findall(r"[a-zA-Z0-9]+", clean_query)
                for i in range(len(clean_query) - 1):
                    gram = clean_query[i : i + 2].strip()
                    if len(gram) == 2 and any("\u4e00" <= c <= "\u9fff" for c in gram):
                        tokens.append(gram)

                conditions = [memory_episodes_table.c.summary.ilike(f"%{clean_query}%")]
                for token in set(tokens):
                    if token:
                        conditions.append(memory_episodes_table.c.summary.ilike(f"%{token}%"))

                stmt = stmt.where(sa.or_(*conditions))
            stmt = stmt.order_by(memory_episodes_table.c.created_at.desc()).limit(limit)
            rows = (await conn.execute(stmt)).fetchall()

        return [
            EpisodeItem(
                episode_id=row.episode_id,
                user_id=row.user_id,
                summary=row.summary,
                details=dict(row.details) if isinstance(row.details, dict) else (row.details or {}),
                metadata=dict(row.metadata_json)
                if isinstance(row.metadata_json, dict)
                else (row.metadata_json or {}),
                created_at=row.created_at,
            )
            for row in rows
        ]
