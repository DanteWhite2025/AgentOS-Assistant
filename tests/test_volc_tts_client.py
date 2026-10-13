"""火山豆包 TTS 2.0（seed-tts-2.0，V3 单向流式 SSE）客户端契约。

全部离线：用 MockTransport 造真实的分块字节流，验证「边收边吐」而不是
「攒完一次性吐」—— 流水线的首包时延全靠这一点。
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest

from app.voice.volc_tts import (
    VolcengineTTSClient,
    VolcTTSConfig,
    VolcTTSError,
    VolcTTSRetryableError,
)

API_KEY = "volc-tts-key-never-logged"
CONFIG = VolcTTSConfig(
    api_key=API_KEY,
    url="https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse",
    resource_id="seed-tts-2.0",
    voice="zh_female_tianmeixiaoyuan_uranus_bigtts",
    speed_ratio=1.15,
)


class _ChunkStream(httpx.AsyncByteStream):
    """按给定顺序逐块吐出的响应体，模拟服务端流式下发。"""

    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _sse(*events: dict[str, object]) -> tuple[bytes, ...]:
    return tuple(f"data: {json.dumps(event)}\n\n".encode() for event in events)


def _audio_event(payload: bytes) -> dict[str, object]:
    return {"code": 0, "data": base64.b64encode(payload).decode()}


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[VolcengineTTSClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return VolcengineTTSClient(http, CONFIG), http


async def _collect(client: VolcengineTTSClient, text: str) -> list[bytes]:
    return [chunk async for chunk in client.stream_synthesize(text)]


@pytest.mark.asyncio
async def test_stream_synthesize_yields_each_sse_audio_chunk_in_order() -> None:
    stream = _ChunkStream(_sse(_audio_event(b"one"), _audio_event(b"two"), _audio_event(b"three")))

    client, http = _client(lambda _request: httpx.Response(200, stream=stream))
    async with http:
        assert await _collect(client, "你好呀！") == [b"one", b"two", b"three"]


@pytest.mark.asyncio
async def test_first_chunk_arrives_before_the_rest_of_the_stream_exists() -> None:
    """首包不能等整句合成完。

    上游在吐完第一个事件后阻塞：如果实现是「攒完再返回」，这里会直接超时。
    流水线 < 300ms 的首包指标全靠这条契约。
    """

    released = asyncio.Event()

    class _BlockingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield _sse(_audio_event(b"first"))[0]
            await released.wait()
            yield _sse(_audio_event(b"second"))[0]

        async def aclose(self) -> None:
            return None

    client, http = _client(lambda _request: httpx.Response(200, stream=_BlockingStream()))
    async with http:
        generator = client.stream_synthesize("你好呀！今天行情不错。")
        first = await asyncio.wait_for(generator.__anext__(), timeout=1)
        assert first == b"first"

        released.set()
        assert await asyncio.wait_for(generator.__anext__(), timeout=1) == b"second"
        await generator.aclose()


@pytest.mark.asyncio
async def test_stream_synthesize_sends_the_documented_v3_contract() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, stream=_ChunkStream(_sse(_audio_event(b"ok"))))

    client, http = _client(handler)
    async with http:
        await _collect(client, "句子内容")

    assert seen["url"] == CONFIG.url
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["x-api-key"] == API_KEY
    assert headers["x-api-resource-id"] == "seed-tts-2.0"
    assert seen["payload"] == {
        "req_params": {
            "speaker": "zh_female_tianmeixiaoyuan_uranus_bigtts",
            "text": "句子内容",
        },
        "audio_params": {"format": "mp3", "sample_rate": 24000, "speed_ratio": 1.15},
    }


@pytest.mark.asyncio
async def test_per_call_voice_and_speed_override_the_configured_defaults() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        return httpx.Response(200, stream=_ChunkStream(_sse(_audio_event(b"ok"))))

    client, http = _client(handler)
    async with http:
        async for _ in client.stream_synthesize(
            "试听", voice="zh_female_cancan_uranus_bigtts", speed=1.0
        ):
            pass

    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["req_params"]["speaker"] == "zh_female_cancan_uranus_bigtts"
    assert payload["audio_params"]["speed_ratio"] == 1.0


@pytest.mark.asyncio
async def test_blank_text_never_reaches_the_network() -> None:
    """分句器 flush 出空白残句是常态，不该为它烧一次 TTS 调用。"""

    def handler(_request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("blank text must not be synthesized")

    client, http = _client(handler)
    async with http:
        assert await _collect(client, "   \n ") == []


@pytest.mark.asyncio
async def test_noise_lines_between_audio_events_are_skipped() -> None:
    """SSE 里的空行、注释、心跳事件与非法 JSON 都不该打断整句合成。"""

    chunks = (
        b"\n",
        b": keep-alive\n",
        b'data: {"code": 0}\n\n',
        b"data: not-json\n\n",
        b'data: {"code": 0, "data": ""}\n\n',
    ) + _sse(_audio_event(b"audio"))

    client, http = _client(lambda _request: httpx.Response(200, stream=_ChunkStream(chunks)))
    async with http:
        assert await _collect(client, "你好") == [b"audio"]


@pytest.mark.asyncio
async def test_the_real_end_of_stream_event_is_not_treated_as_a_failure() -> None:
    """火山用 ``code=20000000 / message=OK`` 表示正常收尾，不是错误。

    真链路抓包实测的尾部三个事件就是下面这样（``event: 351`` 是句子元数据、
    ``event: 152`` 是结束）。把 20000000 当错误的话，每一句都会在音频已经发完
    之后再补一条降级事件 —— 声音是对的，但前端会以为整轮都合成失败了。
    """

    chunks = (
        b"event: 352\n",
        b'data: {"code": 0, "message": "", "data": "'
        + base64.b64encode(b"audio")
        + b'"}\n\n',
        b"event: 351\n",
        b'data: {"code": 0, "message": "", "data": null, "sentence": {"text": "\xe4\xbd\xa0\xe5\xa5\xbd"}}\n\n',
        b"event: 152\n",
        b'data: {"code": 20000000, "message": "OK", "data": null}\n\n',
    )

    client, http = _client(lambda _request: httpx.Response(200, stream=_ChunkStream(chunks)))
    async with http:
        assert await _collect(client, "你好呀！") == [b"audio"]


@pytest.mark.asyncio
async def test_sse_error_code_without_audio_raises_a_non_retryable_error() -> None:
    events = _sse({"code": 40000001, "message": "invalid speaker"})

    client, http = _client(lambda _request: httpx.Response(200, stream=_ChunkStream(events)))
    async with http:
        with pytest.raises(VolcTTSError) as excinfo:
            await _collect(client, "你好")

    assert not isinstance(excinfo.value, VolcTTSRetryableError)


@pytest.mark.asyncio
async def test_client_error_status_is_not_retryable() -> None:
    """参数写错重试一万次也还是错的，只有可重试异常才该进重试分支。"""

    client, http = _client(lambda _request: httpx.Response(400, json={"message": "bad speaker"}))
    async with http:
        with pytest.raises(VolcTTSError) as excinfo:
            await _collect(client, "你好")

    assert not isinstance(excinfo.value, VolcTTSRetryableError)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_server_side_status_is_retryable(status: int) -> None:
    client, http = _client(lambda _request: httpx.Response(status, json={"message": "busy"}))
    async with http:
        with pytest.raises(VolcTTSRetryableError):
            await _collect(client, "你好")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("dns"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("cut")],
)
async def test_transport_failures_are_retryable(failure: Exception) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise failure

    client, http = _client(handler)
    async with http:
        with pytest.raises(VolcTTSRetryableError):
            await _collect(client, "你好")


@pytest.mark.asyncio
async def test_errors_never_carry_the_api_key_or_response_body() -> None:
    """错误对象会进日志和告警，带上 Key 或 body 就等于把凭证写进日志。"""

    client, http = _client(
        lambda _request: httpx.Response(400, json={"message": "bad", "token": API_KEY})
    )
    async with http:
        with pytest.raises(VolcTTSError) as excinfo:
            await _collect(client, "你好")

    rendered = repr(excinfo.value)
    assert API_KEY not in rendered
    assert "bad" not in rendered


@pytest.mark.asyncio
async def test_cancellation_propagates_and_closes_the_upstream_stream() -> None:
    """打断时必须真的断开火山连接，否则被丢弃的那句仍在计费和占连接。"""

    stream = _ChunkStream(_sse(_audio_event(b"one"), _audio_event(b"two")))
    client, http = _client(lambda _request: httpx.Response(200, stream=stream))

    async with http:
        generator = client.stream_synthesize("你好呀！今天行情不错。")
        assert await generator.__anext__() == b"one"

        with pytest.raises(asyncio.CancelledError):
            await generator.athrow(asyncio.CancelledError())

    assert stream.closed
