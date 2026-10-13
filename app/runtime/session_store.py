"""会话存储。MVP 用内存实现；接口为 async，后续可换 Redis。"""

from __future__ import annotations

from typing import Protocol

from app.runtime.session import Session


class SessionStore(Protocol):
    async def create(self, session: Session) -> None: ...
    async def get(self, session_id: str) -> Session | None: ...
    async def update(self, session: Session) -> None: ...
    async def delete(self, session_id: str) -> None: ...


class InMemorySessionStore:
    """进程内会话存储（单副本 MVP / 自测用）。"""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    async def create(self, session: Session) -> None:
        self._sessions[session.session_id] = session

    async def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def update(self, session: Session) -> None:
        self._sessions[session.session_id] = session

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
