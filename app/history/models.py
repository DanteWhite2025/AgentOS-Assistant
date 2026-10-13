"""历史对话领域模型（不可变）。

字段名对内用 `session_id`：它确实就是当时那个 Session 的 id，resume 要拿它
重建 Session，两张表也靠它关联。对外的 `conversation_id` 映射在 API schema
层完成，不渗透到这里。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

MessageRole = Literal["user", "assistant"]

# 一期只收录文本会话。列先建好，二期语音接入不必再动 DDL。
Modality = Literal["text", "voice", "both"]


@dataclass(frozen=True)
class ConversationSummary:
    """一条历史会话的元数据。列表接口的数据源，查它不需要碰消息表。"""

    session_id: str
    user_id: str
    title: str
    modality: Modality
    created_at: datetime
    last_message_at: datetime
    message_count: int


@dataclass(frozen=True)
class ConversationMessage:
    """一条已归档的对话消息。

    没有 tool 角色：工具调用与结果不进对话历史，理由同
    `app/runtime/session.py` 里 ConversationTurn 的注释 —— 过期的行情数字
    留在记录里会被当成当前数据读。
    """

    id: int
    session_id: str
    role: MessageRole
    content: str
    created_at: datetime
