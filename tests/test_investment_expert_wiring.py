"""容器接线回归（T4）：投研证据客户端不再持有独立 LLM，且复用 web_search 同一个 news_store。

见 docs/plans/2026-09-02-investment-expert-evidence/TECH_DESIGN.md §4.10。
"""

from __future__ import annotations

from app.config import Settings
from app.container import build_container

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


def test_container_wires_evidence_client_without_a_second_llm_fake_mode() -> None:
    container = build_container(Settings(provider_mode="fake", env="local"))

    client = container.executor._investment_expert_client
    assert client is not None
    assert not hasattr(client, "_llm")


def test_container_wires_evidence_client_without_a_second_llm_real_mode() -> None:
    # investment_expert_llm_model 曾经会触发装配一个独立的 PydanticAILLM 实例——
    # 这里显式配了它，确保就算配置项还在，也不会再造出第二个 LLM。
    settings = Settings(
        **(REAL | {"investment_expert_llm_model": "deepseek-v4-flash-max"})
    )
    container = build_container(settings)

    client = container.executor._investment_expert_client
    assert client is not None
    assert not hasattr(client, "_llm")


def test_evidence_client_shares_the_same_news_store_as_web_search() -> None:
    container = build_container(Settings(provider_mode="fake", env="local"))

    executor = container.executor
    client = executor._investment_expert_client
    assert client._news_store is executor._news_store
