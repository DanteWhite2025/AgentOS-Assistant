"""火山豆包流式语音识别（sauc，双向 WS 二进制协议）桥接契约。

全部离线：``_FakeConnection`` 在内存里模拟一条双工连接，测试用文档里定义的
二进制帧格式独立构造「服务端下发的帧」，验证 VolcStreamASRSession 的编码、
解析、事件产出与错误分类——不依赖任何真实网络或 websockets 库。

帧格式（与 app/voice/volc_stream_asr.py 模块文档一致）：
  byte0 = (version<<4)|header_units；byte1 = (msg_type<<4)|flags；
  byte2 = (serialization<<4)|compression；byte3 = 0x00；
  [4 字节大端 sequence，仅当 flags 携带序号位]；4 字节大端 payload size；payload。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.voice.volc_stream_asr import (
    FinalTranscript,
    PartialTranscript,
    VolcengineStreamASRClient,
    VolcStreamASRConfig,
    VolcStreamASRError,
    VolcStreamASRRetryableError,
    VolcStreamASRSession,
)

API_KEY = "volc-stream-asr-key-never-logged"
CONFIG = VolcStreamASRConfig(
    api_key=API_KEY,
    ws_url="wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
    resource_id="volc.seedasr.sauc.duration",
)

_MSG_TYPE_FULL_CLIENT_REQUEST = 0b0001
_MSG_TYPE_AUDIO_ONLY_REQUEST = 0b0010
_MSG_TYPE_FULL_SERVER_RESPONSE = 0b1001
_MSG_TYPE_SERVER_ERROR = 0b1111

_FLAG_NO_SEQUENCE = 0b0000
_FLAG_POS_SEQUENCE = 0b0001
_FLAG_NEG_SEQUENCE = 0b0011

_SERIALIZATION_RAW = 0b0000
_SERIALIZATION_JSON = 0b0001


def _build_frame(message_type: int, flags: int, serialization: int, payload: bytes, sequence: int | None) -> bytes:
    header = bytes([(1 << 4) | 1, (message_type << 4) | flags, (serialization << 4) | 0, 0x00])
    body = bytearray()
    if sequence is not None:
        body += sequence.to_bytes(4, "big", signed=True)
    body += len(payload).to_bytes(4, "big", signed=False)
    body += payload
    return header + bytes(body)


def _server_response(text: str, *, is_final: bool) -> bytes:
    """构造一条 full-server-response 帧，形状对齐真实联调抓到的火山响应。

    真实响应里 ``definite`` 挂在 ``result.utterances[-1]``，不是 ``result`` 顶层——
    这正是当初漏掉、导致真机测试里收不到 transcript.done 的地方。
    """

    payload = json.dumps(
        {
            "audio_info": {"duration": 1000},
            "result": {
                "text": text,
                "utterances": [{"text": text, "definite": is_final, "start_time": 0, "end_time": 1000}],
            },
        }
    ).encode()
    return _build_frame(_MSG_TYPE_FULL_SERVER_RESPONSE, _FLAG_NO_SEQUENCE, _SERIALIZATION_JSON, payload, None)


def _parse_sent_frame(data: bytes) -> dict[str, object]:
    """测试侧独立解析 Runtime 发出的帧，核对头部字段与 payload。"""

    message_type = (data[1] >> 4) & 0x0F
    flags = data[1] & 0x0F
    serialization = (data[2] >> 4) & 0x0F
    offset = 4
    sequence = None
    if flags in (_FLAG_POS_SEQUENCE, _FLAG_NEG_SEQUENCE):
        sequence = int.from_bytes(data[offset : offset + 4], "big", signed=True)
        offset += 4
    size = int.from_bytes(data[offset : offset + 4], "big", signed=False)
    offset += 4
    payload = data[offset : offset + size]
    return {
        "message_type": message_type,
        "flags": flags,
        "serialization": serialization,
        "sequence": sequence,
        "payload": payload,
    }


class _FakeConnection:
    def __init__(self, *, to_send: list[bytes | None] | None = None) -> None:
        self.sent: list[bytes] = []
        self._inbox: asyncio.Queue[bytes | None] = asyncio.Queue()
        for frame in to_send or []:
            self._inbox.put_nowait(frame)
        self.closed = False

    def queue_server_frame(self, frame: bytes) -> None:
        self._inbox.put_nowait(frame)

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def recv(self) -> bytes:
        item = await self._inbox.get()
        if item is None:
            raise ConnectionResetError("fake upstream closed the connection")
        return item

    async def close(self) -> None:
        self.closed = True


class _RaisingConnection:
    """send/recv 全部抛错，模拟握手成功但立刻断线的连接。"""

    async def send(self, data: bytes) -> None:
        raise ConnectionResetError("boom")

    async def recv(self) -> bytes:
        raise ConnectionResetError("boom")

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_start_sends_a_full_client_request_with_the_documented_config() -> None:
    connection = _FakeConnection()
    session = VolcStreamASRSession(connection)

    await session.start(audio_format="pcm", sample_rate=16000, channels=1)

    assert len(connection.sent) == 1
    frame = _parse_sent_frame(connection.sent[0])
    assert frame["message_type"] == _MSG_TYPE_FULL_CLIENT_REQUEST
    assert frame["flags"] == _FLAG_POS_SEQUENCE
    assert frame["serialization"] == _SERIALIZATION_JSON
    assert frame["sequence"] == 1
    payload = frame["payload"]
    assert isinstance(payload, bytes)
    body = json.loads(payload)
    assert body["audio"] == {"format": "pcm", "sample_rate": 16000, "channels": 1, "bits": 16}


@pytest.mark.asyncio
async def test_send_audio_sends_a_raw_audio_only_frame() -> None:
    connection = _FakeConnection()
    session = VolcStreamASRSession(connection)
    await session.start(audio_format="pcm", sample_rate=16000, channels=1)

    await session.send_audio(b"\x01\x02\x03\x04")

    frame = _parse_sent_frame(connection.sent[-1])
    assert frame["message_type"] == _MSG_TYPE_AUDIO_ONLY_REQUEST
    assert frame["flags"] == _FLAG_POS_SEQUENCE
    assert frame["serialization"] == _SERIALIZATION_RAW
    assert frame["sequence"] == 2
    assert frame["payload"] == b"\x01\x02\x03\x04"


@pytest.mark.asyncio
async def test_blank_audio_chunk_never_reaches_the_connection() -> None:
    connection = _FakeConnection()
    session = VolcStreamASRSession(connection)
    await session.start(audio_format="pcm", sample_rate=16000, channels=1)

    await session.send_audio(b"")

    assert len(connection.sent) == 1  # 只有 start 那一帧


@pytest.mark.asyncio
async def test_finish_sends_the_last_package_frame_with_a_negative_sequence() -> None:
    connection = _FakeConnection()
    session = VolcStreamASRSession(connection)
    await session.start(audio_format="pcm", sample_rate=16000, channels=1)
    await session.send_audio(b"\x01\x02")

    await session.finish()

    frame = _parse_sent_frame(connection.sent[-1])
    assert frame["message_type"] == _MSG_TYPE_AUDIO_ONLY_REQUEST
    assert frame["flags"] == _FLAG_NEG_SEQUENCE
    assert frame["sequence"] == -3
    assert frame["payload"] == b""


@pytest.mark.asyncio
async def test_finish_is_idempotent_and_only_sends_the_last_package_once() -> None:
    connection = _FakeConnection()
    session = VolcStreamASRSession(connection)
    await session.start(audio_format="pcm", sample_rate=16000, channels=1)

    await session.finish()
    await session.finish()

    assert len(connection.sent) == 2  # start + 一次末包


@pytest.mark.asyncio
async def test_send_audio_after_finish_is_a_silent_no_op() -> None:
    connection = _FakeConnection()
    session = VolcStreamASRSession(connection)
    await session.start(audio_format="pcm", sample_rate=16000, channels=1)
    await session.finish()

    await session.send_audio(b"\x09\x09")

    assert len(connection.sent) == 2  # 没有多出一帧音频


@pytest.mark.asyncio
async def test_events_yields_partials_then_the_final_transcript_and_stops() -> None:
    connection = _FakeConnection(
        to_send=[
            _server_response("帮我查一下比", is_final=False),
            _server_response("帮我查一下比特币", is_final=False),
            _server_response("帮我查一下比特币现在多少钱", is_final=True),
        ]
    )
    session = VolcStreamASRSession(connection)

    events = [event async for event in session.events()]

    assert events == [
        PartialTranscript("帮我查一下比"),
        PartialTranscript("帮我查一下比特币"),
        FinalTranscript("帮我查一下比特币现在多少钱"),
    ]


@pytest.mark.asyncio
async def test_config_ack_and_real_volc_response_shape_are_handled_correctly() -> None:
    """回归测试：2026-09-09 真机联调抓到的真实响应形状。

    第一帧是握手后的配置确认（只有 ``result.additions.log_id``，没有 ``text``），
    必须被跳过而不是当成一条空文字的 partial。真正的定稿帧里 ``definite`` 挂在
    ``result.utterances[-1]``，不是 ``result`` 顶层——当初就是漏了这层嵌套，导致
    真机测试收不到 ``transcript.done``，反而在多等一帧后把干净的连接关闭误判成
    ``VolcStreamASRRetryableError``。
    """

    config_ack = _build_frame(
        _MSG_TYPE_FULL_SERVER_RESPONSE,
        _FLAG_NO_SEQUENCE,
        _SERIALIZATION_JSON,
        json.dumps({"result": {"additions": {"log_id": "20260909-real-log-id"}}}).encode(),
        None,
    )
    partial = _build_frame(
        _MSG_TYPE_FULL_SERVER_RESPONSE,
        _FLAG_POS_SEQUENCE,
        _SERIALIZATION_JSON,
        json.dumps(
            {
                "audio_info": {"duration": 800},
                "result": {
                    "additions": {"log_id": "20260909-real-log-id"},
                    "text": "帮我",
                    "utterances": [
                        {
                            "additions": {"fixed_prefix_result": "", "source": "stream"},
                            "definite": False,
                            "start_time": 352,
                            "end_time": 652,
                            "text": "帮我",
                        }
                    ],
                },
            }
        ).encode(),
        1,
    )
    final = _build_frame(
        _MSG_TYPE_FULL_SERVER_RESPONSE,
        _FLAG_NEG_SEQUENCE,
        _SERIALIZATION_JSON,
        json.dumps(
            {
                "audio_info": {"duration": 3648},
                "result": {
                    "additions": {"log_id": "20260909-real-log-id"},
                    "text": "帮我查一下比特币现在的价格和我的持仓。",
                    "utterances": [
                        {
                            "additions": {"fixed_prefix_result": "", "source": "stream"},
                            "definite": True,
                            "start_time": 352,
                            "end_time": 3212,
                            "text": "帮我查一下比特币现在的价格和我的持仓。",
                        }
                    ],
                },
            }
        ).encode(),
        -11,
    )
    connection = _FakeConnection(to_send=[config_ack, partial, final])
    session = VolcStreamASRSession(connection)

    # 有超时兜底：定稿检测漏掉的话 events() 会在空队列上永久 await，测试挂死
    # 而不是失败——那样的回归会拖垮整个 CI 而不是给出一条清楚的红。
    async def _collect() -> list:
        return [event async for event in session.events()]

    events = await asyncio.wait_for(_collect(), timeout=2)

    assert events == [
        PartialTranscript("帮我"),
        FinalTranscript("帮我查一下比特币现在的价格和我的持仓。"),
    ]


@pytest.mark.asyncio
async def test_events_skips_unparsable_frames_without_dying() -> None:
    """心跳、空 payload、非法 JSON 都不该打断整条流。"""

    connection = _FakeConnection(
        to_send=[
            _build_frame(_MSG_TYPE_FULL_SERVER_RESPONSE, _FLAG_NO_SEQUENCE, _SERIALIZATION_JSON, b"not json", None),
            _build_frame(_MSG_TYPE_FULL_SERVER_RESPONSE, _FLAG_NO_SEQUENCE, _SERIALIZATION_JSON, b"{}", None),
            _server_response("定稿了", is_final=True),
        ]
    )
    session = VolcStreamASRSession(connection)

    events = [event async for event in session.events()]

    assert events == [FinalTranscript("定稿了")]


@pytest.mark.asyncio
async def test_server_error_frame_raises_a_non_retryable_error() -> None:
    connection = _FakeConnection(
        to_send=[_build_frame(_MSG_TYPE_SERVER_ERROR, _FLAG_NO_SEQUENCE, _SERIALIZATION_JSON, b'{"code":45000001}', None)]
    )
    session = VolcStreamASRSession(connection)

    with pytest.raises(VolcStreamASRError) as excinfo:
        async for _ in session.events():
            pass

    assert not isinstance(excinfo.value, VolcStreamASRRetryableError)


@pytest.mark.asyncio
async def test_connection_drop_while_receiving_is_retryable() -> None:
    connection = _FakeConnection(to_send=[None])  # None 触发 recv() 抛连接重置
    session = VolcStreamASRSession(connection)

    with pytest.raises(VolcStreamASRRetryableError):
        async for _ in session.events():
            pass


@pytest.mark.asyncio
async def test_malformed_frame_is_not_retryable() -> None:
    connection = _FakeConnection(to_send=[b"\x00\x00"])  # 头都不够 4 字节
    session = VolcStreamASRSession(connection)

    with pytest.raises(VolcStreamASRError) as excinfo:
        async for _ in session.events():
            pass

    assert not isinstance(excinfo.value, VolcStreamASRRetryableError)


@pytest.mark.asyncio
async def test_send_failure_is_wrapped_as_retryable_and_never_leaks_the_key() -> None:
    session = VolcStreamASRSession(_RaisingConnection())

    with pytest.raises(VolcStreamASRRetryableError) as excinfo:
        await session.start(audio_format="pcm", sample_rate=16000, channels=1)

    assert API_KEY not in repr(excinfo.value)


@pytest.mark.asyncio
async def test_close_closes_the_underlying_connection() -> None:
    connection = _FakeConnection()
    session = VolcStreamASRSession(connection)

    await session.close()

    assert connection.closed


@pytest.mark.asyncio
async def test_open_session_uses_the_injected_connector_and_config() -> None:
    seen_config = None

    async def fake_connect(config: VolcStreamASRConfig) -> _FakeConnection:
        nonlocal seen_config
        seen_config = config
        return _FakeConnection()

    client = VolcengineStreamASRClient(CONFIG, connect=fake_connect)

    session = await client.open_session()

    assert isinstance(session, VolcStreamASRSession)
    assert seen_config is CONFIG


@pytest.mark.asyncio
async def test_open_session_wraps_connect_failures_as_retryable_and_never_leaks_the_key() -> None:
    async def failing_connect(config: VolcStreamASRConfig):
        raise OSError(f"dns failure for key {config.api_key}")

    client = VolcengineStreamASRClient(CONFIG, connect=failing_connect)

    with pytest.raises(VolcStreamASRRetryableError) as excinfo:
        await client.open_session()

    assert API_KEY not in repr(excinfo.value)
