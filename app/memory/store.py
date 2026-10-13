"""长期记忆存储 —— 用户偏好、关注资产、对话摘要与情景记忆。

分层记忆模型：
1. 长期画像/事实记忆 (Profile / Semantic Memory)：用户偏好、关注币种、风控规则，Key-Value/JSONB 结构化存储。
2. 情景记忆 (Episodic Memory)：跨会话决策摘要与重要历史回顾，支持关键词与语义召回。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class MemoryItem:
    user_id: str
    category: str
    key: str
    content: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EpisodeItem:
    episode_id: str
    user_id: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class MemoryStore(Protocol):
    async def remember_profile(
        self,
        user_id: str,
        category: str,
        key: str,
        content: dict[str, Any] | Any,
    ) -> MemoryItem: ...

    async def get_profile(
        self,
        user_id: str,
        category: str | None = None,
    ) -> list[MemoryItem]: ...

    async def forget_profile(
        self,
        user_id: str,
        category: str,
        key: str,
    ) -> bool: ...

    async def remember_episode(
        self,
        user_id: str,
        summary: str,
        details: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EpisodeItem: ...

    async def recall_episodes(
        self,
        user_id: str,
        query: str | None = None,
        limit: int = 5,
    ) -> list[EpisodeItem]: ...


class InMemoryMemoryStore:
    """进程内记忆存储（单机测试/自测用）。"""

    def __init__(self) -> None:
        self._profiles: dict[tuple[str, str, str], MemoryItem] = {}
        self._episodes: list[EpisodeItem] = []

    async def remember_profile(
        self,
        user_id: str,
        category: str,
        key: str,
        content: dict[str, Any] | Any,
    ) -> MemoryItem:
        now = datetime.now(UTC)
        tuple_key = (user_id, category, key)
        wrapped_content = content if isinstance(content, dict) else {"value": content}
        existing = self._profiles.get(tuple_key)
        created_at = existing.created_at if existing else now
        item = MemoryItem(
            user_id=user_id,
            category=category,
            key=key,
            content=wrapped_content,
            created_at=created_at,
            updated_at=now,
        )
        self._profiles[tuple_key] = item
        return item

    async def get_profile(
        self,
        user_id: str,
        category: str | None = None,
    ) -> list[MemoryItem]:
        results = [
            item
            for (uid, cat, _), item in self._profiles.items()
            if uid == user_id and (category is None or cat == category)
        ]
        return sorted(results, key=lambda x: x.updated_at, reverse=True)

    async def forget_profile(
        self,
        user_id: str,
        category: str,
        key: str,
    ) -> bool:
        tuple_key = (user_id, category, key)
        if tuple_key in self._profiles:
            del self._profiles[tuple_key]
            return True
        return False

    async def remember_episode(
        self,
        user_id: str,
        summary: str,
        details: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> EpisodeItem:
        item = EpisodeItem(
            episode_id=uuid.uuid4().hex,
            user_id=user_id,
            summary=summary,
            details=details or {},
            metadata=metadata or {},
            created_at=datetime.now(UTC),
        )
        self._episodes.append(item)
        return item

    async def recall_episodes(
        self,
        user_id: str,
        query: str | None = None,
        limit: int = 5,
    ) -> list[EpisodeItem]:
        user_episodes = [ep for ep in self._episodes if ep.user_id == user_id]
        if not query:
            return sorted(user_episodes, key=lambda x: x.created_at, reverse=True)[:limit]

        query_lower = query.lower().strip()
        query_words = [w.lower() for w in query.split() if w]

        def _score(ep: EpisodeItem) -> int:
            text = f"{ep.summary} {ep.details}".lower()
            if query_lower in text or any(w in text for w in query_words):
                return 10
            # 支持无空格中文 2-gram 匹配
            score = 0
            for i in range(len(query_lower) - 1):
                gram = query_lower[i : i + 2]
                if gram in text:
                    score += 1
            return score

        scored = [(ep, _score(ep)) for ep in user_episodes]
        matched = [ep for ep, s in scored if s > 0]
        matched.sort(key=lambda ep: (_score(ep), ep.created_at), reverse=True)
        return matched[:limit]
