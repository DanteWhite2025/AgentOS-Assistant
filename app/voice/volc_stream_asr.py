"""火山豆包流式语音识别（sauc，`bigmodel_async`）双向 WebSocket 桥接。

与 volc_asr.py（一句话识别，整段音频一次 POST）不同：这是「边说边出字」的
双向流式协议，Runtime 建一条到火山的 WS 长连接，客户端音频分片一边到、一边
转发上去，火山识别到的增量文字也一边到、一边转发回客户端。

**二进制帧协议**（火山「大模型语音」系列 WS 服务通用格式，固定 4 字节头）：

```
byte 0: 高 4 位 protocol version（恒为 1），低 4 位 header size（以 4 字节为单位，恒为 1）
byte 1: 高 4 位 message type，低 4 位 message-type-specific flags
byte 2: 高 4 位 serialization（0=raw，1=JSON），低 4 位 compression（恒为 0=不压缩）
byte 3: 保留字节，恒为 0x00
[4 字节 sequence，仅当 flags 携带序号位时才有]
4 字节 payload size（大端，无符号）
payload
```

message type：full-client-request（起始配置，携带 JSON）/ audio-only-request
（音频分片，raw）/ full-server-response（识别结果，JSON）/ server-error（业务错
误）。flags 用「是否携带序号」「是否末包」区分——末包用负数序号标记，这是告诉
火山「音频发完了，尽快出最终结果」的信号，等价 REST 客户端的 EOF。

两条硬约束延续 volc_asr.py / volc_tts.py：

1. **错误必须区分可否重试。** 连接断开、握手失败是网络抖动，值得客户端重开
   一次流；业务错误码、帧解析失败重开也没用。
2. **错误对象只带异常类型，绝不带响应 payload、URL 或 Key。**
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION: Final = 0b0001
_HEADER_SIZE_UNITS: Final = 0b0001  # 固定 4 字节头，不带扩展字段

_MSG_TYPE_FULL_CLIENT_REQUEST: Final = 0b0001
_MSG_TYPE_AUDIO_ONLY_REQUEST: Final = 0b0010
_MSG_TYPE_FULL_SERVER_RESPONSE: Final = 0b1001
_MSG_TYPE_SERVER_ERROR: Final = 0b1111

_FLAG_NO_SEQUENCE: Final = 0b0000
_FLAG_POS_SEQUENCE: Final = 0b0001
_FLAG_NEG_SEQUENCE: Final = 0b0011  # 末包：负数序号
_SEQUENCE_FLAGS: Final = frozenset({_FLAG_POS_SEQUENCE, _FLAG_NEG_SEQUENCE})

_SERIALIZATION_RAW: Final = 0b0000
_SERIALIZATION_JSON: Final = 0b0001
_COMPRESSION_NONE: Final = 0b0000


class VolcStreamASRError(RuntimeError):
    """流式识别失败。不保存响应 payload、URL 或 API Key。"""


class VolcStreamASRRetryableError(VolcStreamASRError):
    """连接抖动或握手失败，值得客户端重开一次流。"""


@dataclass(frozen=True)
class VolcStreamASRConfig:
    """一条流式识别连接内不变的参数。构造后只读。"""

    api_key: str
    ws_url: str
    resource_id: str


@dataclass(frozen=True)
class PartialTranscript:
    """中间态识别结果——还会被后续增量覆盖，不是定稿。"""

    text: str


@dataclass(frozen=True)
class FinalTranscript:
    """整句定稿。收到它之后这条流不会再有更多事件。"""

    text: str


StreamASREvent = PartialTranscript | FinalTranscript


@dataclass(frozen=True)
class _Frame:
    message_type: int
    flags: int
    serialization: int
    sequence: int | None
    payload: bytes


def _encode_frame(
    *,
    message_type: int,
    flags: int,
    serialization: int,
    payload: bytes,
    sequence: int | None = None,
) -> bytes:
    header = bytes(
        [
            (_PROTOCOL_VERSION << 4) | _HEADER_SIZE_UNITS,
            (message_type << 4) | flags,
            (serialization << 4) | _COMPRESSION_NONE,
            0x00,
        ]
    )
    body = bytearray()
    if sequence is not None:
        body += sequence.to_bytes(4, "big", signed=True)
    body += len(payload).to_bytes(4, "big", signed=False)
    body += payload
    return header + bytes(body)


def _decode_frame(data: bytes) -> _Frame:
    if len(data) < 4:
        raise VolcStreamASRError("volc stream asr returned a truncated frame")
    header_units = data[0] & 0x0F
    header_len = header_units * 4
    if header_len < 4 or len(data) < header_len:
        raise VolcStreamASRError("volc stream asr returned a malformed frame header")
    message_type = (data[1] >> 4) & 0x0F
    flags = data[1] & 0x0F
    serialization = (data[2] >> 4) & 0x0F

    offset = header_len
    sequence: int | None = None
    if flags in _SEQUENCE_FLAGS:
        if len(data) < offset + 4:
            raise VolcStreamASRError("volc stream asr frame missing its sequence number")
        sequence = int.from_bytes(data[offset : offset + 4], "big", signed=True)
        offset += 4

    if len(data) < offset + 4:
        raise VolcStreamASRError("volc stream asr frame missing its payload size")
    payload_size = int.from_bytes(data[offset : offset + 4], "big", signed=False)
    offset += 4
    if len(data) < offset + payload_size:
        raise VolcStreamASRError("volc stream asr frame payload shorter than declared size")
    payload = data[offset : offset + payload_size]
    return _Frame(
        message_type=message_type, flags=flags, serialization=serialization, sequence=sequence, payload=payload
    )


def _parse_result_event(payload: bytes) -> StreamASREvent | None:
    """把一条 full-server-response 的 JSON payload 变成事件；解不出就丢，不炸整条流。"""

    try:
        body = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    result = body.get("result")
    if not isinstance(result, dict):
        return None
    text = result.get("text")
    if not isinstance(text, str):
        return None
    return FinalTranscript(text) if _is_definite(result) else PartialTranscript(text)


def _is_definite(result: dict[str, Any]) -> bool:
    """判断一条 full-server-response 是不是定稿。

    2026-09-09 真机联调实测：``definite`` 挂在 ``result.utterances[-1]``，不是
    ``result`` 顶层——顶层的 ``is_final``/``definite`` 从未出现过，之前按顶层
    判断的版本永远判不出定稿，会在真正的最后一帧之后多等一次 ``recv()``，
    把火山发完最终结果后的正常关闭连接误判成 ``VolcStreamASRRetryableError``。
    两处都查，顶层优先，兼容火山未来可能调整的响应形状。
    """

    if bool(result.get("is_final")) or bool(result.get("definite")):
        return True
    utterances = result.get("utterances")
    if isinstance(utterances, list) and utterances:
        last = utterances[-1]
        if isinstance(last, dict) and bool(last.get("definite")):
            return True
    return False


class DuplexConnection(Protocol):
    """一条到火山的原始双工连接；真实实现是 websockets，测试用内存假连接替换。"""

    async def send(self, data: bytes) -> None: ...
    async def recv(self) -> bytes: ...
    async def close(self) -> None: ...


Connector = Callable[[VolcStreamASRConfig], Awaitable[DuplexConnection]]


async def _connect_real(config: VolcStreamASRConfig) -> DuplexConnection:
    import websockets

    # 握手头名对齐同款一句话识别（volc_asr.py）实测修正过的结论——
    # ``X-Resource-Id`` 会被火山直接 403 拒绝握手，必须是 ``X-Api-Resource-Id``。
    # 仅验证到握手成功；握手之后的二进制帧交换仍是未经真实凭证联调的假设。
    connection = await websockets.connect(
        config.ws_url,
        additional_headers={"X-Api-Key": config.api_key, "X-Api-Resource-Id": config.resource_id},
    )
    return _WebsocketsConnection(connection)


class _WebsocketsConnection:
    """把 websockets.ClientConnection 收窄成 DuplexConnection：只吐 bytes。"""

    def __init__(self, connection: object) -> None:
        self._connection = connection

    async def send(self, data: bytes) -> None:
        await self._connection.send(data)  # type: ignore[attr-defined]

    async def recv(self) -> bytes:
        message = await self._connection.recv()  # type: ignore[attr-defined]
        return bytes(message) if isinstance(message, bytes | bytearray) else str(message).encode()

    async def close(self) -> None:
        await self._connection.close()  # type: ignore[attr-defined]


class VolcStreamASRSession:
    """一条流式识别会话：把音频分片喂进去，把识别事件读出来。

    一个会话对应一条 WS 连接，只能用一次——``finish()`` 之后不能再 ``send_audio``。
    """

    def __init__(self, connection: DuplexConnection) -> None:
        self._connection = connection
        self._sequence = 0
        self._finished = False

    def _next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    async def start(self, *, audio_format: str, sample_rate: int, channels: int) -> None:
        payload = json.dumps(
            {
                "user": {"uid": "agent-runtime"},
                "audio": {
                    "format": audio_format,
                    "sample_rate": sample_rate,
                    "channels": channels,
                    "bits": 16,
                },
                "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True},
            }
        ).encode()
        await self._send_frame(
            message_type=_MSG_TYPE_FULL_CLIENT_REQUEST,
            flags=_FLAG_POS_SEQUENCE,
            serialization=_SERIALIZATION_JSON,
            payload=payload,
            sequence=self._next_sequence(),
            stage="start",
        )

    async def send_audio(self, chunk: bytes) -> None:
        if self._finished or not chunk:
            return
        await self._send_frame(
            message_type=_MSG_TYPE_AUDIO_ONLY_REQUEST,
            flags=_FLAG_POS_SEQUENCE,
            serialization=_SERIALIZATION_RAW,
            payload=chunk,
            sequence=self._next_sequence(),
            stage="send_audio",
        )

    async def finish(self) -> None:
        """告诉火山「音频发完了」。幂等——重复调用只发一次末包。"""

        if self._finished:
            return
        self._finished = True
        await self._send_frame(
            message_type=_MSG_TYPE_AUDIO_ONLY_REQUEST,
            flags=_FLAG_NEG_SEQUENCE,
            serialization=_SERIALIZATION_RAW,
            payload=b"",
            sequence=-self._next_sequence(),
            stage="finish",
        )

    async def _send_frame(
        self, *, message_type: int, flags: int, serialization: int, payload: bytes, sequence: int, stage: str
    ) -> None:
        frame = _encode_frame(
            message_type=message_type,
            flags=flags,
            serialization=serialization,
            payload=payload,
            sequence=sequence,
        )
        try:
            await self._connection.send(frame)
        except Exception as exc:
            raise VolcStreamASRRetryableError(
                f"volc stream asr {stage} failed: {type(exc).__name__}"
            ) from exc

    async def events(self) -> AsyncIterator[StreamASREvent]:
        """边收边吐识别事件，收到 ``FinalTranscript`` 后这条流自然结束。"""

        while True:
            # asyncio.CancelledError is BaseException, not Exception — it
            # propagates through this handler untouched, as it must.
            try:
                raw = await self._connection.recv()
            except Exception as exc:
                raise VolcStreamASRRetryableError(
                    f"volc stream asr connection closed: {type(exc).__name__}"
                ) from exc

            frame = _decode_frame(raw)
            if frame.message_type == _MSG_TYPE_SERVER_ERROR:
                raise VolcStreamASRError("volc stream asr returned a business error frame")
            if frame.message_type != _MSG_TYPE_FULL_SERVER_RESPONSE:
                continue
            event = _parse_result_event(frame.payload)
            if event is None:
                continue
            yield event
            if isinstance(event, FinalTranscript):
                return

    async def close(self) -> None:
        await self._connection.close()


class VolcengineStreamASRClient:
    """流式识别会话工厂。无状态，可跨请求共用；每次上传都开一条新连接。"""

    def __init__(self, config: VolcStreamASRConfig, *, connect: Connector | None = None) -> None:
        self._config = config
        self._connect = connect or _connect_real

    async def open_session(self) -> VolcStreamASRSession:
        try:
            connection = await self._connect(self._config)
        except Exception as exc:
            raise VolcStreamASRRetryableError(
                f"volc stream asr connect failed: {type(exc).__name__}"
            ) from exc
        return VolcStreamASRSession(connection)
