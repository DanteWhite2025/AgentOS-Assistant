"""工具调用审计存储。MVP 用内存；后续换 SQLAlchemy Core 直插 PostgreSQL。

每条记录含 trace_id / session_id / tool_name / args_hash / result_ref（README 红线）。
args_hash 由调用方用 app.audit.hashing.args_hash 计算。只落 hash 与指针，不落原文。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class AuditRecord:
    trace_id: str
    session_id: str
    tool_name: str
    args_hash: str
    result_ref: str
    created_at: datetime
    status: str = "ok"  # ok / rejected / error —— 拒绝或失败的 tool call 也留痕


class AuditStore(Protocol):
    async def record_tool_call(self, record: AuditRecord) -> None: ...
    async def list_by_session(self, session_id: str) -> list[AuditRecord]: ...


class InMemoryAuditStore:
    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    async def record_tool_call(self, record: AuditRecord) -> None:
        self._records.append(record)

    async def list_by_session(self, session_id: str) -> list[AuditRecord]:
        return [r for r in self._records if r.session_id == session_id]
