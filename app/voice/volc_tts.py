"""火山豆包语音合成 2.0（seed-tts-2.0）单向流式客户端。

V3 `unidirectional/sse` 端点：一次 POST 一句文本，服务端用 SSE 逐块回 base64
音频。单 API Key 自包含 —— 不需要 AppID、不需要签名、不签票，音频经 Runtime
转发给前端，所以这里只有一条纯粹的「文本进、音频块出」通道。

两条硬约束：

1. **必须边收边吐。** 流水线的首包时延（目标 < 300ms）全压在这上面，攒完
   再返回等于把流式退化成整句合成。
2. **错误必须区分可否重试。** 参数写错（4xx / SSE 业务错误码）重试一万次
   还是错的；网络抖动和 5xx 才值得重试一次。上层按异常类型分流，不解析文案。

错误对象只带状态码，绝不带响应 body、URL 或 Key —— 它们会进日志和告警。
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Final

import httpx

# 火山的成功业务码。真链路抓包实测：音频事件与句子元数据事件用 0，整段合成
# 正常收尾用 20000000（message="OK"）—— 把它当错误的话，每一句都会在音频已经
# 发完之后再补一条降级事件。其余非零码才是真失败。
_SUCCESS_CODES: Final = frozenset({0, 20000000})
# 明确可重试的 HTTP 状态：限流与服务端故障。
_RETRYABLE_STATUS: Final = frozenset({408, 425, 429, 500, 502, 503, 504})
_SSE_DATA_PREFIX: Final = "data:"

DEFAULT_AUDIO_FORMAT: Final = "mp3"
DEFAULT_SAMPLE_RATE: Final = 24000


class VolcTTSError(RuntimeError):
    """火山 TTS 合成失败。不保存响应 body、URL 或 API Key。"""


class VolcTTSRetryableError(VolcTTSError):
    """网络抖动或服务端故障，值得重试一次。"""


@dataclass(frozen=True)
class VolcTTSConfig:
    """一次会话内不变的合成参数。构造后只读。"""

    api_key: str
    url: str
    resource_id: str
    voice: str
    speed_ratio: float
    audio_format: str = DEFAULT_AUDIO_FORMAT
    sample_rate: int = DEFAULT_SAMPLE_RATE


class VolcengineTTSClient:
    """把一句文本换成一串音频分片。无状态，可跨会话共用。"""

    def __init__(self, client: httpx.AsyncClient, config: VolcTTSConfig) -> None:
        self._client = client
        self._config = config

    @property
    def config(self) -> VolcTTSConfig:
        return self._config

    def _headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self._config.api_key,
            "X-Api-Resource-Id": self._config.resource_id,
            "Content-Type": "application/json",
        }

    def _payload(self, text: str, voice: str | None, speed: float | None) -> dict[str, Any]:
        return {
            "req_params": {
                "speaker": voice or self._config.voice,
                "text": text,
            },
            "audio_params": {
                "format": self._config.audio_format,
                "sample_rate": self._config.sample_rate,
                "speed_ratio": self._config.speed_ratio if speed is None else speed,
            },
        }

    async def stream_synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """逐块吐出这句话的音频字节。

        空白文本直接返回，不发请求 —— 分句器 flush 出空残句是常态。
        ``asyncio.CancelledError`` 原样向上传播；``async with`` 保证被打断时
        底层连接一定关闭，不给火山留一个还在跑的合成任务。
        """

        spoken = text.strip()
        if not spoken:
            return

        request = self._client.build_request(
            "POST",
            self._config.url,
            headers=self._headers(),
            json=self._payload(spoken, voice, speed),
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise VolcTTSRetryableError(f"volc tts transport failure: {type(exc).__name__}") from exc

        try:
            if response.status_code != httpx.codes.OK:
                # body 可能带回声的 Key 或用户文本，一律不读、不记。
                raise _status_error(response.status_code)
            async for line in response.aiter_lines():
                chunk = _decode_audio_line(line)
                if chunk:
                    yield chunk
        except httpx.HTTPError as exc:
            raise VolcTTSRetryableError(
                f"volc tts stream interrupted: {type(exc).__name__}"
            ) from exc
        finally:
            await response.aclose()


def _status_error(status_code: int) -> VolcTTSError:
    message = f"volc tts rejected the request with status {status_code}"
    if status_code in _RETRYABLE_STATUS:
        return VolcTTSRetryableError(message)
    return VolcTTSError(message)


def _decode_audio_line(line: str) -> bytes:
    """从一行 SSE 里取出音频字节；非音频行返回空。

    心跳、注释、空行与解不开的 JSON 全部忽略 —— 一句话的合成不该被一行噪声
    打断。只有「业务错误码 + 没带音频」才是真失败。
    """

    stripped = line.strip()
    if not stripped.startswith(_SSE_DATA_PREFIX):
        return b""

    body = stripped[len(_SSE_DATA_PREFIX) :].strip()
    if not body or body == "[DONE]":
        return b""

    try:
        event = json.loads(body)
    except json.JSONDecodeError:
        return b""
    if not isinstance(event, dict):
        return b""

    payload = event.get("data")
    if isinstance(payload, str) and payload:
        try:
            return base64.b64decode(payload)
        except (ValueError, TypeError):
            return b""

    code = event.get("code")
    if isinstance(code, int) and code not in _SUCCESS_CODES:
        raise VolcTTSError(f"volc tts returned business error code {code}")
    return b""
