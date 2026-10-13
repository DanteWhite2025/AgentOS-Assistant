"""指代识别与检索词锚定。

一份规则两处用：生产链路（runner 决定检索词、以及未来任何"这句话能不能独立
成问"的判断）和评测体检（evaluations/answer_shape.py 检查下发的 chips）。
两边各写一份的话，改歪一个字判定就不一致，而且不会报错——只有人工去看输出
才发现。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.runtime.session import ConversationTurn


# 指代上文的说法。追问是 chip，点一下就作为全新一轮原样发回来；而带指代的
# 提问用作检索词时，搜索引擎只能按词面命中——两种场景下每命中一条，线上就是
# 一次「答案讲了别人」。
#
# 真机事故：问完 Leopold Aschenbrenner 的基金，chip 是「他一季度具体做空了哪些
# 股票？」，点开后模型把"他"补成了做空领域词频最高的 Michael Burry。
#
# 宁可漏报也不能误报：误报会逼着以后忽略这个信号，那就等于没这个体检。所以
# 单字代词全部带负向断言（「其他」里的「他」、「应该」里的「该」都不算），
# 「该」「这」「那」只在后面接得上具体量词或名词时才算指代。
_DANGLING_PATTERNS = (
    re.compile(r"(?<!其)他(?:们)?"),
    re.compile(r"她(?:们)?"),
    re.compile(r"(?<!其)它(?:们)?"),
    re.compile(r"该(?:基金|公司|股|币|标的|项目|机构|团队|策略|指数|平台|代币|ETF|人)"),
    re.compile(r"[这那](?:只|支|家|档|笔|轮|波|位|人)"),
    re.compile(r"上述|前述|此人|此举|此事"),
)


def has_dangling_reference(text: str) -> bool:
    """这段文字里有没有指代上文的说法。"""

    return any(p.search(text) for p in _DANGLING_PATTERNS)


def dangling_reference_questions(questions: Sequence[str]) -> tuple[str, ...]:
    """挑出指代上文、点开就会串人的追问。"""

    return tuple(q for q in questions if has_dangling_reference(q))


def anchor_search_query(text: str, turns: tuple[ConversationTurn, ...]) -> str:
    """给带指代的检索词拼上一轮的问题作主题锚。

    Tavily 是语义检索，前缀足以把主题钉住：「他一季度具体做空了哪些股票」单独
    去搜，命中的是 Michael Burry 的 13F；拼上「leopold的基金现在怎么样了」之后
    主题就锚住了。

    这里刻意不做 LLM 改写——首字延迟实测 7~9s 已经是主要瓶颈，再串一次 LLM
    出检索词只会更糟。规则也不追求完美消解，它要挡住的是"检索词里一个实体名
    都没有"这种必然出错的情况。

    没命中指代词就原样返回：换话题时拼上一轮反而是污染。
    """

    if not turns or not has_dangling_reference(text):
        return text
    return f"{turns[-1].user_text} {text}"
