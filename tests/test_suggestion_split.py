"""从流式文本尾部剥出「你可能还想问」。

追问要以 chips 下发给前端，所以不能同时留在正文里——那样用户会看到同样三句
话出现两遍。但剥离必须在**流式**过程中完成，不能等整段生成完（那等于把 PR #78
刚打磨好的首字延迟又赔回去）。

难点全在边界上：标记会被切成任意多段到达，正文里也有大量 `【…】` 结构会误命中
前缀。原则是**宁可晚发几个字，也不能把模型的输出吃掉**。
"""

from __future__ import annotations

from app.runtime.suggestion_split import MARKER, SuggestionSplitter


def _stream(deltas: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """喂完整条流，返回（用户实际看到的正文, 解析出的追问）。"""
    splitter = SuggestionSplitter()
    shown = "".join(splitter.feed(d) for d in deltas)
    tail, questions = splitter.finish()
    return shown + tail, questions


def test_plain_text_passes_through_untouched() -> None:
    body, questions = _stream(("BTC 现在 ", "92,400", "。"))

    assert body == "BTC 现在 92,400。"
    assert questions == ()


def test_suggestions_are_stripped_from_the_body() -> None:
    body, questions = _stream(
        (f"结论在这里。\n\n{MARKER}\n1. 要不要看持仓？\n2. 资金费率呢？\n3. 要下单吗？",)
    )

    assert body == "结论在这里。"
    assert questions == ("要不要看持仓？", "资金费率呢？", "要下单吗？")


def test_marker_split_across_deltas_is_still_recognised() -> None:
    """标记会被模型的 token 边界切碎，这是常态而不是边角情况。"""
    body, questions = _stream(("正文。\n\n【你可能", "还想", "问】\n1. 甲\n2. 乙\n3. 丙"))

    assert body == "正文。"
    assert questions == ("甲", "乙", "丙")


def test_marker_arriving_one_character_at_a_time() -> None:
    text = f"答案。{MARKER}\n- 甲\n- 乙\n- 丙"
    body, questions = _stream(tuple(text))

    assert body == "答案。"
    assert questions == ("甲", "乙", "丙")


def test_other_bracketed_headings_are_not_mistaken_for_the_marker() -> None:
    """正文里 `【…】` 结构很多（核心结论速览、盘面定性…），不能被扣住不发。"""
    body, questions = _stream(("【核心结论速览】BTC 走弱。", "【盘面定性】当前价 92,400。"))

    assert body == "【核心结论速览】BTC 走弱。【盘面定性】当前价 92,400。"
    assert questions == ()


def test_a_partial_marker_at_end_of_stream_is_flushed_not_swallowed() -> None:
    """流在标记前缀上结束（模型被截断）：那几个字是正文，必须补出去。"""
    body, questions = _stream(("正文写完了。", "【你可能"))

    assert body == "正文写完了。【你可能"
    assert questions == ()


def test_numbering_styles_are_all_accepted() -> None:
    body, questions = _stream((f"正文\n{MARKER}\n1、甲\n2) 乙\n• 丙\n",))

    assert body == "正文"
    assert questions == ("甲", "乙", "丙")


def test_at_most_five_questions_are_kept() -> None:
    body, questions = _stream(
        (f"正文{MARKER}\n1. 甲\n2. 乙\n3. 丙\n4. 丁\n5. 戊\n6. 己\n7. 庚",)
    )

    assert questions == ("甲", "乙", "丙", "丁", "戊")
    assert body == "正文"


def test_four_questions_are_parsed_in_full() -> None:
    """4 条在新的 4~5 条区间内，不应该被当成超限截断。"""
    _body, questions = _stream((f"正文{MARKER}\n1. 甲\n2. 乙\n3. 丙\n4. 丁",))

    assert questions == ("甲", "乙", "丙", "丁")


def test_five_questions_are_parsed_in_full() -> None:
    _body, questions = _stream((f"正文{MARKER}\n1. 甲\n2. 乙\n3. 丙\n4. 丁\n5. 戊",))

    assert questions == ("甲", "乙", "丙", "丁", "戊")


def test_fewer_than_three_still_counts_rather_than_degrading() -> None:
    """模型少给一条不值得把整段退回成纯文本。"""
    _body, questions = _stream((f"正文{MARKER}\n1. 甲\n2. 乙",))

    assert questions == ("甲", "乙")


def test_marker_with_nothing_parsable_after_it_flushes_the_remainder() -> None:
    """标记后面不是追问列表（模型把正文写过去了）：内容原样补回正文，绝不吞。

    标记本身是我们要求模型输出的控制符，不是内容，所以它不回来。
    """
    body, questions = _stream((f"正文{MARKER}",))

    assert questions == ()
    assert body == "正文"

    body2, questions2 = _stream((f"正文{MARKER}\n\n\n",))
    assert questions2 == ()
    assert body2 == "正文"


def test_a_long_paragraph_after_the_marker_degrades_instead_of_becoming_a_chip() -> None:
    """模型没听格式、把正文接着写在标记后面时，那段字要回到正文。

    判据只能是长度：一条追问是短句，一段散文不是。13 个字的一句话到底是追问
    还是散文，谁也分不出来——那种就按追问处理，模型既然输出了标记就是这个意图。
    """
    prose = (
        "这不是一个列表，而是模型没有遵守输出格式、直接把正文的最后一段接着"
        "写在了标记后面，因此它整段都应该回到正文里，绝不能被渲染成一个巨大的 chip。"
    )
    body, questions = _stream((f"正文{MARKER}{prose}",))

    assert questions == ()
    assert prose in body, "解析失败时模型写出来的字一个都不能丢"


def test_feed_never_emits_the_marker_itself() -> None:
    splitter = SuggestionSplitter()
    emitted = "".join(
        splitter.feed(d) for d in ("正文", MARKER, "\n1. 甲\n2. 乙\n3. 丙")
    )
    tail, _questions = splitter.finish()

    assert MARKER not in emitted + tail


def test_empty_deltas_are_harmless() -> None:
    body, questions = _stream(("", "正文", "", f"{MARKER}\n1. 甲", ""))

    assert body == "正文"
    assert questions == ("甲",)


def test_all_three_questions_crammed_onto_one_line() -> None:
    """真实模型（gpt-5.6-terra）的实际输出：三条挤在标记同一行。

    提示词里写的「紧跟其后用 1. 2. 3. 编号列出」被理解成了同一行。提示词已经
    改成要求每条独立成行，但解析器必须同时容错——格式全靠模型听话是不可靠的，
    真机跑一轮就发现 5/5 都这么写。
    """
    answer = (
        "- 暂未获取到你的持仓数据。\n\n"
        f"{MARKER}1. 我的可用余额是多少 2. BTC仓位的开仓均价 3. ETH仓位的未实现盈亏"
    )
    body, questions = _stream((answer,))

    assert questions == (
        "我的可用余额是多少",
        "BTC仓位的开仓均价",
        "ETH仓位的未实现盈亏",
    )
    assert body == "- 暂未获取到你的持仓数据。"


def test_one_line_form_split_across_deltas() -> None:
    """同一行的形式同样会被 token 边界切碎。"""
    text = f"结论。{MARKER}1. 甲问题 2. 乙问题 3. 丙问题"
    body, questions = _stream(tuple(text[i : i + 7] for i in range(0, len(text), 7)))

    assert questions == ("甲问题", "乙问题", "丙问题")
    assert body == "结论。"


def test_one_line_form_with_chinese_punctuation() -> None:
    _body, questions = _stream((f"正文{MARKER}1、甲 2、乙 3、丙",))

    assert questions == ("甲", "乙", "丙")


def test_mixed_newline_and_inline_numbering() -> None:
    """模型有时前两条换行、第三条又挤上去。"""
    _body, questions = _stream((f"正文{MARKER}\n1. 甲\n2. 乙 3. 丙",))

    assert questions == ("甲", "乙", "丙")


def test_a_number_inside_a_question_does_not_split_it() -> None:
    """追问本身带数字（「5 倍的强平价」）不能被当成编号切开。"""
    _body, questions = _stream(
        (f"正文{MARKER}\n1. 5 倍的强平价在哪\n2. 24 小时涨跌多少\n3. 要下单吗",)
    )

    assert questions == ("5 倍的强平价在哪", "24 小时涨跌多少", "要下单吗")
