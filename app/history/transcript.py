"""语音字幕 → 可落库的成对轮次。

纯函数，不碰存储也不碰 HTTP —— 语音配对的规则全是边界情况（打断、AI 先
开口、一句话分多条字幕），放在路由里就没法单独测。

为什么必须成对：`ConversationTurn` 与回放的展示都按 (user, assistant) 组织
（见 app/runtime/session.py）。语音是流式的，字幕序列不保证交替出现，所以
这一层负责把它收敛成交替结构，宁可留一侧为空也不打破成对。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class _Segment(Protocol):
    """一条上报的字幕段。

    三个成员刻意写成只读 property 而不是普通属性：普通属性在 Protocol 匹配
    时是**不变**的，`role: str` 就收不下 `TranscriptTurn.role` 那个更窄的
    `Literal["user", "assistant"]`（可写槽位允许塞任意 str，所以窄类型不算
    兼容）。只读成员是协变的，窄类型才能通过。
    """

    @property
    def role(self) -> str: ...

    @property
    def text(self) -> str: ...

    @property
    def started_at(self) -> datetime | None: ...


@dataclass(frozen=True)
class VoiceTurn:
    """一轮语音问答。

    ``assistant_text`` 允许为空 —— 用户打断得够早时 AI 一个字都没说出来，
    那一轮仍要faithful地记下来（用户确实问了）。空值在灌回模型上下文时会被
    过滤掉，见 app/history/store.py 的 recent_turns。
    """

    user_text: str
    assistant_text: str
    at: datetime


def pair_transcript(segments: Iterable[_Segment], *, fallback: datetime) -> list[VoiceTurn]:
    """把字幕序列收敛成成对轮次。

    ``fallback`` 用于 started_at 缺失时兜底 —— 那是个可选字段，缺了不能丢整轮。

    规则：
    - 空串与纯空白直接丢（ASR 会吐这些，落库等于攒垃圾行）
    - 同一角色连续多条 → 拼成一条。「一句答案分多条字幕上报」与「用户连说
      两句」在字幕里长得一模一样，分不出来，所以一律当一轮；反过来拆成两轮
      会把「帮我看看」「比特币」变成两条历史
    - AI 先开口（语音里很常见的打招呼）→ 配空提问，不丢那句话
    - 结尾处用户问了但 AI 未开口 → 配空答案，用户确实问了
    """

    merged = _merge_adjacent(segments)
    turns: list[VoiceTurn] = []
    pending_user: tuple[str, datetime] | None = None

    for role, text, started_at in merged:
        when = started_at or fallback
        if role == "user":
            # 合并过之后不会出现连续两条 user，所以这里不必处理「上一轮没
            # 等到回答」——那种情况只会落在循环结束后的收尾里。
            pending_user = (text, when)
            continue
        if pending_user is None:
            # AI 先开口。轮次时间只能取它自己的。
            turns.append(VoiceTurn("", text, when))
            continue
        turns.append(VoiceTurn(pending_user[0], text, pending_user[1]))
        pending_user = None

    if pending_user is not None:
        turns.append(VoiceTurn(pending_user[0], "", pending_user[1]))
    return turns


def _merge_adjacent(
    segments: Iterable[_Segment],
) -> Sequence[tuple[str, str, datetime | None]]:
    """丢掉空白段，并把同角色的相邻段拼起来。

    拼接不加分隔符：ASR 分段是按停顿切的，原文本身已带标点，插空格会在
    中文里留下多余间隔。时间戳取该角色这一串里**最早**的那个。
    """

    merged: list[tuple[str, str, datetime | None]] = []
    for segment in segments:
        text = (segment.text or "").strip()
        if not text:
            continue
        if merged and merged[-1][0] == segment.role:
            role, previous, when = merged[-1]
            merged[-1] = (role, previous + text, when or segment.started_at)
            continue
        merged.append((segment.role, text, segment.started_at))
    return merged
