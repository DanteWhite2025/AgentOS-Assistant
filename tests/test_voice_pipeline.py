"""端到端流水线：LLM token 流 → 分句 → 并发 TTS → 按句序输出音频。

这里守三件事：
* **顺序**：音频必须按句序吐，后句先合成完也不能抢先播；
* **重叠**：第 N 句在输出时，第 N+1 句必须已经在合成（否则退化成串行）；
* **打断与降级**：旧轮次的音频一个字节都不能漏出去，单句失败不能拖死整轮。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from app.voice.pipeline_provider import (
    AudioChunk,
    SentenceFallback,
    VoicePipeline,
)
from app.voice.sentence_buffer import SentenceSplitRules
from app.voice.speech_text import DEFAULT_BRAND_SPOKEN_AS
from app.voice.volc_tts import VolcTTSError, VolcTTSRetryableError

RULES = SentenceSplitRules()


class FakeTTS:
    """按句返回固定分片；可以为某句注入延迟、错误或阻塞。"""

    def __init__(
        self,
        *,
        chunks_per_sentence: int = 1,
        failures: dict[str, list[Exception]] | None = None,
        gates: dict[str, asyncio.Event] | None = None,
    ) -> None:
        self.chunks_per_sentence = chunks_per_sentence
        self.failures = failures or {}
        self.gates = gates or {}
        self.started: list[str] = []
        self.attempts: list[str] = []

    async def stream_synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
    ) -> AsyncIterator[bytes]:
        self.attempts.append(text)
        pending = self.failures.get(text)
        if pending:
            raise pending.pop(0)

        self.started.append(text)
        gate = self.gates.get(text)
        if gate is not None:
            await gate.wait()
        for index in range(self.chunks_per_sentence):
            yield f"{text}#{index}".encode()


async def _deltas(*chunks: str) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


def _pipeline(tts: FakeTTS, **kwargs: object) -> VoicePipeline:
    return VoicePipeline(tts, rules=RULES, **kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_single_sentence_is_emitted_as_a_first_chunk() -> None:
    tts = FakeTTS()
    pipeline = _pipeline(tts)
    turn_id = pipeline.begin_turn()

    events = [
        event async for event in pipeline.stream_turn(_deltas("你好呀！"), turn_id=turn_id)
    ]

    assert events == [
        AudioChunk(
            turn_id=turn_id,
            index=0,
            is_first=True,
            text="你好呀！",
            audio_format="mp3",
            audio=b"\xe4\xbd\xa0\xe5\xa5\xbd\xe5\x91\x80\xef\xbc\x81#0",
        )
    ]


@pytest.mark.asyncio
async def test_audio_is_emitted_in_sentence_order() -> None:
    tts = FakeTTS(chunks_per_sentence=2)
    pipeline = _pipeline(tts)
    turn_id = pipeline.begin_turn()

    events = [
        event
        async for event in pipeline.stream_turn(
            _deltas("你好呀！今天行情不错。要看持仓吗？"), turn_id=turn_id
        )
    ]

    assert [event.text for event in events] == [
        "你好呀！",
        "你好呀！",
        "今天行情不错。",
        "今天行情不错。",
        "要看持仓吗？",
        "要看持仓吗？",
    ]
    assert [event.index for event in events] == [0, 0, 1, 1, 2, 2]
    assert [event.is_first for event in events] == [True, False, False, False, False, False]


@pytest.mark.asyncio
async def test_next_sentence_is_already_synthesizing_while_the_first_one_plays() -> None:
    """流水线重叠：第 1 句还没吐完，第 2 句的合成必须已经开始。

    没有这条，TTS 的耗时就藏不进播放时间里，多句回答会一句一顿。
    """

    first_gate = asyncio.Event()
    tts = FakeTTS(gates={"你好呀！": first_gate})
    pipeline = _pipeline(tts)
    turn_id = pipeline.begin_turn()

    stream = pipeline.stream_turn(
        _deltas("你好呀！今天行情不错。"), turn_id=turn_id
    ).__aiter__()
    consumer = asyncio.create_task(stream.__anext__())

    # 第 1 句卡在 gate 上；此时第 2 句应该已经被送进 TTS。
    for _ in range(50):
        if "今天行情不错。" in tts.started:
            break
        await asyncio.sleep(0)
    assert "今天行情不错。" in tts.started, "第二句没有并发合成，流水线退化成串行"

    first_gate.set()
    assert (await consumer).text == "你好呀！"
    await stream.aclose()


@pytest.mark.asyncio
async def test_cancelling_a_turn_stops_emitting_further_audio() -> None:
    gate = asyncio.Event()
    tts = FakeTTS(gates={"第二句在这里。": gate})
    pipeline = _pipeline(tts)
    turn_id = pipeline.begin_turn()

    stream = pipeline.stream_turn(
        _deltas("你好呀！第二句在这里。"), turn_id=turn_id
    ).__aiter__()
    assert (await stream.__anext__()).text == "你好呀！"

    pipeline.cancel()
    gate.set()

    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


@pytest.mark.asyncio
async def test_a_superseded_turn_emits_nothing_even_if_it_is_still_running() -> None:
    """新一轮开始后，上一轮残留的音频一个字节都不能漏给前端。"""

    tts = FakeTTS()
    pipeline = _pipeline(tts)
    stale_turn = pipeline.begin_turn()
    pipeline.begin_turn()

    events = [
        event async for event in pipeline.stream_turn(_deltas("旧的一轮。"), turn_id=stale_turn)
    ]

    assert events == []


@pytest.mark.asyncio
async def test_a_retryable_failure_is_retried_once_and_then_succeeds() -> None:
    tts = FakeTTS(failures={"你好呀！": [VolcTTSRetryableError("boom")]})
    pipeline = _pipeline(tts)
    turn_id = pipeline.begin_turn()

    events = [
        event async for event in pipeline.stream_turn(_deltas("你好呀！"), turn_id=turn_id)
    ]

    assert tts.attempts == ["你好呀！", "你好呀！"]
    assert [type(event) for event in events] == [AudioChunk]


@pytest.mark.asyncio
async def test_a_sentence_that_keeps_failing_degrades_to_text_without_blocking_the_turn() -> None:
    tts = FakeTTS(
        failures={
            "你好呀！": [VolcTTSRetryableError("boom"), VolcTTSRetryableError("boom again")]
        }
    )
    pipeline = _pipeline(tts)
    turn_id = pipeline.begin_turn()

    events = [
        event
        async for event in pipeline.stream_turn(
            _deltas("你好呀！后面这句还得念出来。"), turn_id=turn_id
        )
    ]

    assert isinstance(events[0], SentenceFallback)
    assert events[0].text == "你好呀！"
    assert [event.text for event in events[1:]] == ["后面这句还得念出来。"]
    assert isinstance(events[1], AudioChunk)


@pytest.mark.asyncio
async def test_a_sentence_that_fails_mid_stream_is_never_retried() -> None:
    """分片已经发出去了就不能重试 —— 用户会听到同一句念两遍。"""

    class HalfwayFailingTTS:
        def __init__(self) -> None:
            self.attempts: list[str] = []

        async def stream_synthesize(
            self,
            text: str,
            *,
            voice: str | None = None,
            speed: float | None = None,
        ) -> AsyncIterator[bytes]:
            self.attempts.append(text)
            yield b"first-half"
            raise VolcTTSRetryableError("connection dropped mid sentence")

    tts = HalfwayFailingTTS()
    pipeline = VoicePipeline(tts, rules=RULES, max_retries=1)
    turn_id = pipeline.begin_turn()

    events = [
        event async for event in pipeline.stream_turn(_deltas("你好呀！"), turn_id=turn_id)
    ]

    assert tts.attempts == ["你好呀！"], "已经发过音频的句子被重试了"
    assert isinstance(events[0], AudioChunk)
    assert events[0].audio == b"first-half"
    assert isinstance(events[1], SentenceFallback)


@pytest.mark.asyncio
async def test_a_slow_first_sentence_still_plays_before_a_fast_second_one() -> None:
    """后句先合成完也必须排队 —— 乱序播放比慢一点严重得多。"""

    slow = asyncio.Event()
    tts = FakeTTS(gates={"慢的第一句。": slow})
    pipeline = _pipeline(tts)
    turn_id = pipeline.begin_turn()

    stream = pipeline.stream_turn(
        _deltas("慢的第一句。快的第二句。"), turn_id=turn_id
    ).__aiter__()
    consumer = asyncio.create_task(stream.__anext__())

    # 让第二句跑完，第一句仍卡着。
    for _ in range(50):
        await asyncio.sleep(0)
    assert not consumer.done(), "第二句抢在第一句前面播了"

    slow.set()
    assert (await consumer).text == "慢的第一句。"
    assert [event.text async for event in stream] == ["快的第二句。"]


@pytest.mark.asyncio
async def test_a_non_retryable_failure_degrades_immediately_without_retrying() -> None:
    """参数错重试也没用，重试只是白烧一次调用和 300ms。"""

    tts = FakeTTS(failures={"你好呀！": [VolcTTSError("bad speaker")]})
    pipeline = _pipeline(tts)
    turn_id = pipeline.begin_turn()

    events = [
        event async for event in pipeline.stream_turn(_deltas("你好呀！"), turn_id=turn_id)
    ]

    assert tts.attempts == ["你好呀！"]
    assert isinstance(events[0], SentenceFallback)


@pytest.mark.asyncio
async def test_a_stalled_sentence_times_out_and_does_not_hang_the_turn() -> None:
    stalled = asyncio.Event()
    tts = FakeTTS(gates={"你好呀！": stalled})
    pipeline = _pipeline(tts, first_chunk_timeout=0.01, max_retries=0)
    turn_id = pipeline.begin_turn()

    events = [
        event
        async for event in pipeline.stream_turn(
            _deltas("你好呀！后面这句正常。"), turn_id=turn_id
        )
    ]

    assert isinstance(events[0], SentenceFallback)
    assert events[0].reason == "timeout"
    assert [event.text for event in events[1:]] == ["后面这句正常。"]
    stalled.set()


@pytest.mark.asyncio
async def test_leaving_the_stream_early_cancels_synthesis_and_closes_the_token_stream() -> None:
    """提前退出不会自动关掉内层流 —— 不显式清理就会漏任务和连接。"""

    closed = False
    gate = asyncio.Event()

    async def tracked_deltas() -> AsyncIterator[str]:
        nonlocal closed
        try:
            yield "你好呀！第二句在这里。第三句也在。"
        finally:
            closed = True

    tts = FakeTTS(gates={"第二句在这里。": gate})
    pipeline = _pipeline(tts)
    turn_id = pipeline.begin_turn()

    stream = pipeline.stream_turn(tracked_deltas(), turn_id=turn_id).__aiter__()
    assert (await stream.__anext__()).text == "你好呀！"
    await stream.aclose()

    assert closed, "token 流没有被关闭"
    assert not pipeline.in_flight, f"仍有未清理的合成任务: {pipeline.in_flight}"
    gate.set()


@pytest.mark.asyncio
async def test_an_empty_token_stream_produces_no_events() -> None:
    tts = FakeTTS()
    pipeline = _pipeline(tts)
    turn_id = pipeline.begin_turn()

    events = [event async for event in pipeline.stream_turn(_deltas("", "  "), turn_id=turn_id)]

    assert events == []
    assert tts.attempts == []


@pytest.mark.asyncio
async def test_many_sentences_with_a_tight_lookahead_still_terminate() -> None:
    """结束信号绝不能因为内部队列满而丢掉。

    丢了它，消费端会永久等在下一个事件上 —— 一轮对话再也不会收尾。带
    ``wait_for`` 是因为这个缺陷的表现是挂死，不加超时会把整个测试套件拖住。
    """

    tts = FakeTTS()
    pipeline = _pipeline(tts, max_lookahead=1)
    turn_id = pipeline.begin_turn()
    text = "".join(f"第{index}句话说完了。" for index in range(10))

    async def drain() -> list[str]:
        return [
            event.text
            async for event in pipeline.stream_turn(_deltas(text), turn_id=turn_id)
        ]

    sentences = await asyncio.wait_for(drain(), timeout=5)
    assert len(sentences) == 10


@pytest.mark.asyncio
async def test_concurrent_synthesis_is_bounded_by_the_lookahead() -> None:
    """模型吐 20 句时不能同时开 20 个 TTS 连接。"""

    gates = {}
    text = "第一句。第二句。第三句。第四句。第五句。"
    for sentence in ("第一句。", "第二句。", "第三句。", "第四句。", "第五句。"):
        gates[sentence] = asyncio.Event()

    tts = FakeTTS(gates=gates)
    pipeline = _pipeline(tts, max_lookahead=2)
    turn_id = pipeline.begin_turn()

    stream = pipeline.stream_turn(_deltas(text), turn_id=turn_id).__aiter__()
    consumer = asyncio.create_task(stream.__anext__())
    for _ in range(50):
        await asyncio.sleep(0)

    assert len(tts.started) <= 2, f"并发合成超过 lookahead: {tts.started}"

    for gate in gates.values():
        gate.set()
    await consumer
    await stream.aclose()


@pytest.mark.asyncio
async def test_the_brand_is_sent_to_tts_in_its_spoken_form() -> None:
    """发音只能在这条边界上修：合成器收到的必须是能被念对的写法。

    火山 TTS 看不到系统提示词，提示词里写「读作 Yes Up」对它毫无作用 ——
    它念的就是这里送进去的字面文本。
    """

    tts = FakeTTS()
    pipeline = _pipeline(tts)
    turn_id = pipeline.begin_turn()

    [event async for event in pipeline.stream_turn(_deltas("YesUP 为您播报。"), turn_id=turn_id)]

    assert tts.started == [f"{DEFAULT_BRAND_SPOKEN_AS} 为您播报。"]


@pytest.mark.asyncio
async def test_the_displayed_text_keeps_the_written_brand() -> None:
    """替换只活到 TTS 请求体里 —— 屏幕上必须还是规范写法。"""

    tts = FakeTTS()
    pipeline = _pipeline(tts)
    turn_id = pipeline.begin_turn()

    events = [
        event async for event in pipeline.stream_turn(_deltas("YesUP 为您播报。"), turn_id=turn_id)
    ]

    assert [event.text for event in events] == ["YesUP 为您播报。"]


@pytest.mark.asyncio
async def test_the_fallback_event_also_keeps_the_written_brand() -> None:
    """合成失败时前端会把这段文字显示出来，同样不能漏出朗读写法。"""

    tts = FakeTTS(failures={f"{DEFAULT_BRAND_SPOKEN_AS} 为您播报。": [VolcTTSError("boom")]})
    pipeline = _pipeline(tts, max_retries=0)
    turn_id = pipeline.begin_turn()

    events = [
        event async for event in pipeline.stream_turn(_deltas("YesUP 为您播报。"), turn_id=turn_id)
    ]

    assert [event.text for event in events] == ["YesUP 为您播报。"]
    assert all(isinstance(event, SentenceFallback) for event in events)


@pytest.mark.asyncio
async def test_a_brand_split_across_token_deltas_is_still_replaced() -> None:
    """模型的流式增量会把 YesUP 切成两片，在 delta 上替换必漏。

    归一化必须发生在分句之后、送 TTS 之前 —— 这条守住那个顺序。
    """

    tts = FakeTTS()
    pipeline = _pipeline(tts)
    turn_id = pipeline.begin_turn()

    [
        event
        async for event in pipeline.stream_turn(
            _deltas("Yes", "UP", " 为您播报。"), turn_id=turn_id
        )
    ]

    assert tts.started == [f"{DEFAULT_BRAND_SPOKEN_AS} 为您播报。"]
