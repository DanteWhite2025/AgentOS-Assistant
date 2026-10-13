"""语音字幕 → (user, assistant) 轮次配对。

语音不像文本那样天然一问一答：用户会打断、AI 会先开口、同一句可能分成
多条字幕上报。这一层就是把流式字幕收敛成可落库的成对轮次。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.history.transcript import VoiceTurn, pair_transcript

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def at(seconds: int) -> datetime:
    return NOW + timedelta(seconds=seconds)


class _T:
    """最小 TranscriptTurn 替身（只用到 role / text / started_at）。"""

    def __init__(self, role: str, text: str, started_at: datetime | None = None) -> None:
        self.role = role
        self.text = text
        self.started_at = started_at


def test_pairs_a_normal_exchange() -> None:
    turns = pair_transcript(
        [_T("user", "BTC 多少钱", at(0)), _T("assistant", "六万二", at(2))], fallback=NOW
    )
    assert turns == [VoiceTurn("BTC 多少钱", "六万二", at(0))]


def test_pairs_multiple_exchanges_in_order() -> None:
    turns = pair_transcript(
        [
            _T("user", "问一", at(0)),
            _T("assistant", "答一", at(1)),
            _T("user", "问二", at(2)),
            _T("assistant", "答二", at(3)),
        ],
        fallback=NOW,
    )
    assert [(t.user_text, t.assistant_text) for t in turns] == [
        ("问一", "答一"),
        ("问二", "答二"),
    ]


def test_interrupted_answer_keeps_what_was_actually_said() -> None:
    """打断算一轮，assistant_text 记打断前实际说出的内容。"""
    turns = pair_transcript(
        [_T("user", "BTC 怎么样", at(0)), _T("assistant", "六万二，今天", at(2))],
        fallback=NOW,
    )
    assert turns == [VoiceTurn("BTC 怎么样", "六万二，今天", at(0))]


def test_trailing_question_without_an_answer_yields_empty_assistant() -> None:
    """字幕结尾处用户问了、AI 还没开口：仍要成对落库，答案侧记空。

    用户确实问了那句话，丢掉等于历史里少一轮。这正是「没开口则记空」的
    实际场景 —— 而不是「连续两段 user」，后者从字幕里分不出是打断还是
    同一句被 ASR 切开（见下一条用例），只能合并。
    """
    turns = pair_transcript(
        [_T("user", "问一", at(0)), _T("assistant", "答一", at(1)), _T("user", "问二", at(2))],
        fallback=NOW,
    )
    assert [(t.user_text, t.assistant_text) for t in turns] == [
        ("问一", "答一"),
        ("问二", ""),
    ]


def test_assistant_greeting_before_user_speaks_is_kept() -> None:
    """语音里 AI 常先开口打招呼，那句不能丢，配空提问。"""
    turns = pair_transcript(
        [_T("assistant", "你好，我在", at(0)), _T("user", "查下持仓", at(1)), _T("assistant", "好", at(2))],
        fallback=NOW,
    )
    assert [(t.user_text, t.assistant_text) for t in turns] == [
        ("", "你好，我在"),
        ("查下持仓", "好"),
    ]


def test_consecutive_assistant_segments_are_joined() -> None:
    """同一句答案分成多条字幕上报，属于一轮，不该拆成两轮。"""
    turns = pair_transcript(
        [
            _T("user", "讲讲 BTC", at(0)),
            _T("assistant", "六万二，", at(1)),
            _T("assistant", "今天涨了 2%", at(2)),
        ],
        fallback=NOW,
    )
    assert turns == [VoiceTurn("讲讲 BTC", "六万二，今天涨了 2%", at(0))]


def test_consecutive_user_segments_are_joined_when_no_answer_between() -> None:
    turns = pair_transcript(
        [_T("user", "帮我看看", at(0)), _T("user", "比特币", at(1)), _T("assistant", "好", at(2))],
        fallback=NOW,
    )
    assert turns == [VoiceTurn("帮我看看比特币", "好", at(0))]


def test_blank_and_whitespace_only_segments_are_dropped() -> None:
    """ASR 会吐空串与纯空白，落库等于攒垃圾行。"""
    turns = pair_transcript(
        [
            _T("user", "  ", at(0)),
            _T("user", "有效提问", at(1)),
            _T("assistant", "", at(2)),
            _T("assistant", "有效回答", at(3)),
        ],
        fallback=NOW,
    )
    assert turns == [VoiceTurn("有效提问", "有效回答", at(1))]


def test_empty_input_yields_nothing() -> None:
    assert pair_transcript([], fallback=NOW) == []


def test_all_blank_input_yields_nothing() -> None:
    assert pair_transcript([_T("user", ""), _T("assistant", "   ")], fallback=NOW) == []


def test_missing_started_at_falls_back() -> None:
    """started_at 是可选字段，缺了就用服务端时间——不能因此丢整轮。"""
    turns = pair_transcript([_T("user", "问", None), _T("assistant", "答", None)], fallback=NOW)
    assert turns == [VoiceTurn("问", "答", NOW)]


def test_turn_timestamp_comes_from_the_user_side() -> None:
    """轮次时间取提问时刻，回放顺序才与实际对话一致。

    全部用服务端 now 会让一整批字幕挤在同一时刻，正序 keyset 分页就只能
    靠自增 id 兜底，跨批次的顺序会乱。
    """
    turns = pair_transcript(
        [_T("user", "问", at(10)), _T("assistant", "答", at(30))], fallback=NOW
    )
    assert turns[0].at == at(10)
