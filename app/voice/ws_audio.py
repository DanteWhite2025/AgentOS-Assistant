"""把一轮回答的文本增量分叉进流水线，再把音频分片发成 WS 事件。

字幕和音频是两条并行的下发：文本增量照旧实时发，音频在后台边合成边发。
这条通道的每一处失败都必须是「少一点声音」，而不是「这一轮挂掉」—— 客户端
断线是常态，而这一轮可能已经产生了真实的工具副作用。

事件沿用现有 WS 信封（``type`` + ``seq`` + ``session_id`` + ``data``），不引入
第二套协议：

* ``audio.chunk``    —— 一个音频分片，``is_first`` 为真时前端退出思考动画开播；
* ``audio.fallback`` —— 这句合成不出来，只有文本，前端照常显示但没有声音；
* ``audio.done``     —— 本轮音频发完了。被打断的轮次不发它。
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any, Final

from app.voice.pipeline_provider import AudioChunk, SentenceFallback, VoicePipeline

logger = logging.getLogger(__name__)

SendEvent = Callable[[str, dict[str, Any]], Awaitable[None]]

_END: Final = None


class AudioLane:
    """一轮对话的音频通道。一轮一个实例，不复用。"""

    def __init__(
        self,
        pipeline: VoicePipeline,
        *,
        send: SendEvent,
        response_id: str,
    ) -> None:
        self._pipeline = pipeline
        self._send = send
        self._response_id = response_id
        self._deltas: asyncio.Queue[str | None] = asyncio.Queue()
        self._turn_id = pipeline.begin_turn()
        self._sentences = 0
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def feed(self, delta: str) -> None:
        """把一个文本增量交给流水线。首次调用时才真正启动合成。"""

        if self._closed:
            return
        if self._task is None:
            self._task = asyncio.create_task(self._pump())
        await self._deltas.put(delta)

    async def finish(self) -> None:
        """本轮文本吐完了：等音频发完，再发一条 ``audio.done``。

        幂等 —— 正常收尾和异常收尾都会调它。
        """

        if self._closed:
            return
        self._closed = True
        await self._deltas.put(_END)
        if self._task is None:
            return
        try:
            await self._task
        except asyncio.CancelledError:
            raise
        except Exception:
            # 音频链路的任何意外都只影响声音，不影响这一轮的文本与工具结果。
            logger.warning("音频通道异常结束，本轮降级为纯文本", exc_info=True)
        await self._emit("audio.done", {"sentences": self._sentences})

    async def abort(self) -> None:
        """被打断：立刻作废本轮音频，不发 ``audio.done``。"""

        if self._closed and self._task is None:
            return
        self._closed = True
        self._pipeline.cancel()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _pump(self) -> None:
        async for event in self._pipeline.stream_turn(self._token_stream(), turn_id=self._turn_id):
            if isinstance(event, AudioChunk):
                self._sentences = max(self._sentences, event.index + 1)
                await self._emit(
                    "audio.chunk",
                    {
                        "index": event.index,
                        "is_first": event.is_first,
                        "format": event.audio_format,
                        "text": event.text,
                        "data": base64.b64encode(event.audio).decode(),
                    },
                )
                continue
            if isinstance(event, SentenceFallback):
                self._sentences = max(self._sentences, event.index + 1)
                await self._emit(
                    "audio.fallback",
                    {"index": event.index, "text": event.text, "reason": event.reason},
                )

    async def _token_stream(self) -> AsyncIterator[str]:
        while True:
            delta = await self._deltas.get()
            if delta is _END:
                return
            yield delta

    async def _emit(self, type_: str, data: dict[str, Any]) -> None:
        """发一条音频事件。发不出去就算了 —— 客户端断线不该中断这一轮。"""

        try:
            await self._send(type_, {"response_id": self._response_id, **data})
        except Exception:
            logger.warning("%s 下发失败（客户端可能已断线），忽略", type_, exc_info=True)
