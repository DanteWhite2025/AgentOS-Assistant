"""实时语音 Provider 注册表 —— 按配置分发凭证签发实现，并预留插件化扩展。

与 ``app.tools.registry.ToolRegistry`` 同构：注册表构建后只读，``with_provider``
返回新实例而不原地修改。新增一家 Provider（Gemini Live / Grok WebRTC 等）只需
写一个工厂函数并注册，容器与 API 端点都不用改。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from app.voice.config import load_volc_voice_config
from app.voice.gemini_live import GeminiLiveVoiceProvider
from app.voice.pipeline_channel import PipelineChannelVoiceProvider
from app.voice.provider import (
    FakeVoiceProvider,
    OpenAIVoiceProvider,
    VoiceProvider,
    VolcRtcVoiceProvider,
)
from app.voice.volc_openapi import VolcRtcOpenAPI

if TYPE_CHECKING:
    from app.config import Settings
    from app.tools.registry import ToolRegistry


class UnknownVoiceProviderError(ValueError):
    """配置里的 Provider 名字没有登记过 —— 启动即失败，绝不静默降级。"""


@dataclass(frozen=True)
class VoiceProviderContext:
    """工厂建 Provider 所需的全部输入。

    ``http_client`` 由容器统一创建与关闭；注册表不持有连接生命周期。
    """

    settings: Settings
    tool_registry: ToolRegistry
    instructions: str = ""
    http_client: httpx.AsyncClient | None = None


VoiceProviderFactory = Callable[[VoiceProviderContext], VoiceProvider]


def _require_client(context: VoiceProviderContext, provider: str) -> httpx.AsyncClient:
    if context.http_client is None:
        raise ValueError(f"voice provider {provider} requires an http client")
    return context.http_client


def build_fake_provider(context: VoiceProviderContext) -> VoiceProvider:
    return FakeVoiceProvider()


def build_openai_provider(context: VoiceProviderContext) -> VoiceProvider:
    return OpenAIVoiceProvider(
        context.settings.openai_api_key,
        _require_client(context, "openai"),
        instructions=context.instructions,
        registry=context.tool_registry,
        base_url=context.settings.openai_base_url,
    )


def build_gemini_live_provider(context: VoiceProviderContext) -> VoiceProvider:
    settings = context.settings
    return GeminiLiveVoiceProvider(
        settings.gemini_api_key,
        _require_client(context, "gemini_live"),
        instructions=context.instructions,
        registry=context.tool_registry,
        base_url=settings.gemini_base_url,
        model=settings.gemini_live_model,
        token_ttl_seconds=settings.gemini_token_ttl_seconds,
        session_start_window_seconds=settings.gemini_session_start_window_seconds,
    )


def build_volc_rtc_provider(context: VoiceProviderContext) -> VoiceProvider:
    settings = context.settings
    client = _require_client(context, "volc_rtc")
    voice_config = load_volc_voice_config(
        override_json=settings.volc_voice_chat_config_json,
        s2s_app_id=settings.huochan_s2s_app_id,
        s2s_token=settings.huochan_s2s_token,
        end_smooth_window_ms=settings.volc_asr_end_smooth_window_ms,
    )
    market_tools = tuple(
        context.tool_registry.get(name) for name in ("market_tickers", "get_market_price")
    )
    if any(tool is None for tool in market_tools):
        raise ValueError("market tool definitions are missing")
    return VolcRtcVoiceProvider(
        app_id=settings.rtc_app_id,
        app_key=settings.rtc_app_key,
        api=VolcRtcOpenAPI(
            access_key_id=settings.volc_access_key_id,
            secret_access_key=settings.volc_secret_access_key,
            client=client,
            endpoint=settings.volc_rtc_endpoint,
            region=settings.volc_rtc_region,
            version=settings.volc_rtc_api_version,
        ),
        token_ttl_seconds=settings.rtc_token_ttl_seconds,
        voice_config=voice_config,
        welcome_message=settings.volc_voice_welcome_message,
        callback_url=settings.volc_callback_url,
        callback_secret=settings.volc_callback_secret,
        function_callback_url=settings.volc_function_callback_url,
        function_callback_secret=settings.volc_function_callback_secret,
        ark_model=settings.volc_ark_model,
        ark_endpoint_id=settings.volc_ark_endpoint_id,
        tool_definitions=tuple(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in market_tools
            if tool is not None
        ),
        system_messages=(context.instructions,),
    )


class VoiceProviderRegistry:
    """不可变 Provider 注册表。"""

    def __init__(self, factories: Mapping[str, VoiceProviderFactory] | None = None) -> None:
        self._factories: dict[str, VoiceProviderFactory] = dict(factories or {})

    def with_provider(self, name: str, factory: VoiceProviderFactory) -> VoiceProviderRegistry:
        """返回追加了该 Provider 的新注册表（原实例不变）。"""

        return VoiceProviderRegistry({**self._factories, name: factory})

    def names(self) -> tuple[str, ...]:
        return tuple(self._factories)

    def build(self, name: str, context: VoiceProviderContext) -> VoiceProvider:
        factory = self._factories.get(name)
        if factory is None:
            raise UnknownVoiceProviderError(
                f"unknown realtime voice provider: {name}; "
                f"registered={','.join(self.names())}"
            )
        return factory(context)


def build_pipeline_provider(context: VoiceProviderContext) -> VoiceProvider:
    """pipeline 渠道只需要一个签票守卫；合成能力挂在容器的 ``voice_pipeline`` 上。"""

    return PipelineChannelVoiceProvider()


def default_voice_provider_registry() -> VoiceProviderRegistry:
    """内置五家。

    fake（自测）/ openai（海外直连，含 Azure）/ gemini_live（原生音频 WSS 直连）/
    volc_rtc（国内直连 + 降级兜底）/ pipeline（LLM 出文本 + 火山 TTS 出声音，
    音频经 Runtime 转发，不签票）。
    """

    return VoiceProviderRegistry(
        {
            "fake": build_fake_provider,
            "openai": build_openai_provider,
            "gemini_live": build_gemini_live_provider,
            "volc_rtc": build_volc_rtc_provider,
            "pipeline": build_pipeline_provider,
        }
    )
