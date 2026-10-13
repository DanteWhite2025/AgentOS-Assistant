"""分句流式缓冲器：把 LLM 的 token 流切成可以立刻送去合成的完整短句。

两条规则来自 TECH_DESIGN 2.2：
* 首句加速 —— 攒够 6 字后遇到任何停顿标点（含英文空格）立刻切，压首字延迟；
* 后续常规 —— 只在终结标点切，逗号要等长句（> 20 字）才切，避免碎句破坏语调。

最重要的不变量：切出来的句子拼起来必须等于原文，一个字都不能吞。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.voice.sentence_buffer import (
    SentenceSplitRules,
    split_pending,
    stream_sentences,
)


async def _deltas(*chunks: str) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


async def _collect(*chunks: str, rules: SentenceSplitRules | None = None) -> list[str]:
    return [
        sentence
        async for sentence in stream_sentences(_deltas(*chunks), rules=rules or SentenceSplitRules())
    ]


@pytest.mark.asyncio
async def test_first_sentence_is_cut_at_the_earliest_pause_to_lower_latency() -> None:
    sentences = await _collect("你好呀！今天比特币刚刚突破 68000 美元啦，要不要看下持仓呢？")
    assert sentences[0] == "你好呀！"


@pytest.mark.asyncio
async def test_first_sentence_waits_until_it_has_enough_characters() -> None:
    """「好，」只有两个字，切出去合成只会得到一声突兀的气口。"""

    assert await _collect("好，我看下。") == ["好，我看下。"]


@pytest.mark.asyncio
async def test_later_commas_do_not_split_short_clauses() -> None:
    """首句之后按语义走：短句里的逗号不切，否则语调被切得七零八落。"""

    sentences = await _collect("第一句先说完。然后呢，接着说，最后结束。")
    assert sentences == ["第一句先说完。", "然后呢，接着说，最后结束。"]


@pytest.mark.asyncio
async def test_a_long_clause_is_split_at_a_comma_before_it_grows_unbounded() -> None:
    long_clause = "第一句。" + "行情数据我已经帮你核对过一遍确认无误" + "，" + "接着看下一段。"
    sentences = await _collect(long_clause, rules=SentenceSplitRules(long_clause_chars=10))
    assert sentences == ["第一句。", "行情数据我已经帮你核对过一遍确认无误，", "接着看下一段。"]


@pytest.mark.asyncio
async def test_consecutive_punctuation_never_produces_an_empty_sentence() -> None:
    sentences = await _collect("真的吗？！我再确认一下……好的。")
    assert "" not in sentences
    assert all(sentence.strip() for sentence in sentences)


@pytest.mark.asyncio
async def test_trailing_text_without_punctuation_is_flushed_at_the_end() -> None:
    """模型经常最后一句不带标点就结束，不 flush 就等于把结尾吞掉。"""

    assert await _collect("先说一句完整的。", "然后这句没有标点") == [
        "先说一句完整的。",
        "然后这句没有标点",
    ]


@pytest.mark.asyncio
async def test_token_by_token_stream_splits_the_same_way_as_one_shot_text() -> None:
    text = "你好呀！今天行情不错。要看持仓吗？"
    one_shot = await _collect(text)
    token_stream = await _collect(*text)
    assert token_stream == one_shot


@pytest.mark.asyncio
async def test_nothing_is_dropped_across_mixed_chinese_and_english() -> None:
    text = "Hey there! BTC 刚破 68000 美元，USDT 余额还够吗？Let me check.\n再看下持仓。"
    sentences = await _collect(*text)
    assert "".join(sentences).replace(" ", "") == text.replace(" ", "").replace("\n", "")


@pytest.mark.asyncio
async def test_newline_ends_a_sentence() -> None:
    assert await _collect("第一行说完了\n第二行接着说。") == ["第一行说完了", "第二行接着说。"]


@pytest.mark.asyncio
async def test_blank_and_whitespace_only_deltas_produce_nothing() -> None:
    assert await _collect("", "   ", "\n", "  ") == []


@pytest.mark.asyncio
async def test_decimal_numbers_are_not_split_at_the_dot() -> None:
    """币价小数点被当句号切开，念出来就成了两句 —— 交易场景下这是硬错。"""

    sentences = await _collect(*"BTC 现在 68000.58 美元，涨了 3.5%。")
    assert not any(sentence.endswith("68000.") for sentence in sentences)
    assert "".join(sentences).replace(" ", "") == "BTC现在68000.58美元，涨了3.5%。"


@pytest.mark.asyncio
async def test_first_sentence_rule_applies_across_delta_boundaries() -> None:
    """首句判定要看累积长度，不是单个 delta 的长度。"""

    assert await _collect("你好", "呀", "！", "后面继续说。") == ["你好呀！", "后面继续说。"]


def test_split_pending_rejects_nonsensical_thresholds() -> None:
    with pytest.raises(ValueError):
        SentenceSplitRules(first_sentence_min_chars=0)
    with pytest.raises(ValueError):
        SentenceSplitRules(long_clause_chars=0)


def test_split_pending_is_pure_and_returns_the_untouched_remainder() -> None:
    """纯函数：不改入参，把「切出的句子」和「还没切完的尾巴」一起返回。"""

    pending = "你好呀！今天行情"
    sentences, remainder = split_pending(pending, is_first=True, rules=SentenceSplitRules())
    assert sentences == ("你好呀！",)
    assert remainder == "今天行情"
    assert pending == "你好呀！今天行情"
