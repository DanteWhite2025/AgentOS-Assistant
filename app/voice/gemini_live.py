"""Gemini Live v1beta 原生音频 Provider（客户端 WSS 直连）。

Runtime 只签发单次、短期、受约束的 ephemeral token。Google SDK 把约束暴露为
``liveConnectConstraints``，当前 v1beta REST wire 实际接收
``bidiGenerateContentSetup``。模型、AUDIO 输出、转写、system instruction 与
受控工具白名单全部锁进票据；长期 API Key 永不下发。数据面是 Gemini v1beta
Constrained WebSocket，不经过 Runtime。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from typing import Any

import httpx

from app.voice.provider import VoiceCredential, VoicePresence, VoiceTaskAcceptance

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com"
GEMINI_LIVE_MODEL = "gemini-3.1-flash-live-preview"
GEMINI_LIVE_VOICE_NAME = "Leda"
GEMINI_CONTRACT_VERSION = "v1beta"
CONSTRAINED_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService."
    "BidiGenerateContentConstrained"
)

# 服务端权威白名单：即使 Session scope 或客户端传入更多工具，也不会被签进
# Gemini Token。这里只放行 Core 受控工具集 —— 行情、余额、持仓，外加
# create_trade_draft（只生成待确认意图，不确认/不签名/不广播）。旧 delegated
# trading readiness 不再作为用户钱包直签意图的前置工具。AgentOS 直连
# 写工具（agentos_* / create_perp_trade_draft / create_spot_swap_draft）走的是
# token 派生身份，与语音会话的 user 身份语义不同，一律不上票。
GEMINI_LIVE_ALLOWED_TOOLS = frozenset(
    {
        "get_market_price",
        "market_tickers",
        "get_balances",
        "get_positions",
        "create_trade_draft",
        "create_spot_swap_draft",
    }
)

DEFAULT_TOKEN_TTL_SECONDS = 1800
DEFAULT_SESSION_START_WINDOW_SECONDS = 60


def _audio_generation_config() -> dict[str, Any]:
    """Return the single AgentOS Gemini voice contract used for every session."""

    return {
        "responseModalities": ["AUDIO"],
        "speechConfig": {
            "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": GEMINI_LIVE_VOICE_NAME}}
        },
    }


# Gemini 的 functionDeclarations 用的是 OpenAPI 子集，不是完整 JSON Schema。
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {"additionalProperties", "$schema", "$id", "definitions", "$defs"}
)


class GeminiLiveProviderError(RuntimeError):
    """安全的 Gemini 控制面错误；不保存响应 body、URL、Key 或 Token。"""

    def __init__(self, upstream_status: int | None, reason: str, *, retryable: bool) -> None:
        super().__init__(reason)
        self.upstream_status = upstream_status
        self.reason = reason
        self.retryable = retryable


def _rfc3339(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_schema(value: Any) -> Any:
    """递归剔除 Gemini 不认的 JSON Schema 关键字（原对象不变）。"""

    if isinstance(value, Mapping):
        return {
            key: _sanitize_schema(item)
            for key, item in value.items()
            if key not in _UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_schema(item) for item in value]
    return value


def to_gemini_function_declarations(
    specs: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """OpenAI 风格的 function spec → Gemini functionDeclarations。"""

    declarations: list[dict[str, Any]] = []
    for spec in specs:
        name = str(spec.get("name") or "").strip()
        if not name:
            continue
        declarations.append(
            {
                "name": name,
                "description": str(spec.get("description") or f"YesUP controlled tool: {name}"),
                "parameters": _sanitize_schema(
                    spec.get("parameters") or {"type": "object", "properties": {}}
                ),
            }
        )
    return declarations


class GeminiLiveVoiceProvider:
    """签发短期票让客户端直连 Gemini Live；服务端不持有会话。"""

    # 客户端直连 Live 会话，服务端不起 Bot；不能跑进房监控。
    requires_bot_presence = False
    issues_credentials = True

    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient,
        *,
        instructions: str = "",
        registry: Any = None,
        base_url: str = GEMINI_BASE_URL,
        model: str = GEMINI_LIVE_MODEL,
        token_ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
        session_start_window_seconds: int = DEFAULT_SESSION_START_WINDOW_SECONDS,
    ) -> None:
        if model != GEMINI_LIVE_MODEL:
            raise ValueError(f"unsupported Gemini Live model: {model}")
        if base_url.rstrip("/") != GEMINI_BASE_URL:
            raise ValueError("Gemini Live must use the official Google v1beta endpoint")
        self._api_key = api_key
        self._client = client
        self._instructions = instructions
        self._registry = registry
        self._auth_tokens_url = f"{base_url.rstrip('/') or GEMINI_BASE_URL}/v1beta/auth_tokens"
        self._model = model
        self._token_ttl_seconds = token_ttl_seconds
        self._session_start_window_seconds = session_start_window_seconds

    def _function_declarations(self, tools: tuple[str, ...]) -> list[dict[str, Any]]:
        if not tools:
            return []
        if self._registry is not None and hasattr(self._registry, "get_function_specs"):
            specs = self._registry.get_function_specs(tools)
        else:
            specs = [
                {"name": tool, "description": f"YesUP controlled tool: {tool}"}
                for tool in tools
            ]
        return to_gemini_function_declarations(specs)

    @staticmethod
    def _approved_tools(tools: tuple[str, ...]) -> tuple[str, ...]:
        """Session scope 与服务端受控白名单取交集，保持顺序并去重。"""

        return tuple(dict.fromkeys(tool for tool in tools if tool in GEMINI_LIVE_ALLOWED_TOOLS))

    def _locked_bidi_setup(
        self, tools: tuple[str, ...], context_summary: str
    ) -> dict[str, Any]:
        """构造 v1beta Auth Token REST wire 中由上游强制执行的 setup。"""

        instructions = (
            f"{self._instructions}\n\n{context_summary.strip()}"
            if context_summary.strip()
            else self._instructions
        )
        setup: dict[str, Any] = {
            "model": f"models/{self._model}",
            "generationConfig": _audio_generation_config(),
            # 双向转写用于 Mobile partial/final 字幕与会话证据链。
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
        }
        if instructions:
            setup["systemInstruction"] = {"parts": [{"text": instructions}]}
        declarations = self._function_declarations(tools)
        if declarations:
            # 空的 functionDeclarations 会被上游拒，没有工具就整个字段不发。
            setup["tools"] = [{"functionDeclarations": declarations}]
        return setup

    def client_setup(self) -> dict[str, Any]:
        """Constrained WSS 握手的最小 BidiGenerateContentSetup。"""

        return {
            "setup": {
                "model": f"models/{self._model}",
                "generationConfig": _audio_generation_config(),
            }
        }

    async def issue_credentials(
        self,
        session_id: str,
        user_id: str,
        tools: tuple[str, ...],
        model: str,
        now: datetime,
        attempt: int = 0,
        context_summary: str = "",
    ) -> VoiceCredential:
        approved_tools = self._approved_tools(tools)
        expires_at = now + timedelta(seconds=self._token_ttl_seconds)
        new_session_expires_at = now + timedelta(seconds=self._session_start_window_seconds)
        try:
            response = await self._client.post(
                self._auth_tokens_url,
                headers={"x-goog-api-key": self._api_key},
                json={
                    "uses": 1,
                    "expireTime": _rfc3339(expires_at),
                    # 拿到票要立刻连；开新会话的窗口故意留得很短。
                    "newSessionExpireTime": _rfc3339(new_session_expires_at),
                    "bidiGenerateContentSetup": self._locked_bidi_setup(
                        approved_tools, context_summary
                    ),
                },
            )
        except httpx.HTTPError as exc:
            raise GeminiLiveProviderError(
                None, "auth_token_upstream_unavailable", retryable=True
            ) from exc
        if response.status_code >= 400:
            status = response.status_code
            reason = {
                401: "auth_token_rejected",
                409: "auth_token_conflict",
                429: "auth_token_rate_limited",
                503: "auth_token_upstream_unavailable",
            }.get(status, "auth_token_upstream_error")
            raise GeminiLiveProviderError(
                status,
                reason,
                retryable=status in {409, 429, 500, 502, 503, 504},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise GeminiLiveProviderError(
                502, "auth_token_invalid_response", retryable=True
            ) from exc
        token = payload.get("name") if isinstance(payload, Mapping) else None
        if not isinstance(token, str) or not token:
            raise GeminiLiveProviderError(502, "auth_token_missing_name", retryable=True)
        return VoiceCredential(
            provider="gemini_live",
            client_secret=token,
            expires_at=expires_at,
            new_session_expires_at=new_session_expires_at,
            # 短期票是密钥，绝不拼进 URL（URL 会进日志与崩溃上报）。
            # 客户端自行拼 `?access_token=<client_secret>`。
            endpoint_url=CONSTRAINED_WS_URL,
            allowed_tools=approved_tools,
            model=self._model,
        )

    # ---- 以下都是 no-op：会话由客户端直连持有，服务端没有可控任务 ----

    async def start(
        self,
        session_id: str,
        user_id: str,
        attempt: int = 0,
        context_summary: str = "",
    ) -> VoiceTaskAcceptance:
        return VoiceTaskAcceptance()

    async def stop(self, session_id: str, user_id: str, attempt: int = 0) -> None:
        return None

    async def presence(self, session_id: str, user_id: str, attempt: int = 0) -> VoicePresence:
        # Runtime 无法观察 Mobile 持有的 WSS，不能把 no-op 当成 setupComplete 证据。
        return VoicePresence(bot_joined=False)

    async def send_function_result(
        self,
        session_id: str,
        user_id: str,
        tool_call_id: str,
        content: dict[str, Any],
        attempt: int = 0,
    ) -> VoiceTaskAcceptance:
        # 工具结果由客户端经自己那条 WS 回灌，服务端只负责风控执行。
        return VoiceTaskAcceptance()

    async def send_progress_message(
        self,
        session_id: str,
        user_id: str,
        message: str,
        attempt: int = 0,
    ) -> VoiceTaskAcceptance:
        return VoiceTaskAcceptance()
