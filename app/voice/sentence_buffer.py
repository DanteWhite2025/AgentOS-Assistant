"""分句流式缓冲器：LLM 的 token 流 → 可以立刻送去合成的完整短句。

等模型把 150~300 字的整段答案生成完再交给 TTS，用户要等 5 秒以上。破法是
按标点边生成边切句：第一句一出来就送去合成，后面的句子在前一句播放期间
并发合成，把 TTS 的耗时藏进播放时间里。

切句规则（TECH_DESIGN 2.2）：

* **首句加速**：攒够 ``first_sentence_min_chars`` 个字后，遇到任何停顿标点
  （逗号、感叹号、问号、句号、分号、换行，英文空格也算）立刻切。首句越短，
  首字发音越早 —— 这条规则只作用于第一句。
* **后续常规**：只在终结标点（。！？；换行）切。逗号要等到当前句超过
  ``long_clause_chars`` 才切，否则语调会被切碎。

不变量：切出来的句子按顺序拼起来等于原文，一个字都不吞。标点跟着前一句走，
TTS 需要它来决定语调和停顿。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Final

# 终结标点：任何时候遇到都切句。
_TERMINATORS: Final = frozenset("。！？；!?;\n")
# 停顿标点：只在首句加速、或当前句已经很长时才切。
_PAUSES: Final = frozenset("，,、：:")
# 英文句点单独处理 —— 只有它后面跟着空白或结尾时才是句末，避免切碎 68000.5。
_ASCII_PERIOD: Final = "."


@dataclass(frozen=True)
class SentenceSplitRules:
    """切句阈值。构造后只读。"""

    first_sentence_min_chars: int = 6
    long_clause_chars: int = 20

    def __post_init__(self) -> None:
        if self.first_sentence_min_chars < 1:
            raise ValueError("first_sentence_min_chars must be at least 1")
        if self.long_clause_chars < 1:
            raise ValueError("long_clause_chars must be at least 1")


def _is_boundary(
    text: str,
    index: int,
    *,
    is_first: bool,
    rules: SentenceSplitRules,
) -> bool:
    """``text[index]`` 是否构成一个切句点。``text`` 是当前尚未切出的整句。"""

    char = text[index]
    # strip 后计长：前导空白不该算进「攒够几个字」。
    length = len(text[: index + 1].strip())
    if length < 1:
        return False

    if char in _TERMINATORS:
        return True

    if char == _ASCII_PERIOD:
        # 句末的点才是句号；68000.5 里的点不是。判定必须看下一个字符，而流式下
        # 它可能还没到 —— 那就先不切，等下一个 delta 补齐再判。落在整段末尾的点
        # 由 flush 带出去，不会丢字。
        following = text[index + 1 : index + 2]
        return bool(following) and following.isspace()

    if is_first:
        if length < rules.first_sentence_min_chars:
            return False
        # 首句加速：停顿标点和英文空格都算气口。
        return char in _PAUSES or char.isspace()

    if char in _PAUSES:
        return length >= rules.long_clause_chars
    return False


def split_pending(
    pending: str,
    *,
    is_first: bool,
    rules: SentenceSplitRules,
) -> tuple[tuple[str, ...], str]:
    """把 ``pending`` 切成 (完整句子元组, 剩余未切文本)。

    纯函数：不改入参，不持有状态。``is_first`` 只影响第一次切分 —— 一旦切出
    过句子，同一次调用里后面的句子就按常规规则走。
    """

    sentences: list[str] = []
    start = 0
    first_pending = is_first
    for index in range(len(pending)):
        if not _is_boundary(pending, index, is_first=first_pending, rules=rules):
            continue
        candidate = pending[start : index + 1].strip()
        start = index + 1
        if not candidate:
            # 纯标点或纯空白，不值得单独合成，并进下一句。
            continue
        sentences.append(candidate)
        first_pending = False

    return tuple(sentences), pending[start:]


async def stream_sentences(
    deltas: AsyncIterator[str],
    *,
    rules: SentenceSplitRules | None = None,
) -> AsyncGenerator[str, None]:
    """消费 token 流，逐句吐出可合成的短句。

    流结束时 flush 尾巴 —— 模型经常最后一句不带标点就收尾，不 flush 等于把
    结尾吞掉。上游提前退出时由调用方 ``aclose()`` 负责关掉 ``deltas``。
    """

    effective = rules or SentenceSplitRules()
    pending = ""
    is_first = True

    async for delta in deltas:
        if not delta:
            continue
        pending += delta
        sentences, pending = split_pending(pending, is_first=is_first, rules=effective)
        for sentence in sentences:
            is_first = False
            yield sentence

    tail = pending.strip()
    if tail:
        yield tail
