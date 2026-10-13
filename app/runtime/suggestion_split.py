"""把「你可能还想问」从流式正文尾部剥出来。

追问以 chips 形式单独下发（response.suggestions），所以不能同时留在正文里——
用户会看到同样三句话出现两遍。但剥离必须在流式过程中完成：等整段生成完再切，
等于把 PR #78 刚打磨好的首字延迟赔回去。

原则是**宁可晚发几个字，也不能把模型的输出吃掉**：
- 尾部只要还可能是标记的前缀就先扣住，确认不是了立刻放行；
- 标记后面解析不出任何一条时，那段原文补回正文（模型可能把正文写过去了）；
- 只有标记本身永不下发——它是我们要求模型输出的控制符，不是内容。
"""

from __future__ import annotations

import re

# 控制符。用【】包裹是因为提示词里的段落标题本来就是这个形状（模型输出稳定），
# 而完整这一串在正文里几乎不可能自然出现。
MARKER = "【你可能还想问】"

# 一行里的编号与项目符号：1. / 1、/ 1) / - / • / * 都接受——模型在这上面不稳定，
# 为一个符号把整段退回成纯文本不值得。
_BULLET_PREFIX = re.compile(r"^\s*(?:\d+\s*[.、)）]|[-*•·])\s*")

# 行内编号。真机实测（gpt-5.6-terra）5/5 都把三条挤在标记同一行：
#   【你可能还想问】1. 我的可用余额是多少 2. BTC仓位的开仓均价 3. ETH仓位的未实现盈亏
# 提示词已改成要求每条独立成行，但格式全靠模型听话不可靠，这里同时容错。
#
# 必须在行首或空白之后才算编号，且数字后要紧跟标点——否则「5 倍的强平价在哪」
# 「24 小时涨跌多少」这种追问本身带的数字会被当成编号切开。
_INLINE_NUMBERING = re.compile(r"(?:^|(?<=\s))\d+\s*[.、)）]\s*")

_MAX_QUESTIONS = 5

# 一条追问就是一个 chip，本来就是短句（实测 10~25 字）。设这个上限是为了挡住
# 「模型没听格式、把正文接着写在标记后面」的情况——那种一整段散文会被逐行
# 当成追问，渲染成一个巨大的 chip。超限就整段判为解析失败，退回正文。
_MAX_QUESTION_LENGTH = 60


def _marker_prefix_suffix_length(text: str) -> int:
    """text 的最长后缀长度，该后缀同时是 MARKER 的前缀。

    这是「还不能确定要不要发」的那一小段。标记会被 token 边界切成任意多段，
    逐字符到达也必须能拼回来，所以判断只能基于「当前尾部像不像开头」。
    """

    limit = min(len(text), len(MARKER) - 1)
    for length in range(limit, 0, -1):
        if MARKER.startswith(text[-length:]):
            return length
    return 0


def _parse_questions(tail: str) -> tuple[str, ...]:
    """把标记后面的内容解析成追问列表。解析不出来时返回空。"""

    questions: list[str] = []
    for raw_line in tail.splitlines():
        for candidate in _split_line(raw_line):
            if len(candidate) > _MAX_QUESTION_LENGTH:
                return ()
            questions.append(candidate)
    return tuple(questions[:_MAX_QUESTIONS])


def _split_line(raw_line: str) -> list[str]:
    """一行里可能塞了一条，也可能塞了三条。"""

    parts = [part.strip() for part in _INLINE_NUMBERING.split(raw_line)]
    inline = [part for part in parts if part]
    if len(inline) > 1:
        return inline
    # 只切出一段（或零段）说明这行本来就是一条：走原来的前缀剥离，
    # 这样 `- 甲` `• 乙` 这些非数字记号也能处理。
    line = _BULLET_PREFIX.sub("", raw_line).strip()
    return [line] if line else []


class SuggestionSplitter:
    """流式文本的尾部切分器。

    用法：每个 delta 喂 feed()，拿它的返回值下发；流结束调 finish()，把返回的
    尾巴补进正文、把追问列表下发成事件。
    """

    def __init__(self) -> None:
        # 尚未确定能不能发的一小段（可能是标记的前缀）
        self._pending = ""
        # 标记已出现，之后的一切都是追问区
        self._in_tail = False
        self._tail: list[str] = []

    def feed(self, delta: str) -> str:
        """吃一个增量，返回其中可以立刻下发的部分。"""

        if self._in_tail:
            self._tail.append(delta)
            return ""

        self._pending += delta
        index = self._pending.find(MARKER)
        if index >= 0:
            # rstrip 掉标记之前的空白：模型习惯在标记前空一行，留着的话正文
            # 尾巴上会挂两个换行。扣住机制管的是「空白先到、标记后到」，这里
            # 管的是两者落在同一个 delta 里——两种都会发生。
            emitted = self._pending[:index].rstrip()
            self._in_tail = True
            self._tail = [self._pending[index + len(MARKER) :]]
            self._pending = ""
            return emitted

        # 尾部空白同样先扣住：模型习惯在标记前空一行，直接发出去的话剥掉标记
        # 之后正文尾巴上会挂着两个换行，前端渲染出一段空白。后面来的是正文就
        # 连同空白一起发，来的是标记就随标记一起丢。
        #
        # 扣住标记前缀时要连它前面的空白一起扣——"正文。\n\n【你可能" 这种
        # 一个 delta 就跨过了空白，光看尾部是看不到空白的。
        held = _marker_prefix_suffix_length(self._pending)
        before = self._pending[: len(self._pending) - held]
        held += len(before) - len(before.rstrip())
        split_at = len(self._pending) - held
        emitted, self._pending = self._pending[:split_at], self._pending[split_at:]
        return emitted

    def finish(self) -> tuple[str, tuple[str, ...]]:
        """收流。返回（要补进正文的尾巴, 追问列表）。"""

        if not self._in_tail:
            # 流停在标记前缀上（模型被截断）：那几个字是正文，必须补出去。
            leftover, self._pending = self._pending, ""
            return leftover, ()

        tail = "".join(self._tail)
        self._tail = []
        questions = _parse_questions(tail)
        if questions:
            return "", questions
        # 标记后面不是列表——把原文补回正文，只丢掉标记本身。
        return tail.strip(), ()
