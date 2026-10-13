"""02 方案的语音流水线：LLM 出文本，火山 TTS 出声音。

一轮回答的处理形状：

    token 流 ──► 分句器 ──► 并发合成（最多 max_lookahead 句同时在跑）
                              │
                              └──► 按句序输出音频分片 ──► WS 下发

「并发合成 + 按序输出」是这里的全部价值：第 1 句在播的时候第 2 句已经在合成，
TTS 的耗时被藏进播放时间里，多句回答不会一句一顿。

三条硬约束：

1. **顺序**：音频必须按句序吐。后句先合成完也得排队，否则用户听到的是乱序。
2. **打断**：``cancel()`` 之后旧轮次一个字节都不能再漏给前端。音频已经发出去
   的句子不重试 —— 重试会让用户听到同一句念两遍。
3. **单句失败不拖死整轮**：可重试错误重试一次，仍失败就降级成纯文本事件，
   后面的句子照常合成。参数类错误直接降级，重试只是白烧一次调用。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, suppress
from dataclasses import dataclass
from typing import Final, Protocol

from app.voice.sentence_buffer import SentenceSplitRules, stream_sentences
from app.voice.speech_text import DEFAULT_BRAND_SPOKEN_AS, normalize_for_speech
from app.voice.volc_tts import DEFAULT_AUDIO_FORMAT, VolcTTSError, VolcTTSRetryableError

DEFAULT_FIRST_CHUNK_TIMEOUT_SECONDS: Final = 1.5
DEFAULT_MAX_RETRIES: Final = 1
DEFAULT_MAX_LOOKAHEAD: Final = 2

FALLBACK_TIMEOUT: Final = "timeout"
FALLBACK_ERROR: Final = "error"


class SpeechSynthesizer(Protocol):
    """``VolcengineTTSClient`` 的最小面。测试替身只要实现这一个方法。

    返回类型必须是 ``AsyncGenerator`` 而不是 ``AsyncIterator``：打断时要能
    ``aclose()`` 掉底层的 TTS 连接，而 ``AsyncIterator`` 没有这个能力。
    """

    def stream_synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
    ) -> AsyncGenerator[bytes, None]: ...


@dataclass(frozen=True)
class AudioChunk:
    """一句话里的一个音频分片。"""

    turn_id: int
    index: int
    is_first: bool
    text: str
    audio_format: str
    audio: bytes


@dataclass(frozen=True)
class SentenceFallback:
    """这句没合成出来，只能把文本交给前端 —— 降级，不是失败退出。"""

    turn_id: int
    index: int
    text: str
    reason: str


PipelineEvent = AudioChunk | SentenceFallback


@dataclass(frozen=True)
class _Failed:
    reason: str


_END: Final = None


class _SentenceJob:
    """一句话的合成任务：分片进自己的队列，主循环按句序取。

    ``text`` 是给人看的，``speech_text`` 是给 TTS 念的 —— 两者分开存，因为
    品牌名的书面写法和念法不同（见 ``app.voice.speech_text``），而事件里带出去
    的必须始终是书面写法。
    """

    def __init__(self, index: int, text: str, speech_text: str) -> None:
        self.index = index
        self.text = text
        self.speech_text = speech_text
        self.chunks: asyncio.Queue[bytes | _Failed | None] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        # 记在 job 上而不是 _pump 的局部变量里：合成中途抛异常时，局部变量
        # 根本传不出来，重试判断会误以为「一个分片都没发过」。
        self.emitted = False

    def cancel(self) -> None:
        if self.task is not None and not self.task.done():
            self.task.cancel()


class VoicePipeline:
    """把一轮 LLM 文本流变成一串有序音频分片。

    单会话使用：``begin_turn()`` 拿轮次号，``stream_turn()`` 消费它，
    ``cancel()`` 让当前轮次立刻作废。
    """

    def __init__(
        self,
        synthesizer: SpeechSynthesizer,
        *,
        rules: SentenceSplitRules | None = None,
        audio_format: str = DEFAULT_AUDIO_FORMAT,
        first_chunk_timeout: float = DEFAULT_FIRST_CHUNK_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_lookahead: int = DEFAULT_MAX_LOOKAHEAD,
        brand_spoken_as: str = DEFAULT_BRAND_SPOKEN_AS,
    ) -> None:
        if max_lookahead < 1:
            raise ValueError("max_lookahead must be at least 1")
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")

        self._synthesizer = synthesizer
        self._rules = rules or SentenceSplitRules()
        self._audio_format = audio_format
        self._first_chunk_timeout = first_chunk_timeout
        self._max_retries = max_retries
        self._max_lookahead = max_lookahead
        self._brand_spoken_as = brand_spoken_as
        self._current_turn = 0
        self._in_flight: set[asyncio.Task[None]] = set()

    @property
    def current_turn(self) -> int:
        return self._current_turn

    @property
    def in_flight(self) -> tuple[asyncio.Task[None], ...]:
        """还没结束的合成任务。清理是否干净全看它。"""

        return tuple(task for task in self._in_flight if not task.done())

    def begin_turn(self) -> int:
        """开一个新轮次；上一轮立刻作废。"""

        self._current_turn += 1
        return self._current_turn

    def cancel(self) -> None:
        """作废当前轮次。正在跑的 ``stream_turn`` 会在下一次检查时停止。"""

        self._current_turn += 1

    async def stream_turn(
        self,
        deltas: AsyncIterator[str],
        *,
        turn_id: int,
    ) -> AsyncIterator[PipelineEvent]:
        """消费一轮 token 流，按句序吐音频分片。

        调用方提前退出（``aclose()`` / ``break``）时，producer、所有在跑的合成
        任务和底层 token 流都会被关掉 —— 提前退出不会自动关闭内层流。
        """

        # 句序队列故意无界。有界队列会让收尾信号在队列满时被丢掉，消费端就
        # 永久等在下一个事件上 —— 一轮对话再也不会收尾，而且只在特定调度下
        # 才发作。真正要限制的是「同时开几个 TTS 连接」，那由 gate 保证；
        # 队列里排着的只是轻量 job 对象。
        ready: asyncio.Queue[_SentenceJob | None] = asyncio.Queue()
        gate = asyncio.Semaphore(self._max_lookahead)
        jobs: list[_SentenceJob] = []
        producer = asyncio.create_task(
            self._produce(deltas, ready=ready, gate=gate, jobs=jobs, turn_id=turn_id)
        )

        try:
            while True:
                if turn_id != self._current_turn:
                    return
                job = await ready.get()
                if job is _END:
                    break
                async for event in self._drain(job, turn_id=turn_id):
                    if turn_id != self._current_turn:
                        return
                    yield event
            # producer 的异常要浮出来，不能静默吞掉。
            await producer
        finally:
            producer.cancel()
            with suppress(asyncio.CancelledError):
                await producer
            for job in jobs:
                job.cancel()
            for job in jobs:
                if job.task is not None:
                    with suppress(asyncio.CancelledError):
                        await job.task

    async def _produce(
        self,
        deltas: AsyncIterator[str],
        *,
        ready: asyncio.Queue[_SentenceJob | None],
        gate: asyncio.Semaphore,
        jobs: list[_SentenceJob],
        turn_id: int,
    ) -> None:
        """读句子、启动合成、按序入队。

        ``aclosing`` 是必须的：分句器底下套着 LLM 的 token 流，这条链上任何
        一层提前退出都不会自动关闭内层。
        """

        index = 0
        try:
            async with aclosing(stream_sentences(deltas, rules=self._rules)) as sentences:
                async for sentence in sentences:
                    if turn_id != self._current_turn:
                        return
                    # 归一化只能落在这里：分句之后、送合成之前。放到 delta 上
                    # 做必漏 —— 流式增量会把品牌名切成「Yes」+「UP」两片。
                    job = _SentenceJob(
                        index,
                        sentence,
                        normalize_for_speech(sentence, brand_spoken_as=self._brand_spoken_as),
                    )
                    jobs.append(job)
                    index += 1
                    ready.put_nowait(job)
                    job.task = asyncio.create_task(self._synthesize(job, gate=gate))
                    self._in_flight.add(job.task)
                    job.task.add_done_callback(self._in_flight.discard)
        finally:
            # 收尾信号必须送到，异常和取消路径也一样 —— 否则消费端永久挂起。
            ready.put_nowait(_END)

    async def _synthesize(self, job: _SentenceJob, *, gate: asyncio.Semaphore) -> None:
        """合成一句，分片进 ``job.chunks``；失败则放一个降级标记。"""

        attempt = 0
        while True:
            try:
                async with gate:
                    await self._pump(job)
            except asyncio.CancelledError:
                raise
            except VolcTTSError as exc:
                retryable = isinstance(exc, VolcTTSRetryableError)
                # 已经吐过音频的句子不能重试 —— 用户会听到同一句念两遍。
                if not job.emitted and retryable and attempt < self._max_retries:
                    attempt += 1
                    continue
                await job.chunks.put(_Failed(FALLBACK_ERROR))
            except TimeoutError:
                if not job.emitted and attempt < self._max_retries:
                    attempt += 1
                    continue
                await job.chunks.put(_Failed(FALLBACK_TIMEOUT))
            await job.chunks.put(_END)
            return

    async def _pump(self, job: _SentenceJob) -> None:
        """把一次合成的分片全部搬进队列，并在 job 上记下是否吐过分片。

        只对首包设超时：首包决定用户什么时候听到声音，后续分片慢一点无所谓，
        而给整句设超时会把长句的正常合成误杀。
        """

        async with aclosing(self._synthesizer.stream_synthesize(job.speech_text)) as stream:
            while True:
                if job.emitted:
                    chunk = await anext(stream, None)
                else:
                    async with asyncio.timeout(self._first_chunk_timeout):
                        chunk = await anext(stream, None)
                if chunk is None:
                    return
                job.emitted = True
                await job.chunks.put(chunk)

    async def _drain(self, job: _SentenceJob, *, turn_id: int) -> AsyncIterator[PipelineEvent]:
        """按序输出一句话的全部事件。"""

        is_first = job.index == 0
        while True:
            item = await job.chunks.get()
            if item is _END:
                return
            if isinstance(item, _Failed):
                yield SentenceFallback(
                    turn_id=turn_id,
                    index=job.index,
                    text=job.text,
                    reason=item.reason,
                )
                continue
            yield AudioChunk(
                turn_id=turn_id,
                index=job.index,
                is_first=is_first,
                text=job.text,
                audio_format=self._audio_format,
                audio=item,
            )
            is_first = False
