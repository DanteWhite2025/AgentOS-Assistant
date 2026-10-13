"""统一 LLM 网关配置测试。

全部文本推理走自建 New API 中转站，代码里不再有 provider 分支：
一组 BASE_URL + API_KEY + MODEL 就是唯一出口。

三个配置在 real 模式全部必填 —— 少任何一个都会让 LLM 调用打到错误的
地址或用错模型名，与其在运行时静默失败，不如启动即失败。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.container import build_container

REAL: dict[str, Any] = {
    "provider_mode": "real",
    "env": "staging",
    "realtime_voice_provider": "openai",
    "openai_api_key": "sk-real-not-logged",
    "llm_base_url": "https://newapi.example.com/v1",
    "llm_api_key": "sk-newapi-not-logged",
    "llm_model": "deepseek-chat",
    "followin_api_key": "followin-key-for-test",
    "core_actor_token": "agent-staging-token",
    "core_base_url": "https://core-staging.example.com",
    "rt_ws_base": "wss://runtime-staging.example.com",
    "redis_url": "",
}


@pytest.mark.parametrize(
    "override",
    [
        {"llm_base_url": ""},
        {"llm_api_key": ""},
        {"llm_model": ""},
    ],
)
def test_real_mode_requires_all_gateway_settings(override: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        Settings(**(REAL | override))  # type: ignore[arg-type]


def test_gateway_settings_read_from_canonical_env_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_LLM_BASE_URL", "https://newapi.example.com/v1")
    monkeypatch.setenv("AGENT_LLM_API_KEY", "sk-newapi")
    monkeypatch.setenv("AGENT_LLM_MODEL", "claude-opus-5")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_base_url == "https://newapi.example.com/v1"
    assert settings.llm_api_key == "sk-newapi"
    assert settings.llm_model == "claude-opus-5"


def test_legacy_openrouter_env_names_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧名不再被当作别名读取：出口与配置彻底统一，旧配置直接忽略以避免配置漂移。"""
    monkeypatch.delenv("AGENT_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("AGENT_LLM_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_LLM_MODEL", raising=False)
    monkeypatch.setenv("AGENT_OPENROUTER_BASE_URL", "https://legacy.example.com/v1")
    monkeypatch.setenv("AGENT_OPENROUTER_API_KEY", "sk-legacy")
    monkeypatch.setenv("AGENT_OPENROUTER_MODEL", "legacy-model")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.llm_base_url == ""
    assert settings.llm_api_key == ""
    assert settings.llm_model == ""


def test_openrouter_fields_are_gone() -> None:
    """字段层面不留 openrouter_*：出口只有一个，配置也只有一组。"""
    fields = Settings.model_fields
    assert "openrouter_base_url" not in fields
    assert "openrouter_api_key" not in fields
    assert "openrouter_model" not in fields


def test_container_wires_llm_to_gateway() -> None:
    container = build_container(Settings(**REAL))  # type: ignore[arg-type]
    model = container.llm._model  # type: ignore[attr-defined]
    assert model.model_name == "deepseek-chat"
    assert model.base_url.rstrip("/") == "https://newapi.example.com/v1"


def test_profile_model_matches_gateway_model() -> None:
    """审计里记的模型名必须等于真正调用的模型名，否则对不上账。"""
    container = build_container(Settings(**REAL))  # type: ignore[arg-type]
    assert container.runner._profile.model == "deepseek-chat"  # type: ignore[attr-defined]
