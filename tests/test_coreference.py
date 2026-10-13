"""指代词识别与检索词锚定。

事故的下半（docs/plans/2026-09-04-text-turn-conversation-history/TECH_DESIGN.md §6）：
web_search 强制调用直接把 user_text 当检索词，Tavily 拿到「他一季度具体做空了
哪些股票？」，返回的证据包全是 Michael Burry 的 13F。模型有了上下文也会被错误
证据带跑——提示词还要求"必须基于本轮提供的搜索结果回答"。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.runtime.coreference import anchor_search_query, has_dangling_reference
from app.runtime.session import ConversationTurn


def _now() -> datetime:
    return datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "text",
    [
        "他一季度具体做空了哪些股票？",
        "她的持仓有多少",
        "它的资金费率是多少",
        "他们后续会加仓吗",
        "该基金现在规模多少",
        "该公司下季度指引如何",
        "这只股票的支撑位在哪",
        "那家机构还持有多少",
        "上述标的哪个更值得关注",
        "此人还管理其他基金吗",
    ],
)
def test_dangling_references_are_detected(text: str) -> None:
    assert has_dangling_reference(text)


@pytest.mark.parametrize(
    "text",
    [
        "Leopold Aschenbrenner 一季度做空了哪些股票",
        "BTC 仓位的开仓均价是多少",
        "英伟达空头仓位有多大",
        "ARK 成长 ETF 具体是哪几只",
        "我的可用余额是多少",
        "还有其他可以关注的板块吗",
        "美联储下次议息是什么时候",
        "这个月的 CPI 数据什么时候公布",
        "我应该现在减仓吗",
    ],
)
def test_self_contained_text_is_not_flagged(text: str) -> None:
    assert not has_dangling_reference(text)


def test_anchor_prepends_the_previous_question_when_the_query_is_dangling() -> None:
    turns = (ConversationTurn("leopold的基金现在怎么样了", "……", _now()),)

    assert (
        anchor_search_query("他一季度具体做空了哪些股票？", turns)
        == "leopold的基金现在怎么样了 他一季度具体做空了哪些股票？"
    )


def test_anchor_uses_only_the_most_recent_turn() -> None:
    turns = (
        ConversationTurn("很久以前问的事", "……", _now()),
        ConversationTurn("leopold的基金现在怎么样了", "……", _now()),
    )

    anchored = anchor_search_query("他做空了哪些股票", turns)

    assert anchored.startswith("leopold的基金现在怎么样了")
    assert "很久以前" not in anchored


def test_anchor_leaves_a_self_contained_query_untouched() -> None:
    """换话题时拼上一轮反而是污染。"""

    turns = (ConversationTurn("leopold的基金现在怎么样了", "……", _now()),)

    assert anchor_search_query("BTC 现在多少", turns) == "BTC 现在多少"


def test_anchor_without_history_returns_the_query_as_is() -> None:
    assert anchor_search_query("他做空了哪些股票", ()) == "他做空了哪些股票"


def test_evaluations_reuse_the_same_rules() -> None:
    """评测侧不能自己再写一份——改歪一个字两边判定就不一致，且不会报错。"""

    from evaluations import answer_shape

    assert answer_shape.dangling_reference_questions is dangling_reference_questions_source()


def dangling_reference_questions_source() -> object:
    from app.runtime.coreference import dangling_reference_questions

    return dangling_reference_questions
