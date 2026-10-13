"""英文标题翻译测试。

重点不是"翻得准不准"（那取决于模型），而是这一层的健壮性契约：
LLM 挂了、格式跑偏、漏答、多答，都不能让采集入库跟着挂或写进脏标题。
"""

from __future__ import annotations

import json

import pytest

from app.agents.llm import LLMResult
from app.news.title_translator import (
    NewsTitleTranslator,
    needs_zh_title,
    needs_zh_translation,
)


class _StubLLM:
    """按调用序号返回预设文本；异常项直接抛。"""

    def __init__(self, *responses) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system, messages, tools) -> LLMResult:
        self.calls.append((system, messages[-1].content))
        outcome = self._responses.pop(0) if self._responses else ""
        # BaseException 而非 Exception：CancelledError 不属于后者，
        # 写成 Exception 会让取消用例悄悄退化成「返回一个异常对象」
        if isinstance(outcome, BaseException):
            raise outcome
        return LLMResult(text=outcome)


# ---------------------------------------------------------------------------
# needs_zh_title：判据是「标题无中文」
# ---------------------------------------------------------------------------


def test_needs_zh_title_for_english_title_with_chinese_body() -> None:
    assert needs_zh_title(title="UBIK price up fivefold", content="UBIK为概念代币。")


def test_does_not_need_translation_when_title_already_chinese() -> None:
    assert not needs_zh_title(title="UBIK 价格五倍上涨", content="UBIK为概念代币。")


def test_needs_translation_even_when_body_is_english_too() -> None:
    """分类 tab 下海外英文源直出的条目，标题也统一翻译为中文。"""
    assert needs_zh_title(
        title="Fed holds rates steady", content="The Fed kept rates unchanged."
    )


def test_needs_translation_for_english_title_with_blank_body() -> None:
    """极简英文条目即使无正文也需翻译标题。"""
    assert needs_zh_title(title="Some title", content="")


def test_does_not_need_translation_for_blank_title() -> None:
    assert not needs_zh_title(title="", content="中文正文")
    assert not needs_zh_title(title="")


# ---------------------------------------------------------------------------
# translate：正常路径
# ---------------------------------------------------------------------------


async def test_translate_maps_each_title_to_chinese() -> None:
    llm = _StubLLM('{"1": "英伟达财报超预期", "2": "比特币突破七万一"}')
    translator = NewsTitleTranslator(llm)

    result = await translator.translate(["NVDA beats estimates", "BTC breaks 71k"])

    assert result == {
        "NVDA beats estimates": "英伟达财报超预期",
        "BTC breaks 71k": "比特币突破七万一",
    }
    assert len(llm.calls) == 1, "一批应只发一次调用"


async def test_translate_sends_no_request_for_empty_input() -> None:
    llm = _StubLLM()
    assert await NewsTitleTranslator(llm).translate([]) == {}
    assert llm.calls == []


async def test_translate_deduplicates_identical_titles() -> None:
    """上游同一标题会在多个 route / 多轮里重复出现，重复送翻译等于白烧钱。"""
    llm = _StubLLM('{"1": "英伟达财报超预期"}')
    translator = NewsTitleTranslator(llm)

    result = await translator.translate(["NVDA beats estimates"] * 3)

    assert result == {"NVDA beats estimates": "英伟达财报超预期"}
    assert llm.calls[0][1].count("NVDA beats estimates") == 1


async def test_translate_splits_into_batches() -> None:
    llm = _StubLLM('{"1": "一", "2": "二"}', '{"1": "三"}')
    translator = NewsTitleTranslator(llm, batch_size=2)

    result = await translator.translate(["a", "b", "c"])

    assert result == {"a": "一", "b": "二", "c": "三"}
    assert len(llm.calls) == 2


# ---------------------------------------------------------------------------
# translate：格式容错。模型输出格式不可靠，解析必须宽容
# ---------------------------------------------------------------------------


async def test_translate_parses_json_inside_code_fence() -> None:
    llm = _StubLLM('好的，结果如下：\n```json\n{"1": "英伟达财报超预期"}\n```\n')
    assert await NewsTitleTranslator(llm).translate(["NVDA beats estimates"]) == {
        "NVDA beats estimates": "英伟达财报超预期"
    }


async def test_translate_accepts_integer_like_and_padded_keys() -> None:
    llm = _StubLLM('{" 1 ": "一", "2.": "二"}')
    assert await NewsTitleTranslator(llm).translate(["a", "b"]) == {"a": "一", "b": "二"}


@pytest.mark.parametrize(
    "response",
    [
        "",  # 空回复（max_tokens 被推理吃光时的真实表现）
        "抱歉，我无法完成这个请求。",  # 没有 JSON
        "{不是合法 JSON",
        '{"1": null}',
        '["一", "二"]',  # 类型不对：给了数组不是对象
    ],
)
async def test_translate_returns_empty_on_unusable_response(response: str) -> None:
    """拿不到可用结果就返回空映射——调用方据此保留英文原标题。"""
    assert await NewsTitleTranslator(_StubLLM(response)).translate(["a"]) == {}


async def test_translate_skips_missing_and_out_of_range_keys() -> None:
    """漏答的不编造，多答的越界编号直接丢。"""
    llm = _StubLLM('{"1": "一", "3": "越界"}')
    assert await NewsTitleTranslator(llm).translate(["a", "b"]) == {"a": "一"}


@pytest.mark.parametrize("value", ["", "   ", "NVDA beats estimates"])
async def test_translate_drops_blank_or_untranslated_values(value: str) -> None:
    """空串会把标题清空；原样回抄说明没翻译——两种都不如保留英文原文。"""
    llm = _StubLLM(json.dumps({"1": value}))
    assert await NewsTitleTranslator(llm).translate(["NVDA beats estimates"]) == {}


async def test_translate_drops_values_without_chinese() -> None:
    """模型有时会改写成另一句英文。没有中文就是没完成任务。"""
    llm = _StubLLM('{"1": "NVIDIA exceeded expectations"}')
    assert await NewsTitleTranslator(llm).translate(["NVDA beats estimates"]) == {}


# ---------------------------------------------------------------------------
# translate：故障隔离
# ---------------------------------------------------------------------------


async def test_translate_survives_llm_failure() -> None:
    llm = _StubLLM(RuntimeError("gateway 503"))
    assert await NewsTitleTranslator(llm).translate(["a"]) == {}


async def test_translate_keeps_successful_batches_when_one_batch_fails() -> None:
    """一批挂掉不该让整轮翻译作废——已经花掉的 token 要留下成果。"""
    llm = _StubLLM('{"1": "一"}', RuntimeError("gateway 503"), '{"1": "三"}')
    translator = NewsTitleTranslator(llm, batch_size=1)

    assert await translator.translate(["a", "b", "c"]) == {"a": "一", "c": "三"}


async def test_translate_propagates_cancellation() -> None:
    """CancelledError 必须穿透：容器关停时不能被当成普通失败咽掉。"""
    import asyncio

    translator = NewsTitleTranslator(_StubLLM(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await translator.translate(["a"])


# ---------------------------------------------------------------------------
# translate_items 与 needs_zh_translation：正文联合中文化测试
# ---------------------------------------------------------------------------


def test_needs_zh_translation() -> None:
    assert needs_zh_translation(title="Title", content="Content")
    assert needs_zh_translation(title="Title", content="中文内容")
    assert needs_zh_translation(title="中文标题", content="Content")
    assert not needs_zh_translation(title="中文标题", content="中文内容")
    assert not needs_zh_translation(title="", content="")


async def test_translate_items_full_translation() -> None:
    from types import SimpleNamespace

    item = SimpleNamespace(
        news_id="n1",
        title="BTC breaks 70k",
        content="Bitcoin price surged past 70000 on strong ETF inflows.",
    )
    resp = json.dumps({
        "1": {
            "title": "比特币突破七万",
            "summary": "由于强劲的ETF资金流入，比特币价格突破70000美元大关。",
        }
    })
    llm = _StubLLM(resp)
    translator = NewsTitleTranslator(llm)
    res = await translator.translate_items([item])
    assert res == {
        "n1": ("比特币突破七万", "由于强劲的ETF资金流入，比特币价格突破70000美元大关。")
    }


async def test_translate_items_title_only_when_body_is_chinese() -> None:
    from types import SimpleNamespace

    item = SimpleNamespace(
        news_id="n1", title="BTC breaks 70k", content="比特币价格出现大涨。"
    )
    llm = _StubLLM('{"1": "比特币突破七万"}')
    translator = NewsTitleTranslator(llm)
    res = await translator.translate_items([item])
    assert res == {"n1": ("比特币突破七万", "")}


async def test_translate_items_fallback_on_error() -> None:
    from types import SimpleNamespace

    item = SimpleNamespace(news_id="n1", title="BTC breaks 70k", content="Bitcoin price surged.")
    llm = _StubLLM(RuntimeError("gateway 503"))
    translator = NewsTitleTranslator(llm)
    res = await translator.translate_items([item])
    assert res == {}

