"""WSS /v1/agent/audio/stream：边说边出字的双向流式语音识别。

覆盖握手鉴权、二进制/JSON 两种音频上行方式、增量与定稿下发、参数校验、
上游失败后的受控降级与同连接重试——上游全部替换成可控的假会话，不碰真网络。
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.voice.volc_stream_asr import (
    FinalTranscript,
    PartialTranscript,
    VolcStreamASRRetryableError,
)

URL = "/v1/agent/audio/stream?token=test-user:u1"


class _FakeStreamASRSession:
    """事件严格跟着实际调用产生——一次 send_audio 最多顶一条待发的 partial，
    finish() 才产出 final。这样测试里的发送/接收顺序才能对得上真实时序，不会
    因为假会话「抢跑」而把还没来得及送达的客户端消息冲掉。
    """

    def __init__(self, partial_texts: list[str], final_text: str) -> None:
        self.started_with: tuple[str, int, int] | None = None
        self.audio_chunks: list[bytes] = []
        self.finished = False
        self.closed = False
        self._partial_texts = list(partial_texts)
        self._final_text = final_text
        self._events: asyncio.Queue = asyncio.Queue()

    async def start(self, *, audio_format: str, sample_rate: int, channels: int) -> None:
        self.started_with = (audio_format, sample_rate, channels)

    async def send_audio(self, chunk: bytes) -> None:
        self.audio_chunks.append(chunk)
        if self._partial_texts:
            await self._events.put(PartialTranscript(self._partial_texts.pop(0)))

    async def finish(self) -> None:
        self.finished = True
        await self._events.put(FinalTranscript(self._final_text))

    async def events(self):
        while True:
            event = await self._events.get()
            yield event
            if isinstance(event, FinalTranscript):
                return

    async def close(self) -> None:
        self.closed = True


class _FailingStreamASRSession:
    """上游连接在读事件时直接断线——模拟握手成功但随后掉线。"""

    def __init__(self) -> None:
        self.closed = False

    async def start(self, *, audio_format: str, sample_rate: int, channels: int) -> None:
        return None

    async def send_audio(self, chunk: bytes) -> None:
        return None

    async def finish(self) -> None:
        return None

    async def events(self):
        raise VolcStreamASRRetryableError("volc stream asr connection closed: ConnectionResetError")
        yield  # pragma: no cover - 让这个方法保持是 async generator

    async def close(self) -> None:
        self.closed = True


class _FakeStreamASRClient:
    def __init__(self, sessions: list) -> None:
        self._sessions = sessions
        self.opened = 0

    async def open_session(self):
        session = self._sessions[self.opened]
        self.opened += 1
        return session


def test_happy_path_with_binary_audio_frames() -> None:
    with TestClient(app) as client:
        container = client.app.state.container
        session = _FakeStreamASRSession(["帮我查一下比", "帮我查一下比特币"], "帮我查一下比特币现在多少钱")
        container.stream_asr_client = _FakeStreamASRClient([session])

        with client.websocket_connect(URL) as ws:
            ws.send_json({"type": "audio.start", "data": {"format": "pcm", "sample_rate": 16000, "channels": 1}})
            ws.send_bytes(b"\x01\x02")

            first = ws.receive_json()
            assert first == {"type": "transcript.delta", "data": {"text": "帮我查一下比", "is_final": False}}

            ws.send_bytes(b"\x03\x04")
            second = ws.receive_json()
            assert second == {"type": "transcript.delta", "data": {"text": "帮我查一下比特币", "is_final": False}}

            ws.send_json({"type": "audio.end"})
            done = ws.receive_json()
            assert done == {
                "type": "transcript.done",
                "data": {"text": "帮我查一下比特币现在多少钱", "is_final": True},
            }

        assert session.started_with == ("pcm", 16000, 1)
        assert session.audio_chunks == [b"\x01\x02", b"\x03\x04"]
        assert session.finished
        assert session.closed


def test_happy_path_with_json_base64_audio_frames() -> None:
    with TestClient(app) as client:
        container = client.app.state.container
        session = _FakeStreamASRSession([], "整段一次到位")
        container.stream_asr_client = _FakeStreamASRClient([session])

        with client.websocket_connect(URL) as ws:
            ws.send_json({"type": "audio.start", "data": {"format": "wav"}})
            ws.send_json({"type": "audio.data", "data": base64.b64encode(b"\x05\x06").decode()})
            ws.send_json({"type": "audio.end"})
            done = ws.receive_json()
            assert done["data"]["text"] == "整段一次到位"

        assert session.audio_chunks == [b"\x05\x06"]


def test_unsupported_format_sends_invalid_request_and_keeps_connection_open() -> None:
    with TestClient(app) as client:
        container = client.app.state.container
        session = _FakeStreamASRSession([], "重来一次")
        container.stream_asr_client = _FakeStreamASRClient([session])

        with client.websocket_connect(URL) as ws:
            ws.send_json({"type": "audio.start", "data": {"format": "flac"}})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["data"]["code"] == "INVALID_REQUEST"

            # 同一条连接可以直接用合法格式重开一段——不该被前面的坏参数拖垮。
            ws.send_json({"type": "audio.start", "data": {"format": "pcm"}})
            ws.send_json({"type": "audio.end"})
            done = ws.receive_json()
            assert done["data"]["text"] == "重来一次"


def test_missing_token_closes_with_4001() -> None:
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/v1/agent/audio/stream") as ws,
    ):
        ws.receive_json()

    assert excinfo.value.code == 4001


def test_invalid_token_closes_with_4001() -> None:
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect) as excinfo,
        client.websocket_connect("/v1/agent/audio/stream?token=garbage") as ws,
    ):
        ws.receive_json()

    assert excinfo.value.code == 4001


def test_unconfigured_asr_client_sends_error_and_closes_with_1011() -> None:
    with TestClient(app) as client:
        container = client.app.state.container
        container.stream_asr_client = None

        with pytest.raises(WebSocketDisconnect) as excinfo, client.websocket_connect(URL) as ws:
            error = ws.receive_json()
            assert error == {
                "type": "error",
                "data": {"code": "ASR_UPSTREAM_ERROR", "message": "语音识别服务暂不可用"},
            }
            ws.receive_json()

        assert excinfo.value.code == 1011


def test_upstream_failure_sends_error_event_and_allows_retry_on_the_same_connection() -> None:
    with TestClient(app) as client:
        container = client.app.state.container
        failing = _FailingStreamASRSession()
        recovered = _FakeStreamASRSession([], "重试后成功")
        container.stream_asr_client = _FakeStreamASRClient([failing, recovered])

        with client.websocket_connect(URL) as ws:
            ws.send_json({"type": "audio.start", "data": {"format": "pcm"}})
            ws.send_bytes(b"\x01")
            ws.send_json({"type": "audio.end"})

            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["data"]["code"] == "ASR_UPSTREAM_ERROR"

            ws.send_json({"type": "audio.start", "data": {"format": "pcm"}})
            ws.send_json({"type": "audio.end"})
            done = ws.receive_json()
            assert done == {"type": "transcript.done", "data": {"text": "重试后成功", "is_final": True}}

        assert failing.closed


def test_malformed_json_before_start_sends_invalid_request_but_stays_open() -> None:
    with TestClient(app) as client:
        container = client.app.state.container
        session = _FakeStreamASRSession([], "最终")
        container.stream_asr_client = _FakeStreamASRClient([session])

        with client.websocket_connect(URL) as ws:
            ws.send_text("not json at all")
            error = ws.receive_json()
            assert error["data"]["code"] == "INVALID_REQUEST"

            ws.send_json({"type": "audio.start", "data": {"format": "pcm"}})
            ws.send_json({"type": "audio.end"})
            done = ws.receive_json()
            assert done["data"]["text"] == "最终"
