"""火山豆包语音识别（Seed ASR Flash，一句话识别）客户端契约。

全部离线：用 MockTransport 造真实的同步 JSON 响应，验证请求构建、返回解析、
重试分类与错误脱敏 —— 风格与 test_volc_tts_client.py 对齐。
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.voice.volc_asr import (
    VolcASRConfig,
    VolcASRError,
    VolcASRRetryableError,
    VolcengineASRClient,
)

API_KEY = "volc-asr-key-never-logged"
CONFIG = VolcASRConfig(
    api_key=API_KEY,
    url="https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash",
    resource_id="volc.bigasr.auc_turbo",
)
AUDIO_BYTES = b"\x00\x01\x02\x03fake-wav-bytes"


def _client(handler) -> tuple[VolcengineASRClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return VolcengineASRClient(http, CONFIG), http


def _ok(text: str, duration_ms: int = 0) -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": 0, "result": {"text": text}, "audio_info": {"duration": duration_ms}},
    )


@pytest.mark.asyncio
async def test_transcribe_returns_the_recognized_text_and_duration() -> None:
    client, http = _client(lambda _request: _ok("帮我查看一下当前比特币价格", 1250))
    async with http:
        result = await client.transcribe(AUDIO_BYTES, audio_format="wav")

    assert result.text == "帮我查看一下当前比特币价格"
    assert result.duration_ms == 1250


@pytest.mark.asyncio
async def test_transcribe_sends_the_documented_contract() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["payload"] = json.loads(request.content)
        return _ok("你好")

    client, http = _client(handler)
    async with http:
        await client.transcribe(AUDIO_BYTES, audio_format="m4a")

    assert seen["url"] == CONFIG.url
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["x-api-key"] == API_KEY
    assert headers["x-api-resource-id"] == "volc.bigasr.auc_turbo"
    assert headers["x-api-request-id"]  # 火山强制必填，每次请求必须不同
    assert seen["payload"] == {
        "audio": {
            "format": "m4a",
            "data": base64.b64encode(AUDIO_BYTES).decode("ascii"),
        }
    }


@pytest.mark.asyncio
async def test_each_request_carries_a_distinct_request_id() -> None:
    """火山按 X-Api-Request-Id 做幂等/追踪，重复用同一个值会被上游拒绝或误判重复。"""

    seen_ids: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_ids.append(request.headers["x-api-request-id"])
        return _ok("你好")

    client, http = _client(handler)
    async with http:
        await client.transcribe(AUDIO_BYTES, audio_format="wav")
        await client.transcribe(AUDIO_BYTES, audio_format="wav")

    assert len(seen_ids) == 2
    assert seen_ids[0] != seen_ids[1]


@pytest.mark.asyncio
async def test_silent_audio_returns_an_empty_transcript_not_an_error() -> None:
    client, http = _client(lambda _request: _ok(""))
    async with http:
        result = await client.transcribe(AUDIO_BYTES, audio_format="wav")

    assert result.text == ""


@pytest.mark.asyncio
async def test_business_error_code_without_text_raises_a_non_retryable_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 45000001, "message": "invalid audio"})

    client, http = _client(handler)
    async with http:
        with pytest.raises(VolcASRError) as excinfo:
            await client.transcribe(AUDIO_BYTES, audio_format="wav")

    assert not isinstance(excinfo.value, VolcASRRetryableError)


@pytest.mark.asyncio
async def test_client_error_status_is_not_retryable() -> None:
    """参数或格式写错重试一万次也还是错的。"""

    client, http = _client(lambda _request: httpx.Response(400, json={"message": "bad audio"}))
    async with http:
        with pytest.raises(VolcASRError) as excinfo:
            await client.transcribe(AUDIO_BYTES, audio_format="wav")

    assert not isinstance(excinfo.value, VolcASRRetryableError)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_server_side_status_is_retryable(status: int) -> None:
    client, http = _client(lambda _request: httpx.Response(status, json={"message": "busy"}))
    async with http:
        with pytest.raises(VolcASRRetryableError):
            await client.transcribe(AUDIO_BYTES, audio_format="wav")


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
        with pytest.raises(VolcASRRetryableError):
            await client.transcribe(AUDIO_BYTES, audio_format="wav")


@pytest.mark.asyncio
async def test_malformed_response_body_is_not_retryable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all")

    client, http = _client(handler)
    async with http:
        with pytest.raises(VolcASRError) as excinfo:
            await client.transcribe(AUDIO_BYTES, audio_format="wav")

    assert not isinstance(excinfo.value, VolcASRRetryableError)


@pytest.mark.asyncio
async def test_errors_never_carry_the_api_key_or_response_body() -> None:
    """错误对象会进日志和告警，带上 Key 或 body 就等于把凭证写进日志。"""

    client, http = _client(
        lambda _request: httpx.Response(400, json={"message": "bad", "token": API_KEY})
    )
    async with http:
        with pytest.raises(VolcASRError) as excinfo:
            await client.transcribe(AUDIO_BYTES, audio_format="wav")

    rendered = repr(excinfo.value)
    assert API_KEY not in rendered
    assert "bad" not in rendered
