"""PostgreSQL 审计存储（SQLAlchemy 2.x async Core，直插不搞 ORM 关系）。

实现 AuditStore 接口。配置 AGENT_DATABASE_URL + provider_mode=real 即启用。
只写审计表（本服务对业务库只读，账号权限应隔离）。
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.audit.store import AuditRecord

metadata = sa.MetaData()

audit_tool_calls = sa.Table(
    "audit_tool_calls",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("trace_id", sa.String(64), nullable=False),
    sa.Column("session_id", sa.String(64), nullable=False, index=True),
    sa.Column("tool_name", sa.String(128), nullable=False),
    sa.Column("args_hash", sa.String(64), nullable=False),
    sa.Column("result_ref", sa.Text, nullable=False),
    sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
)


class PostgresAuditStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_schema(self) -> None:
        """建表（MVP 用；生产改 Alembic 迁移）。"""
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def record_tool_call(self, record: AuditRecord) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                sa.insert(audit_tool_calls).values(
                    trace_id=record.trace_id,
                    session_id=record.session_id,
                    tool_name=record.tool_name,
                    args_hash=record.args_hash,
                    result_ref=record.result_ref,
                    status=record.status,
                    created_at=record.created_at,
                )
            )

    async def list_by_session(self, session_id: str) -> list[AuditRecord]:
        async with self._engine.connect() as conn:
            result = await conn.execute(
                sa.select(audit_tool_calls)
                .where(audit_tool_calls.c.session_id == session_id)
                .order_by(audit_tool_calls.c.id)
            )
            rows = result.fetchall()
        return [
            AuditRecord(
                trace_id=row.trace_id,
                session_id=row.session_id,
                tool_name=row.tool_name,
                args_hash=row.args_hash,
                result_ref=row.result_ref,
                status=row.status,
                created_at=row.created_at,
            )
            for row in rows
        ]
