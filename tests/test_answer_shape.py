"""追问自包含体检的单测。

追问是我们自己下发的 chip，点一下就变成下一轮的完整提问。而文本回合是无状态
的（app/agents/runner.py 每轮只把当前一句喂给模型），所以一条带代词的追问被
点开时，"他" 没有任何指代对象——模型只能自己补，补成谁全看语料词频。

真机事故：问完 Leopold Aschenbrenner 的基金，chip 给的是「他一季度具体做空了
哪些股票？」，点开后答案讲的是 Michael Burry 的 2021Q1 特斯拉空头。

这类问题只有真机能发现（提示词遵守率离线测不出来），所以体检器要能在评测报告
里把它标出来，别等同事截图。
"""

from __future__ import annotations

import pytest

from evaluations.answer_shape import dangling_reference_questions, inspect


@pytest.mark.parametrize(
    "question",
    [
        "他一季度具体做空了哪些股票？",  # 真机事故原文
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
def test_pronoun_questions_are_flagged(question: str) -> None:
    """带代词的追问一律标出来——点开就会串到别人身上。"""

    assert dangling_reference_questions((question,)) == (question,)


@pytest.mark.parametrize(
    "question",
    [
        "Leopold Aschenbrenner 一季度做空了哪些股票",  # 事故追问的正确写法
        "BTC 仓位的开仓均价是多少",
        "英伟达空头仓位有多大",
        "ARK 成长 ETF 具体是哪几只",
        "我的可用余额是多少",
        "还有其他可以关注的板块吗",  # 「其他」里的「他」不是代词
        "美联储下次议息是什么时候",
        "这个月的 CPI 数据什么时候公布",  # 时间指代不影响重新提问
        "我应该现在减仓吗",  # 「应该」里的「该」不是指代
    ],
)
def test_self_contained_questions_pass(question: str) -> None:
    """自带主语的追问不能误报——误报会逼着以后忽略这个信号。"""

    assert dangling_reference_questions((question,)) == ()


def test_inspect_reports_self_contained_status_from_a_real_answer() -> None:
    """体检走的是生产同一个切分器，代词判定挂在抽出来的 chips 上。"""

    answer = (
        "【重点摘要】\n"
        "- Leopold Aschenbrenner 的基金近期出现回撤传闻。\n"
        "- 报道称规模一度升至约 137 亿美元。\n"
        "- 「450% 亏损」说法缺少权威文件佐证。\n"
        "\n"
        "【你可能还想问】\n"
        "1. 他一季度具体做空了哪些股票\n"
        "2. 13F 持仓能看出真实收益吗\n"
        "3. Situational Awareness 基金规模多少\n"
    )

    shape = inspect(answer)

    assert shape.suggestions_ok, "三条都抽出来了，格式本身没问题"
    assert shape.dangling_questions == ("他一季度具体做空了哪些股票",)
    assert not shape.self_contained_ok


def test_inspect_marks_self_contained_when_every_chip_names_its_subject() -> None:
    answer = (
        "英伟达空头持仓集中在期权端。\n"
        "\n"
        "【你可能还想问】\n"
        "1. 英伟达空头仓位有多大\n"
        "2. 伯里何时平掉特斯拉空头\n"
        "3. ARK 成长 ETF 具体是哪几只\n"
    )

    shape = inspect(answer)

    assert shape.self_contained_ok
    assert shape.dangling_questions == ()


def test_no_questions_means_nothing_to_flag() -> None:
    """抽不出追问是另一个指标的事（suggestions_ok），别在这里也报一次红。"""

    shape = inspect("就一句话，没有追问区。")

    assert shape.dangling_questions == ()
    assert shape.self_contained_ok


def test_report_cell_flags_dangling_chips() -> None:
    """评测报告要把代词追问标出来——这类问题只有真机能发现，报告是唯一的哨兵。"""

    from evaluations.run_baseline import _shape_cell

    answer = (
        "Leopold Aschenbrenner 的基金近期出现回撤传闻。\n"
        "\n"
        "【你可能还想问】\n"
        "1. 他一季度具体做空了哪些股票\n"
        "2. Situational Awareness 基金规模多少\n"
        "3. 该基金是否披露过持仓\n"
    )

    *_, self_contained = _shape_cell(answer, ["web_search"])

    assert "❌" in self_contained
    assert "2" in self_contained, "两条带代词，条数要写进报告好定位"


def test_report_cell_passes_self_contained_chips() -> None:
    from evaluations.run_baseline import _shape_cell

    answer = (
        "英伟达空头持仓集中在期权端。\n"
        "\n"
        "【你可能还想问】\n"
        "1. 英伟达空头仓位有多大\n"
        "2. 伯里何时平掉特斯拉空头\n"
        "3. ARK 成长 ETF 具体是哪几只\n"
    )

    *_, self_contained = _shape_cell(answer, ["web_search"])

    assert self_contained == "✅"
