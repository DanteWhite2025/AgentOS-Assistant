"""LLM 网关加固：超时、语言约束、token 用量采集。

这三项的共同点是「不出事时看不见，出事时全站受影响」：
- 超时走 SDK 默认 600s，一个卡住的请求能占住 worker 十分钟
- 提示词没有语言约束，换模型就可能换语言（实测 claude 会说英文旁白）
- usage 全丢，成本只能靠账单事后倒推
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from pydantic import ValidationError

from app.agents.llm import LLMMessage, LLMResult, PydanticAILLM
from app.config import Settings
from app.container import _DEFAULT_PROMPT, build_container

REAL: dict[str, object] = {
    "provider_mode": "real",
    "env": "staging",
    "realtime_voice_provider": "openai",
    "openai_api_key": "sk-real-not-logged",
    "llm_base_url": "https://newapi.example.com/v1",
    "llm_api_key": "sk-newapi-not-logged",
    "llm_model": "claude-opus-4-8-low",
    "followin_api_key": "followin-key-for-test",
    "core_actor_token": "agent-staging-token",
    "core_base_url": "https://core-staging.example.com",
    "rt_ws_base": "wss://runtime-staging.example.com",
    "redis_url": "",
}


# ---- 超时 ----


def test_llm_timeout_default_is_not_sdk_600s() -> None:
    """openai SDK 默认 read timeout 是 600 秒，对交互式对话完全不可接受。"""
    assert 0 < Settings().llm_timeout_seconds <= 180


def test_llm_timeout_is_configurable() -> None:
    assert Settings(**(REAL | {"llm_timeout_seconds": 45.0})).llm_timeout_seconds == 45.0


@pytest.mark.parametrize("bad", [0, -1, 601])
def test_llm_timeout_rejects_out_of_range(bad: float) -> None:
    with pytest.raises(ValidationError):
        Settings(**(REAL | {"llm_timeout_seconds": bad}))


async def test_container_applies_llm_timeout_to_http_client() -> None:
    container = build_container(Settings(**(REAL | {"llm_timeout_seconds": 42.0})))
    try:
        # 断言秒数而不是 Timeout 对象相等：openai 会 re-export 自己的 Timeout
        # 类型，跨版本 __eq__ 行为不一致 —— CI 上实测两个 repr 完全相同的对象
        # 判不相等，本地却相等。比较数值才是稳的。
        timeout = container.llm._model.client.timeout  # type: ignore[attr-defined]
        assert timeout.read == 42.0
        assert timeout.connect == 42.0
    finally:
        # 必须关：泄漏的 httpx 客户端会在 GC 时抛 "Event loop is closed"，
        # 把干净的测试输出弄得像出了大事。
        await container.aclose()


def test_llm_max_output_tokens_is_configurable() -> None:
    assert Settings(**(REAL | {"llm_max_output_tokens": 100})).llm_max_output_tokens == 100


async def test_container_applies_max_output_tokens_to_llm() -> None:
    """max_tokens 熔断必须真的传到 PydanticAILLM，否则形同虚设。

    实测过 claude-opus-4-8 隐藏 reasoning_tokens 能吃掉 1500~1900 token；
    默认值 4096 是留出这部分开销之外还有可见文本预算的下限，不是随手写的数字。
    """
    container = build_container(Settings(**(REAL | {"llm_max_output_tokens": 777})))
    try:
        assert container.llm._max_output_tokens == 777  # type: ignore[attr-defined]
    finally:
        await container.aclose()


async def test_llm_http_client_is_closed_by_container() -> None:
    """httpx 客户端必须交容器统一关闭，不然每次重建容器都漏一个连接池。"""
    container = build_container(Settings(**REAL))
    client = container.llm._model.client._client  # type: ignore[attr-defined]
    assert not client.is_closed
    await container.aclose()
    assert client.is_closed


# ---- 语言约束 ----


def test_default_prompt_pins_reply_language() -> None:
    """提示词自身是中文写的但从没要求用中文回答，换模型就会漏出英文旁白。"""
    assert "中文" in _DEFAULT_PROMPT


# ---- token 用量 ----


def test_llm_result_carries_usage_field() -> None:
    assert LLMResult(text="x").usage is None


async def test_pydantic_ai_llm_reports_usage() -> None:
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    def respond(messages: list, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("好的")])

    result = await PydanticAILLM(FunctionModel(respond)).complete(
        "系统提示", (LLMMessage("user", "你好"),), ()
    )
    assert result.usage is not None
    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0


# ---- 预检接进容器与 /readyz ----


async def test_container_wires_model_preflight_in_real_mode() -> None:
    container = build_container(Settings(**REAL))
    try:
        assert container.llm_model_preflight is not None
        assert container.llm_model_preflight.snapshot.model == "claude-opus-4-8-low"
    finally:
        await container.aclose()


def test_fake_mode_has_no_model_preflight() -> None:
    """fake 模式不打任何网络，也就没有预检对象。"""
    assert build_container(Settings(provider_mode="fake", env="local")).llm_model_preflight is None


async def test_readyz_is_503_when_model_is_misconfigured() -> None:
    """核心保护：模型名写错时 readyz 必须转红，否则部署会静默上线坏配置。"""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.agents.model_preflight import ModelPreflightSnapshot
    from app.api import health
    from app.api.deps import get_container

    container = build_container(Settings(**REAL))
    # 凭证就绪，只让模型预检失败 —— 隔离出预检对 readyz 的影响
    container.core_credential_readiness._snapshot = replace(  # type: ignore[attr-defined]
        container.core_credential_readiness.snapshot, status="valid", ready=True
    )
    container.llm_model_preflight._snapshot = ModelPreflightSnapshot(  # type: ignore[union-attr]
        status="model_not_found",
        ready=False,
        model="deepseek-chat",
        available_models=("claude-opus-4-8-low",),
    )
    app = FastAPI()
    app.include_router(health.router)
    app.dependency_overrides[get_container] = lambda: container
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/readyz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["llm_model"]["status"] == "model_not_found"
        assert "claude-opus-4-8-low" in body["llm_model"]["available_models"]
    finally:
        await container.aclose()


async def test_readyz_stays_green_on_transient_gateway_outage() -> None:
    """站点抖动不该把服务判死 —— 预检只跑一次，那会永久 not_ready。"""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.agents.model_preflight import ModelPreflightSnapshot
    from app.api import health
    from app.api.deps import get_container

    container = build_container(Settings(**REAL))
    container.core_credential_readiness._snapshot = replace(  # type: ignore[attr-defined]
        container.core_credential_readiness.snapshot, status="valid", ready=True
    )
    container.llm_model_preflight._snapshot = ModelPreflightSnapshot(  # type: ignore[union-attr]
        status="gateway_unavailable", ready=False, model="claude-opus-4-8-low"
    )
    app = FastAPI()
    app.include_router(health.router)
    app.dependency_overrides[get_container] = lambda: container
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["llm_model"]["status"] == "gateway_unavailable"
    finally:
        await container.aclose()
