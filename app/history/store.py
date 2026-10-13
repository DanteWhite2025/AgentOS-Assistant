"""ConversationStore 协议、游标编解码与进程内实现（Fake / 测试用）。

游标格式与 `app/news/store.py` 保持一致：base64(ISO 时间|标识符)。两个排序
键缺一不可 —— 只按时间分页，同一毫秒的多条记录跨页时会重复或遗漏。
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import replace
from datetime import datetime
from typing import Final, Protocol

from app.history.models import ConversationMessage, ConversationSummary, MessageRole

# 标题降级值的截断长度。LLM 生成成功后会覆盖它（见 app/history/titler.py），
# 失败则永久停留在这个值 —— 它是最终降级值，不是占位符。
TITLE_MAX_CHARS: Final[int] = 30

# 首句为空时的标题。语音会话里 AI 先开口的那一轮提问侧就是空的。
DEFAULT_TITLE: Final[str] = "新对话"


def encode_cursor(when: datetime, ident: str) -> str:
    """游标 = base64(ISO 时间|标识符)。不透明化，避免调用方拿它当 offset 用。"""
    raw = f"{when.isoformat()}|{ident}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """解游标。非法一律抛 ValueError，由路由层转 400。"""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("游标格式不合法") from exc
    when, _, ident = raw.partition("|")
    if not ident:
        raise ValueError("游标缺少标识符")
    return datetime.fromisoformat(when), ident


class ConversationStore(Protocol):
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
        """归档一轮问答；会话行不存在时懒创建。返回是否**新建**了会话行。

        懒创建而非在 POST /sessions 时建行：建了会话却一句没聊的空记录会
        污染历史列表，而用户对着一个点进去什么都没有的条目只会觉得是 bug。

        返回值让调用方判断该不该触发标题生成，省掉一次额外的 get 往返；
        也把「只在首轮生成标题」这个判断收在存储层，两个实现共用同一判据。
        """
        ...

    async def get_conversation(self, session_id: str) -> ConversationSummary | None: ...

    async def list_conversations(
        self, user_id: str, limit: int, cursor: str | None
    ) -> tuple[list[ConversationSummary], str | None]: ...

    async def list_messages(
        self, session_id: str, limit: int, cursor: str | None
    ) -> tuple[list[ConversationMessage], str | None]: ...

    async def recent_turns(self, session_id: str, max_turns: int) -> list[tuple[str, str]]:
        """取最近几轮 (user, assistant)，供 resume 灌回上下文。"""
        ...

    async def set_title(self, session_id: str, title: str, *, only_if: str | None = None) -> None:
        """改标题。会话已不存在时静默返回。

        ``only_if`` 给定时是**比较后再写**：仅当当前标题等于它才生效。

        这个守卫存在的唯一理由是一个真实竞态：标题生成是异步的（统一网关单次
        1.5~3 分钟），用户完全可能在它返回之前就手动改了名。无条件写的话，
        两分钟后模型生成的标题会把用户改的名字冲掉，而用户没有任何反馈。

        所以 titler 传 ``only_if=降级值``（它知道自己是从什么值升级上来的），
        用户重命名则不传 —— 用户意图无条件胜出。
        """
        ...

    async def delete_conversation(self, session_id: str) -> bool:
        """删除会话及其全部消息，返回是否真的删到了行。

        真删而非软删：历史里有仓位与资产类对话，用户点删除就是期待真的没了；
        且本仓库无 Alembic、create_all 不补列，加软删除字段要在线上表手写
        ALTER TABLE，代价大于收益。
        """
        ...

    async def has_message(
        self, session_id: str, role: str, content: str, created_at: datetime
    ) -> bool:
        """这条消息是否已归档。语音字幕去重用。

        App 是异步批量上报，网络重试会重发同一批。重复历史是用户可见的数据
        损坏，不能靠「客户端保证不重传」—— 而 archive_transcript 声明了
        Idempotency-Key 却从未使用它。判据用 (role, content, created_at)：
        created_at 取上报里的 started_at，重传天然带同一个值。
        """
        ...

    async def purge_before(self, cutoff: datetime) -> int:
        """删除最后活跃时间早于 cutoff 的会话及其消息，返回删除的会话数。"""
        ...


def fallback_title(user_text: str) -> str:
    """标题的最终降级值。两个存储实现与 titler 的守卫共用同一份。

    首句为空时给一个中性默认值：语音会话里 AI 常先开口打招呼，首轮的提问侧
    是空的，而 title 列非空 —— 直接写空串会让历史列表出现一行没有文字的条目。
    """
    trimmed = (user_text or "").strip()
    return trimmed[:TITLE_MAX_CHARS] if trimmed else DEFAULT_TITLE


def merged_modality(stored: str, incoming: str) -> str:
    """同一会话里既打字又说话就升级成 both，且只升不降。

    降级会让「混合会话」这个事实随最后一轮漂移 —— 用户上一秒看到语音图标，
    打一行字之后图标就没了。
    """
    return stored if stored == incoming else "both"


class InMemoryConversationStore:
    """进程内实现。Fake 模式与单测用；生产一律 PostgresConversationStore。"""

    def __init__(self) -> None:
        self._conversations: dict[str, ConversationSummary] = {}
        self._messages: list[ConversationMessage] = []
        self._next_id = 1

    def _append(
        self, session_id: str, role: MessageRole, content: str, created_at: datetime
    ) -> None:
        self._messages.append(
            ConversationMessage(
                id=self._next_id,
                session_id=session_id,
                role=role,
                content=content,
                created_at=created_at,
            )
        )
        self._next_id += 1

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
        existing = self._conversations.get(session_id)
        if existing is None:
            self._conversations[session_id] = ConversationSummary(
                session_id=session_id,
                user_id=user_id,
                title=fallback_title(user_text),
                modality=modality,  # type: ignore[arg-type]
                created_at=now,
                last_message_at=now,
                message_count=2,
            )
        else:
            # 刻意不带 title：续写若跟着改标题，第二轮的首句会把已生成的
            # 语义标题冲掉，用户会看到标题莫名其妙变来变去。
            self._conversations[session_id] = replace(
                existing,
                last_message_at=now,
                message_count=existing.message_count + 2,
                modality=merged_modality(existing.modality, modality),  # type: ignore[arg-type]
            )
        self._append(session_id, "user", user_text, now)
        self._append(session_id, "assistant", assistant_text, now)
        return existing is None

    async def get_conversation(self, session_id: str) -> ConversationSummary | None:
        return self._conversations.get(session_id)

    async def list_conversations(
        self, user_id: str, limit: int, cursor: str | None
    ) -> tuple[list[ConversationSummary], str | None]:
        rows = [c for c in self._conversations.values() if c.user_id == user_id]
        rows.sort(key=lambda c: (c.last_message_at, c.session_id), reverse=True)
        if cursor:
            when, ident = decode_cursor(cursor)
            rows = [c for c in rows if (c.last_message_at, c.session_id) < (when, ident)]
        page = rows[:limit]
        next_cursor = (
            encode_cursor(page[-1].last_message_at, page[-1].session_id)
            if len(rows) > limit
            else None
        )
        return page, next_cursor

    async def list_messages(
        self, session_id: str, limit: int, cursor: str | None
    ) -> tuple[list[ConversationMessage], str | None]:
        rows = [m for m in self._messages if m.session_id == session_id]
        rows.sort(key=lambda m: (m.created_at, m.id))
        if cursor:
            when, ident = decode_cursor(cursor)
            rows = [m for m in rows if (m.created_at, m.id) > (when, int(ident))]
        page = rows[:limit]
        next_cursor = (
            encode_cursor(page[-1].created_at, str(page[-1].id)) if len(rows) > limit else None
        )
        return page, next_cursor

    async def recent_turns(self, session_id: str, max_turns: int) -> list[tuple[str, str]]:
        rows = [m for m in self._messages if m.session_id == session_id]
        rows.sort(key=lambda m: (m.created_at, m.id))
        turns: list[tuple[str, str]] = []
        pending: str | None = None
        for message in rows:
            if message.role == "user":
                pending = message.content
            elif pending is not None:
                # 空答案不进上下文：faithful 地存下来是为了回放，但告诉模型
                # 「你上一轮沉默了」会让它下一轮跟着道歉或重复。一期
                # _save_turn 跳过空回合就是这个理由。
                if message.content.strip():
                    turns.append((pending, message.content))
                pending = None
        return turns[-max_turns:]

    async def set_title(self, session_id: str, title: str, *, only_if: str | None = None) -> None:
        existing = self._conversations.get(session_id)
        if existing is None:
            # 标题生成是异步的，回写时会话可能已被清理。
            return
        if only_if is not None and existing.title != only_if:
            # 用户在 titler 返回前改过名，他的意图优先。
            return
        self._conversations[session_id] = replace(existing, title=title)

    async def delete_conversation(self, session_id: str) -> bool:
        if self._conversations.pop(session_id, None) is None:
            return False
        self._messages = [m for m in self._messages if m.session_id != session_id]
        return True

    async def has_message(
        self, session_id: str, role: str, content: str, created_at: datetime
    ) -> bool:
        return any(
            m.session_id == session_id
            and m.role == role
            and m.content == content
            and m.created_at == created_at
            for m in self._messages
        )

    async def purge_before(self, cutoff: datetime) -> int:
        # 判据是最后活跃时间而非创建时间：一直在聊的老会话不该被清掉。
        stale = [sid for sid, c in self._conversations.items() if c.last_message_at < cutoff]
        for sid in stale:
            del self._conversations[sid]
        if stale:
            doomed = set(stale)
            self._messages = [m for m in self._messages if m.session_id not in doomed]
        return len(stale)
