"""火山豆包语音识别（Seed ASR Flash，一句话识别）客户端。

``auc/bigmodel/recognize/flash`` 是同步识别端点：一次 POST 一段完整音频（base64
内嵌进请求体），服务端直接在响应体里回一段文字，没有 SSE、没有分片 —— 移动端
上传的都是几秒到几十秒的短语音，等一次往返完全够用，不需要为它另起一条流式
链路。

沿用 volc_tts.py 的两条硬约束：

1. **错误必须区分可否重试。** 参数或音频格式写错（4xx）重试一万次还是错的；
   网络抖动和 5xx 才值得重试一次。上层（/audio/transcribe）按异常类型分流，
   不解析文案。
2. **错误对象只带状态码和异常类型，绝不带响应 body、URL 或 Key** —— 它们会
   进日志和告警。
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from typing import Any, Final

import httpx

# 明确可重试的 HTTP 状态：限流与服务端故障，与 volc_tts.py 保持一致。
_RETRYABLE_STATUS: Final = frozenset({408, 425, 429, 500, 502, 503, 504})
# 火山的成功业务码；非零码即使 HTTP 200 也代表识别失败（如音频不合法）。
_SUCCESS_CODE: Final = 0


class VolcASRError(RuntimeError):
    """火山 ASR 识别失败。不保存响应 body、URL 或 API Key。"""


class VolcASRRetryableError(VolcASRError):
    """网络抖动或服务端故障，值得重试一次。"""


@dataclass(frozen=True)
class VolcASRConfig:
    """一次识别请求内不变的参数。构造后只读。"""

    api_key: str
    url: str
    resource_id: str


@dataclass(frozen=True)
class VolcASRResult:
    """一次识别的结果：文字与音频时长（毫秒）。"""

    text: str
    duration_ms: int


class VolcengineASRClient:
    """把一段完整音频字节换成文字。无状态，可跨请求共用。"""

    def __init__(self, client: httpx.AsyncClient, config: VolcASRConfig) -> None:
        self._client = client
        self._config = config

    @property
    def config(self) -> VolcASRConfig:
        return self._config

    def _headers(self) -> dict[str, str]:
        return {
            "X-Api-Key": self._config.api_key,
            "X-Api-Resource-Id": self._config.resource_id,
            # 火山强制必填，缺了直接 400；每次请求必须不同，不能固定值或复用。
            "X-Api-Request-Id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }

    def _payload(self, audio_bytes: bytes, audio_format: str) -> dict[str, Any]:
        return {
            "audio": {
                "format": audio_format,
                "data": base64.b64encode(audio_bytes).decode("ascii"),
            },
        }

    async def transcribe(self, audio_bytes: bytes, *, audio_format: str) -> VolcASRResult:
        """识别一段完整音频，返回文字与时长。

        ``asyncio.CancelledError`` 原样向上传播。
        """

        request = self._client.build_request(
            "POST",
            self._config.url,
            headers=self._headers(),
            json=self._payload(audio_bytes, audio_format),
        )
        try:
            response = await self._client.send(request)
        except httpx.HTTPError as exc:
            raise VolcASRRetryableError(
                f"volc asr transport failure: {type(exc).__name__}"
            ) from exc

        try:
            if response.status_code != httpx.codes.OK:
                # body 可能带回声的 Key 或音频派生信息，一律不读、不记。
                raise _status_error(response.status_code)
            return _parse_result(response)
        finally:
            await response.aclose()


def _status_error(status_code: int) -> VolcASRError:
    message = f"volc asr rejected the request with status {status_code}"
    if status_code in _RETRYABLE_STATUS:
        return VolcASRRetryableError(message)
    return VolcASRError(message)


def _parse_result(response: httpx.Response) -> VolcASRResult:
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise VolcASRError(f"volc asr returned unparsable response: {type(exc).__name__}") from exc
    if not isinstance(body, dict):
        raise VolcASRError("volc asr returned a non-object response")

    code = body.get("code")
    if isinstance(code, int) and code != _SUCCESS_CODE:
        raise VolcASRError(f"volc asr returned business error code {code}")

    result = body.get("result")
    text = result.get("text") if isinstance(result, dict) else None
    if not isinstance(text, str):
        raise VolcASRError("volc asr response missing result.text")

    audio_info = body.get("audio_info")
    duration = audio_info.get("duration") if isinstance(audio_info, dict) else None
    duration_ms = duration if isinstance(duration, int) else 0

    return VolcASRResult(text=text, duration_ms=duration_ms)
