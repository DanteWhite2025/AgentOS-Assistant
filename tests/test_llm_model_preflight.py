"""模型 ID 预检。

这是本次最贵的一课：`AGENT_LLM_MODEL` 写成站内不存在的 ID（如老名字
`deepseek-chat`）时，`Settings()` 只校验非空所以通过、容器正常启动、
`/healthz` 正常返回 ok —— 但每一次真实对话都 503。配置错误必须在
readiness 上暴露出来，而不是等用户发现聊天坏了。

同时不能因为中转站临时抖动就让服务起不来：配置错（模型不存在）要拦，
可用性问题（超时/5xx）只记录不拦启动。
"""

from __future__ import annotations

import httpx
import pytest

from app.agents.model_preflight import LLMModelPreflight

_MODELS_BODY = {
    "data": [
        {"id": "claude-opus-4-8-low"},
        {"id": "deepseek-v4-pro"},
    ]
}


def _preflight(handler, model: str = "claude-opus-4-8-low") -> LLMModelPreflight:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LLMModelPreflight(
        base_url="https://newapi.example.com/v1",
        api_key="sk-secret-never-logged",
        model=model,
        client=client,
    )


async def test_model_present_is_valid() -> None:
    snapshot = await _preflight(lambda r: httpx.Response(200, json=_MODELS_BODY)).check()
    assert snapshot.status == "valid"
    assert snapshot.ready is True


async def test_missing_model_is_reported_with_available_list() -> None:
    """报错必须带上站内实际有什么，否则运维只能靠猜。"""
    snapshot = await _preflight(
        lambda r: httpx.Response(200, json=_MODELS_BODY), model="deepseek-chat"
    ).check()
    assert snapshot.status == "model_not_found"
    assert snapshot.ready is False
    assert "claude-opus-4-8-low" in snapshot.available_models


async def test_gateway_5xx_does_not_block_startup() -> None:
    """站点抖动是可用性问题，不是配置错误 —— 不能因此判定配置无效。"""
    snapshot = await _preflight(lambda r: httpx.Response(503, text="upstream down")).check()
    assert snapshot.status == "gateway_unavailable"
    assert snapshot.ready is False


async def test_gateway_timeout_does_not_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    snapshot = await _preflight(handler).check()
    assert snapshot.status == "gateway_unavailable"


async def test_unauthorized_is_distinguished_from_missing_model() -> None:
    """401 是 key 的问题，不是模型的问题；混在一起会把运维引向错误的排查方向。"""
    snapshot = await _preflight(lambda r: httpx.Response(401, json={"error": "bad key"})).check()
    assert snapshot.status == "gateway_rejected"


async def test_api_key_never_appears_in_public_dict() -> None:
    snapshot = await _preflight(lambda r: httpx.Response(200, json=_MODELS_BODY)).check()
    assert "sk-secret-never-logged" not in str(snapshot.public_dict())


async def test_request_uses_bearer_auth_on_models_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_MODELS_BODY)

    await _preflight(handler).check()
    assert seen[0].url.path.endswith("/models")
    assert seen[0].headers["authorization"] == "Bearer sk-secret-never-logged"


async def test_malformed_payload_is_not_a_crash() -> None:
    snapshot = await _preflight(lambda r: httpx.Response(200, json={"unexpected": 1})).check()
    assert snapshot.status == "gateway_unavailable"


@pytest.mark.parametrize("body", [{"data": [{"id": None}]}, {"data": ["not-a-dict"]}])
async def test_junk_entries_are_ignored(body: dict) -> None:
    snapshot = await _preflight(lambda r: httpx.Response(200, json=body)).check()
    assert snapshot.status == "model_not_found"


# ---- readiness 语义：只拦配置错误，不拦站点抖动 ----


async def test_config_errors_block_readiness() -> None:
    missing = await _preflight(
        lambda r: httpx.Response(200, json=_MODELS_BODY), model="deepseek-chat"
    ).check()
    rejected = await _preflight(lambda r: httpx.Response(401, json={})).check()
    assert missing.blocks_readiness is True
    assert rejected.blocks_readiness is True


async def test_transient_outage_does_not_block_readiness_forever() -> None:
    """预检只在启动时跑一次；把抖动算进 readiness 会让服务永久 not_ready。"""
    snapshot = await _preflight(lambda r: httpx.Response(503, text="down")).check()
    assert snapshot.ready is False
    assert snapshot.blocks_readiness is False


async def test_valid_does_not_block_readiness() -> None:
    snapshot = await _preflight(lambda r: httpx.Response(200, json=_MODELS_BODY)).check()
    assert snapshot.blocks_readiness is False
