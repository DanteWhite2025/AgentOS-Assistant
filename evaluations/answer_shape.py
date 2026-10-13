"""回答结构体检 —— 摘要与追问的格式遵守率。

这几项都是靠提示词约束的，效果取决于模型听不听话，离线测不出来。所以真模型
评测跑完后在这里体检一遍：模型有没有给 3 条摘要、有没有输出追问标记、切分器
能不能从**真实输出**里把追问抽出来、以及抽出来的追问点开后还成不成立。

抽取一律走生产同一个 SuggestionSplitter（而且是按 delta 逐段喂进去），不另写
一份解析逻辑 —— 否则体检通过而线上抽不出来，等于没测。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.runtime.coreference import dangling_reference_questions
from app.runtime.suggestion_split import MARKER, SuggestionSplitter

# 行首的 bullet 记号。模型在 `-` / `*` / `•` / `1.` 之间不稳定，都算。
_BULLET_LINE = re.compile(r"^\s*(?:[-*•·]|\d+\s*[.、)）])\s+\S")

# 把答案切成 delta 喂给切分器：一次性整段喂进去，跨 delta 的边界问题就测不到，
# 而那正是线上最容易出错的地方。7 个字符是随手取的一个不对齐的宽度。
_CHUNK = 7


# 只有深度分析场景才要求「恰好 3 条摘要」。简单查询场景的提示词要求是
# 「结构化分点」，本来就可以是任意条数——按 3 条去卡它是误报。
_DEEP_ANALYSIS_TOOLS = frozenset({"web_search", "ask_investment_expert"})


def is_deep_analysis(tool_calls: Sequence[str]) -> bool:
    """本轮是不是深度分析场景（拿到了证据包或搜索结果）。"""

    return any(name in _DEEP_ANALYSIS_TOOLS for name in tool_calls)


@dataclass(frozen=True)
class AnswerShape:
    """一条回答的结构体检结果。"""

    summary_bullets: int
    has_marker: bool
    questions: tuple[str, ...]
    body: str

    @property
    def summary_ok(self) -> bool:
        return self.summary_bullets == 3

    @property
    def suggestions_ok(self) -> bool:
        return len(self.questions) == 3

    @property
    def leaked_marker(self) -> bool:
        """标记漏进正文——线上就是用户直接看到那行控制符。"""
        return MARKER in self.body

    @property
    def dangling_questions(self) -> tuple[str, ...]:
        """指代上文的追问——点开就是下一次「答案讲了别人」。"""
        return dangling_reference_questions(self.questions)

    @property
    def self_contained_ok(self) -> bool:
        return not self.dangling_questions


def _leading_bullets(body: str) -> int:
    """正文开头连续的 bullet 条数。

    只数第一段：模型在正文里还会用 bullet 展开论据，从头一直数会把它们算进来。
    """

    count = 0
    seen_bullet = False
    for line in body.splitlines():
        if _BULLET_LINE.match(line):
            count += 1
            seen_bullet = True
        elif seen_bullet and line.strip():
            break
    return count


def inspect(answer: str) -> AnswerShape:
    """按线上的方式（逐 delta 过切分器）体检一条真实回答。"""

    splitter = SuggestionSplitter()
    visible = "".join(
        splitter.feed(answer[i : i + _CHUNK]) for i in range(0, len(answer), _CHUNK)
    )
    tail, questions = splitter.finish()
    body = visible + tail
    return AnswerShape(
        summary_bullets=_leading_bullets(body),
        has_marker=MARKER in answer,
        questions=questions,
        body=body,
    )
