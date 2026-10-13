"""POST /v1/agent/audio/transcribe：上传短语音换文字。

覆盖成功识别、入参校验（空文件/超限/不支持格式）、鉴权、上游受控重试与最终
502 降级——不真打火山网络，asr_client 全部替换成可控的假实现。
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from app.config import Settings
from app.container import Container, build_container
from app.main import create_app
from app.voice.volc_asr import VolcASRError, VolcASRResult, VolcASRRetryableError

USER = {"Authorization": "Bearer test-user:u1"}
_WAV_BYTES = b"RIFF....WAVEfmt fake-audio-bytes"


class _FakeASRClient:
    def __init__(self, result: VolcASRResult) -> None:
        self.result = result
        self.calls = 0

    async def transcribe(self, audio_bytes: bytes, *, audio_format: str) -> VolcASRResult:
        self.calls += 1
        return self.result


class _FailingASRClient:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def transcribe(self, audio_bytes: bytes, *, audio_format: str) -> VolcASRResult:
        self.calls += 1
        raise self.error


class _FlakyASRClient:
    """第一次抛可重试错误，第二次成功——验证「受控重试 1 次」。"""

    def __init__(self, result: VolcASRResult) -> None:
        self.result = result
        self.calls = 0

    async def transcribe(self, audio_bytes: bytes, *, audio_format: str) -> VolcASRResult:
        self.calls += 1
        if self.calls == 1:
            raise VolcASRRetryableError("volc asr transport failure: ConnectError")
        return self.result


def _app() -> tuple[FastAPI, Container]:
    container = build_container(Settings(redis_url=""))
    app = create_app()
    app.state.container = container
    return app, container


async def _post_audio(
    client: httpx.AsyncClient,
    *,
    filename: str = "clip.m4a",
    content: bytes = _WAV_BYTES,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    files = {"file": (filename, content, "application/octet-stream")}
    return await client.post(
        "/v1/agent/audio/transcribe", files=files, headers=headers if headers is not None else USER
    )


@pytest.mark.asyncio
async def test_valid_audio_returns_the_transcript() -> None:
    app, container = _app()
    container.asr_client = _FakeASRClient(  # type: ignore[assignment]
        VolcASRResult(text="帮我查看一下当前比特币价格和我的持仓", duration_ms=1250)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await _post_audio(client)

    assert response.status_code == 200
    body = response.json()
    assert body["transcript"] == "帮我查看一下当前比特币价格和我的持仓"
    assert body["duration_ms"] == 1250
    assert body["trace_id"].startswith("trc_")


@pytest.mark.asyncio
async def test_missing_authorization_is_rejected() -> None:
    app, container = _app()
    container.asr_client = _FakeASRClient(VolcASRResult(text="x", duration_ms=1))  # type: ignore[assignment]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await _post_audio(client, headers={})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_empty_file_is_rejected_as_invalid_request() -> None:
    app, container = _app()
    container.asr_client = _FakeASRClient(VolcASRResult(text="x", duration_ms=1))  # type: ignore[assignment]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await _post_audio(client, content=b"")

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_unsupported_extension_is_rejected() -> None:
    app, container = _app()
    container.asr_client = _FakeASRClient(VolcASRResult(text="x", duration_ms=1))  # type: ignore[assignment]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await _post_audio(client, filename="clip.txt")

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_oversized_file_is_rejected() -> None:
    app, container = _app()
    container.asr_client = _FakeASRClient(VolcASRResult(text="x", duration_ms=1))  # type: ignore[assignment]
    oversized = b"0" * (10 * 1024 * 1024 + 1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await _post_audio(client, content=oversized)

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_asr_client_is_never_called_when_validation_fails() -> None:
    app, container = _app()
    fake = _FakeASRClient(VolcASRResult(text="x", duration_ms=1))
    container.asr_client = fake  # type: ignore[assignment]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _post_audio(client, content=b"")

    assert fake.calls == 0


@pytest.mark.asyncio
async def test_retryable_upstream_failure_is_retried_once_then_returns_502() -> None:
    app, container = _app()
    failing = _FailingASRClient(VolcASRRetryableError("volc asr rejected the request with status 503"))
    container.asr_client = failing  # type: ignore[assignment]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await _post_audio(client)

    assert response.status_code == 502
    assert response.json()["code"] == "INTERNAL"
    assert failing.calls == 2  # 首次 + 受控重试 1 次


@pytest.mark.asyncio
async def test_retry_recovers_and_returns_200() -> None:
    app, container = _app()
    flaky = _FlakyASRClient(VolcASRResult(text="重试后成功", duration_ms=800))
    container.asr_client = flaky  # type: ignore[assignment]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await _post_audio(client)

    assert response.status_code == 200
    assert response.json()["transcript"] == "重试后成功"
    assert flaky.calls == 2


@pytest.mark.asyncio
async def test_non_retryable_upstream_failure_is_not_retried() -> None:
    app, container = _app()
    failing = _FailingASRClient(VolcASRError("volc asr rejected the request with status 400"))
    container.asr_client = failing  # type: ignore[assignment]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await _post_audio(client)

    assert response.status_code == 502
    assert failing.calls == 1


@pytest.mark.asyncio
async def test_upstream_failure_message_never_leaks_the_api_key() -> None:
    app, container = _app()
    secret = "volc-asr-key-never-logged"
    failing = _FailingASRClient(VolcASRError(f"leaked {secret}"))
    container.asr_client = failing  # type: ignore[assignment]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await _post_audio(client)

    assert secret not in response.text
