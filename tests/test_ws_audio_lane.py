"""WS 音频通道：把一轮的文本增量分叉进流水线，把音频分片发成 WS 事件。

文本事件一个都不能少 —— 字幕和音频是两条并行的下发，音频链路出问题不该把
文本对话一起拖下水。
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.voice.pipeline_provider import VoicePipeline
from app.voice.sentence_buffer import SentenceSplitRules
from app.voice.volc_tts import VolcTTSError
from app.voice.ws_audio import AudioLane


class FakeTTS:
    def __init__(
        self,
        *,
        failures: dict[str, list[Exception]] | None = None,
        gates: dict[str, asyncio.Event] | None = None,
    ) -> None:
        self.failures = failures or {}
        self.gates = gates or {}
        self.seen: list[str] = []

    async def stream_synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
    ) -> AsyncIterator[bytes]:
        pending = self.failures.get(text)
        if pending:
            raise pending.pop(0)
        self.seen.append(text)
        gate = self.gates.get(text)
        if gate is not None:
            await gate.wait()
        yield f"audio:{text}".encode()


class Recorder:
    """收集下发事件，可选地模拟发送失败。"""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._fail_on = fail_on

    async def __call__(self, type_: str, data: dict[str, Any]) -> None:
        if type_ == self._fail_on:
            raise RuntimeError("client disconnected")
        self.events.append((type_, data))

    def types(self) -> list[str]:
        return [type_ for type_, _ in self.events]


def _lane(tts: FakeTTS, recorder: Recorder, **kwargs: Any) -> AudioLane:
    pipeline = VoicePipeline(tts, rules=SentenceSplitRules(), **kwargs)
    return AudioLane(pipeline, send=recorder, response_id="r_1")


@pytest.mark.asyncio
async def test_a_finished_turn_emits_audio_chunks_then_done() -> None:
    recorder = Recorder()
    lane = _lane(FakeTTS(), recorder)

    await lane.feed("你好呀！今天行情不错。")
    await lane.finish()

    assert recorder.types() == ["audio.chunk", "audio.chunk", "audio.done"]
    first = recorder.events[0][1]
    assert first == {
        "response_id": "r_1",
        "index": 0,
        "is_first": True,
        "format": "mp3",
        "text": "你好呀！",
        "data": base64.b64encode("audio:你好呀！".encode()).decode(),
    }
    assert recorder.events[-1][1] == {"response_id": "r_1", "sentences": 2}


@pytest.mark.asyncio
async def test_audio_is_base64_encoded_for_json_transport() -> None:
    recorder = Recorder()
    lane = _lane(FakeTTS(), recorder)

    await lane.feed("你好呀！")
    await lane.finish()

    payload = recorder.events[0][1]["data"]
    assert base64.b64decode(payload) == "audio:你好呀！".encode()


@pytest.mark.asyncio
async def test_a_sentence_that_cannot_be_synthesized_is_reported_as_a_fallback() -> None:
    recorder = Recorder()
    lane = _lane(
        FakeTTS(failures={"你好呀！": [VolcTTSError("bad speaker")]}),
        recorder,
        max_retries=0,
    )

    await lane.feed("你好呀！后面这句正常。")
    await lane.finish()

    assert recorder.types() == ["audio.fallback", "audio.chunk", "audio.done"]
    assert recorder.events[0][1] == {
        "response_id": "r_1",
        "index": 0,
        "text": "你好呀！",
        "reason": "error",
    }


@pytest.mark.asyncio
async def test_aborting_stops_the_audio_and_never_sends_done() -> None:
    """被打断的那一轮不能再吐音频，也不该报「说完了」。"""

    gate = asyncio.Event()
    tts = FakeTTS(gates={"第二句在这里。": gate})
    recorder = Recorder()
    lane = _lane(tts, recorder)

    await lane.feed("你好呀！第二句在这里。")
    for _ in range(50):
        if recorder.types():
            break
        await asyncio.sleep(0)

    await lane.abort()
    gate.set()
    await asyncio.sleep(0)

    assert "audio.done" not in recorder.types()
    assert not lane.running


@pytest.mark.asyncio
async def test_a_dead_client_never_breaks_the_turn() -> None:
    """客户端断线是常态。音频发不出去只能忽略，绝不能让这一轮抛异常。"""

    recorder = Recorder(fail_on="audio.chunk")
    lane = _lane(FakeTTS(), recorder)

    await lane.feed("你好呀！")
    await lane.finish()

    assert recorder.types() == ["audio.done"]


@pytest.mark.asyncio
async def test_an_empty_turn_produces_no_audio_events_at_all() -> None:
    recorder = Recorder()
    tts = FakeTTS()
    lane = _lane(tts, recorder)

    await lane.finish()

    assert recorder.events == []
    assert tts.seen == []


@pytest.mark.asyncio
async def test_finish_is_idempotent() -> None:
    """异常收尾和正常收尾都会调它，调两次不能吐两条 done。"""

    recorder = Recorder()
    lane = _lane(FakeTTS(), recorder)

    await lane.feed("你好呀！")
    await lane.finish()
    await lane.finish()

    assert recorder.types().count("audio.done") == 1


@pytest.mark.asyncio
async def test_abort_after_finish_is_safe() -> None:
    recorder = Recorder()
    lane = _lane(FakeTTS(), recorder)

    await lane.feed("你好呀！")
    await lane.finish()
    await lane.abort()

    assert recorder.types().count("audio.done") == 1
