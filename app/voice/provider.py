"""可插拔实时语音 Provider。

默认真链路是火山 RTC；OpenAI Realtime 只保留为显式选择的兼容路径。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlsplit

import httpx

from app.voice.callback_auth import derive_callback_signature
from app.voice.volc_openapi import VolcRtcOpenAPI
from app.voice.volc_rtc_token import generate_rtc_token

OPENAI_BASE_URL = "https://api.openai.com"
CALL_URL = f"{OPENAI_BASE_URL}/v1/realtime/calls"
_AZURE_HOST_SUFFIXES = (".openai.azure.com", ".azure.com")


def _is_azure_endpoint(base_url: str) -> bool:
    """Azure OpenAI 用 `api-key` 头，不认 Bearer 形式的 API Key。"""

    host = (urlsplit(base_url).hostname or "").lower()
    return host.endswith(_AZURE_HOST_SUFFIXES)


@dataclass(frozen=True)
class VoiceCredential:
    """所有 Provider 的统一凭证出参。

    标准字段（客户端只认这几个就能建立媒体直连）：``provider`` /
    ``endpoint_url`` / ``client_secret`` 或 ``token``（统一取 ``auth_token``）/
    ``expires_at`` / ``allowed_tools``。其余字段是各家 Provider 的补充身份信息，
    缺省为 None，不参与统一契约。

    ``endpoint_url`` 是客户端建立媒体直连的入口；火山 RTC 由原生 SDK 用
    app_id/room_id/token 进房，没有客户端可见的 HTTP 入口，因此为 None。
    """

    provider: Literal["fake", "openai", "volc_rtc", "gemini_live"]
    expires_at: datetime
    endpoint_url: str | None = None
    allowed_tools: tuple[str, ...] = ()
    client_secret: str | None = None
    model: str | None = None
    call_url: str | None = None
    app_id: str | None = None
    room_id: str | None = None
    user_id: str | None = None
    token: str | None = None
    task_id: str | None = None
    bot_user_id: str | None = None
    new_session_expires_at: datetime | None = None

    @property
    def auth_token(self) -> str | None:
        """统一取密钥：OpenAI 用 client_secret，火山 RTC 用 token。"""

        return self.client_secret or self.token


@dataclass(frozen=True)
class VoiceTaskAcceptance:
    provider_request_id: str | None = None
    provider_result: str | None = None


@dataclass(frozen=True)
class VoicePresence:
    bot_joined: bool
    provider_request_id: str | None = None
    online_user_ids: set[str] = field(default_factory=set)


class VoiceProvider(Protocol):
    """实时语音 Provider 的统一契约。

    ``requires_bot_presence`` 区分两类渠道：火山 RTC 会在服务端起一个 AI Bot
    进 RTC 房间，必须轮询 presence 确认它到位；OpenAI / Gemini 是客户端直连，
    服务端根本没有 Bot 可等 —— 对它们跑进房监控只会在超时后把一通正常的通话
    误判成连接失败（线上实测过）。缺省 True 以保守兼容未声明的实现。

    新增 Provider（Gemini Live / Grok WebRTC 等）只要实现本 Protocol，
    再用 ``app.voice.registry`` 注册工厂即可接入，容器与端点无需改动。

    ``context_summary`` 用于文字 → 语音的记忆衔接：接通前把最近的文字上下文
    注入 Realtime Session，使通话一开口就接得上前面的聊天。缺省空串表示不注入。
    """

    requires_bot_presence: bool
    # ``issues_credentials`` 区分「客户端直连第三方」与「音频经 Runtime 转发」
    # 两类链路。前者必须签票；后者（02 方案流水线）根本没有票这回事，音频走
    # WebSocket 下发 —— 对它调 issue_credentials 是调用方问的问题，不是故障。
    # 缺省 True：未声明的实现按需要签票处理。
    issues_credentials: bool

    async def issue_credentials(
        self,
        session_id: str,
        user_id: str,
        tools: tuple[str, ...],
        model: str,
        now: datetime,
        attempt: int = 0,
        context_summary: str = "",
    ) -> VoiceCredential: ...

    async def start(
        self,
        session_id: str,
        user_id: str,
        attempt: int = 0,
        context_summary: str = "",
    ) -> VoiceTaskAcceptance: ...

    async def stop(self, session_id: str, user_id: str, attempt: int = 0) -> None: ...

    async def presence(self, session_id: str, user_id: str, attempt: int = 0) -> VoicePresence: ...

    async def send_function_result(
        self,
        session_id: str,
        user_id: str,
        tool_call_id: str,
        content: dict[str, Any],
        attempt: int = 0,
    ) -> VoiceTaskAcceptance: ...

    async def send_progress_message(
        self,
        session_id: str,
        user_id: str,
        message: str,
        attempt: int = 0,
    ) -> VoiceTaskAcceptance: ...


class FakeVoiceProvider:
    # 自测桩没有服务端 Bot。
    requires_bot_presence = False
    issues_credentials = True

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
        return VoiceCredential(
            provider="fake",
            client_secret="ek_fake_" + secrets.token_hex(8),
            expires_at=now + timedelta(seconds=60),
            endpoint_url=CALL_URL,
            allowed_tools=tuple(tools),
            model=model,
            call_url=CALL_URL,
        )

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
        return VoicePresence(bot_joined=True)

    async def send_function_result(
        self,
        session_id: str,
        user_id: str,
        tool_call_id: str,
        content: dict[str, Any],
        attempt: int = 0,
    ) -> VoiceTaskAcceptance:
        return VoiceTaskAcceptance()

    async def send_progress_message(
        self,
        session_id: str,
        user_id: str,
        message: str,
        attempt: int = 0,
    ) -> VoiceTaskAcceptance:
        return VoiceTaskAcceptance()


class OpenAIVoiceProvider:
    """可选 OpenAI 兼容路径，不再是 real 模式的必需依赖。"""

    # 客户端直连 Realtime，服务端不起 Bot。
    requires_bot_presence = False
    issues_credentials = True

    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient,
        instructions: str = "",
        registry: Any = None,
        base_url: str = OPENAI_BASE_URL,
    ) -> None:
        self._api_key = api_key
        self._client = client
        self._instructions = instructions
        self._registry = registry
        root = base_url.rstrip("/") or OPENAI_BASE_URL
        self._call_url = f"{root}/v1/realtime/calls"
        self._client_secrets_url = f"{root}/v1/realtime/client_secrets"
        self._is_azure = _is_azure_endpoint(root)

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
        if self._registry is not None and hasattr(self._registry, "get_function_specs"):
            formatted_tools = self._registry.get_function_specs(tools)
        else:
            formatted_tools = [
                {
                    "type": "function",
                    "name": tool,
                    "description": f"YesUP controlled tool: {tool}",
                    "parameters": {"type": "object", "additionalProperties": True},
                }
                for tool in tools
            ]
        instructions = (
            f"{self._instructions}\n\n{context_summary.strip()}"
            if context_summary.strip()
            else self._instructions
        )
        payload = {
            "session": {
                "type": "realtime",
                "model": model,
                "instructions": instructions,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {"turn_detection": {"type": "semantic_vad"}},
                    "output": {"voice": "marin"},
                },
                "tools": formatted_tools,
                "tool_choice": "auto",
            }
        }

        # Azure OpenAI 与公有 OpenAI 的鉴权头不同：Azure 用 `api-key`，把 API Key
        # 塞进 Bearer 会被直接拒。两个都发只会让上游报错更难定位，所以按端点二选一。
        auth_header = (
            {"api-key": self._api_key}
            if self._is_azure
            else {"Authorization": f"Bearer {self._api_key}"}
        )
        resp = await self._client.post(
            self._client_secrets_url,
            json=payload,
            headers={**auth_header, "OpenAI-Safety-Identifier": session_id},
        )
        resp.raise_for_status()
        data = resp.json()
        secret = data.get("value") or data.get("client_secret", "")
        if not isinstance(secret, str) or not secret:
            raise ValueError("OpenAI Realtime client secret response is missing value")
        raw_expires_at = data.get("expires_at")
        expires_at = (
            datetime.fromtimestamp(raw_expires_at, tz=UTC)
            if isinstance(raw_expires_at, (int, float))
            else now + timedelta(seconds=60)
        )
        return VoiceCredential(
            provider="openai",
            client_secret=secret,
            expires_at=expires_at,
            endpoint_url=self._call_url,
            allowed_tools=tuple(tools),
            model=model,
            call_url=self._call_url,
        )

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
        return VoicePresence(bot_joined=True)

    async def send_function_result(
        self,
        session_id: str,
        user_id: str,
        tool_call_id: str,
        content: dict[str, Any],
        attempt: int = 0,
    ) -> VoiceTaskAcceptance:
        return VoiceTaskAcceptance()

    async def send_progress_message(
        self,
        session_id: str,
        user_id: str,
        message: str,
        attempt: int = 0,
    ) -> VoiceTaskAcceptance:
        return VoiceTaskAcceptance()


class VolcRtcVoiceProvider:
    # 服务端 StartVoiceChat 起 Bot 进房，必须轮询确认到位。
    requires_bot_presence = True
    issues_credentials = True

    def __init__(
        self,
        *,
        app_id: str,
        app_key: str,
        api: VolcRtcOpenAPI,
        token_ttl_seconds: int,
        voice_config: dict[str, Any],
        welcome_message: str,
        callback_url: str,
        callback_secret: str,
        function_callback_url: str,
        function_callback_secret: str,
        ark_model: str,
        ark_endpoint_id: str,
        tool_definitions: tuple[dict[str, Any], ...],
        system_messages: tuple[str, ...],
    ) -> None:
        self._app_id = app_id
        self._app_key = app_key
        self._api = api
        self._token_ttl_seconds = token_ttl_seconds
        self._voice_config = voice_config
        self._welcome_message = welcome_message
        self._callback_url = callback_url
        self._callback_secret = callback_secret
        self._function_callback_url = function_callback_url
        self._function_callback_secret = function_callback_secret
        self._ark_model = ark_model
        self._ark_endpoint_id = ark_endpoint_id
        self._tool_definitions = tool_definitions
        self._system_messages = system_messages

    @staticmethod
    def _short(prefix: str, value: str) -> str:
        return prefix + hashlib.sha256(value.encode()).hexdigest()[:24]

    def _identity(
        self, session_id: str, user_id: str, attempt: int = 0
    ) -> tuple[str, str, str, str]:
        room_id = self._short("room_", session_id)
        rtc_user_id = self._short("user_", f"{session_id}:{user_id}")
        task_id = self._short("task_", f"{session_id}:{attempt}")
        bot_user_id = self._short("agent_", session_id)
        return room_id, rtc_user_id, task_id, bot_user_id

    def _effective_voice_config(self) -> dict[str, Any]:
        """Build the S2S request config without mutating configured secrets."""

        config = json.loads(json.dumps(self._voice_config))
        s2s_config = config.setdefault("S2SConfig", {})
        s2s_config["OutputMode"] = 1
        provider_params = s2s_config.setdefault("ProviderParams", {})
        provider_params.setdefault("asr", {"extra": {"end_smooth_window_ms": 800}})
        provider_params.setdefault(
            "tts",
            {
                "speaker": "zh_female_vv_jupiter_bigtts",
                "audio_config": {
                    "channel": 1,
                    "format": "pcm_s16le",
                    "sample_rate": 24000,
                },
            },
        )
        return config

    def safe_diagnostics(
        self, session_id: str, user_id: str, attempt: int = 0
    ) -> dict[str, object]:
        """Return configuration presence and identity checks without credentials."""

        room_id, rtc_user_id, task_id, bot_user_id = self._identity(session_id, user_id, attempt)
        callback = urlsplit(self._callback_url)
        function_callback = urlsplit(self._function_callback_url)
        effective_config = self._effective_voice_config()
        s2s_config = effective_config.get("S2SConfig")
        s2s_config = s2s_config if isinstance(s2s_config, dict) else {}
        provider_params = s2s_config.get("ProviderParams")
        provider_params = provider_params if isinstance(provider_params, dict) else {}
        s2s_app = provider_params.get("app")
        s2s_app = s2s_app if isinstance(s2s_app, dict) else {}
        s2s_dialog = provider_params.get("dialog")
        s2s_dialog = s2s_dialog if isinstance(s2s_dialog, dict) else {}
        s2s_asr = provider_params.get("asr")
        s2s_tts = provider_params.get("tts")

        def digest(value: str) -> str:
            return hashlib.sha256(value.encode()).hexdigest()[:12]

        return {
            "rtc_app_id_hash": digest(self._app_id),
            "room_hash": digest(room_id),
            "mobile_user_hash": digest(rtc_user_id),
            "bot_user_hash": digest(bot_user_id),
            "task_hash": digest(task_id),
            "mobile_and_bot_differ": rtc_user_id != bot_user_id,
            "s2s_configured": bool(s2s_config),
            "s2s_app_id_configured": bool(s2s_app.get("appid")),
            "s2s_token_configured": bool(s2s_app.get("token")),
            "s2s_dialog_configured": bool(s2s_dialog),
            "s2s_asr_configured": isinstance(s2s_asr, dict),
            "s2s_tts_configured": isinstance(s2s_tts, dict),
            "standalone_asr_configured": isinstance(effective_config.get("ASRConfig"), dict),
            "standalone_tts_configured": isinstance(effective_config.get("TTSConfig"), dict),
            "llm_mode": "ArkV3",
            "ark_model": self._ark_model,
            "callback": f"{callback.scheme}://{callback.netloc}{callback.path}",
            "callback_signature_configured": bool(self._callback_secret),
            "function_callback": (
                f"{function_callback.scheme}://{function_callback.netloc}{function_callback.path}"
            ),
            "function_callback_signature_configured": bool(self._function_callback_secret),
            "provider_entitlement_verified": False,
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
        room_id, rtc_user_id, task_id, bot_user_id = self._identity(session_id, user_id, attempt)
        expires_at = now + timedelta(seconds=self._token_ttl_seconds)
        token = generate_rtc_token(
            app_id=self._app_id,
            app_key=self._app_key,
            room_id=room_id,
            user_id=rtc_user_id,
            expires_at=expires_at,
            issued_at=now,
        )
        return VoiceCredential(
            provider="volc_rtc",
            expires_at=expires_at,
            # 火山 RTC 由原生 SDK 用 app_id/room_id/token 进房，无客户端可见 HTTP 入口。
            endpoint_url=None,
            allowed_tools=tuple(tools),
            app_id=self._app_id,
            room_id=room_id,
            user_id=rtc_user_id,
            token=token,
            task_id=task_id,
            bot_user_id=bot_user_id,
        )

    async def start(
        self,
        session_id: str,
        user_id: str,
        attempt: int = 0,
        context_summary: str = "",
    ) -> VoiceTaskAcceptance:
        room_id, rtc_user_id, task_id, bot_user_id = self._identity(session_id, user_id, attempt)
        config = self._effective_voice_config()
        # VoiceChatRoleForRTC obtains a temporary Ark key for this exact custom
        # endpoint. Preset `ep-m-*` IDs and an empty value are rejected later by
        # Ark GetApiKey even when StartVoiceChat was accepted synchronously.
        config["LLMConfig"] = {
            "Mode": "ArkV3",
            "ModelName": self._ark_model,
            "EndPointId": self._ark_endpoint_id,
            # 文字 → 语音衔接：把最近的文字上下文追加成一条额外 system message，
            # 配置的基础人设永远排在前面，绝不被通话上下文覆盖。
            "SystemMessages": [
                *self._system_messages,
                *([context_summary.strip()] if context_summary.strip() else []),
            ],
            "Tools": list(self._tool_definitions),
        }
        config["FunctionCallingConfig"] = {
            "ServerMessageUrl": (
                f"{self._function_callback_url}?session_id={quote(session_id, safe='')}"
            ),
            "ServerMessageSignature": derive_callback_signature(
                self._function_callback_secret,
                purpose="function",
                session_id=session_id,
                task_id=task_id,
            ),
        }
        response = await self._api.call(
            "StartVoiceChat",
            {
                "AppId": self._app_id,
                "RoomId": room_id,
                "TaskId": task_id,
                "Config": config,
                "AgentConfig": {
                    "TargetUserId": [rtc_user_id],
                    "UserId": bot_user_id,
                    "WelcomeMessage": self._welcome_message,
                    "EnableConversationStateCallback": True,
                    "ServerMessageURLForRTS": (
                        f"{self._callback_url}?session_id={quote(session_id, safe='')}"
                    ),
                    # 火山原样放入回调 payload 的 signature 字段；Runtime
                    # 使用常量时间比较验证，绝不记录此值。
                    "ServerMessageSignatureForRTS": derive_callback_signature(
                        self._callback_secret,
                        purpose="state",
                        session_id=session_id,
                        task_id=task_id,
                    ),
                },
            },
        )
        metadata = response.get("ResponseMetadata")
        request_id = metadata.get("RequestId") if isinstance(metadata, dict) else None
        raw_result = response.get("Result")
        if isinstance(raw_result, dict):
            raw_result = raw_result.get("Status") or raw_result.get("Code") or "accepted"
        result = str(raw_result)[:64] if raw_result is not None else "accepted"
        return VoiceTaskAcceptance(
            provider_request_id=str(request_id)[:128] if request_id is not None else None,
            provider_result=result,
        )

    async def stop(self, session_id: str, user_id: str, attempt: int = 0) -> None:
        room_id, _, task_id, _ = self._identity(session_id, user_id, attempt)
        await self._api.call(
            "StopVoiceChat",
            {"AppId": self._app_id, "RoomId": room_id, "TaskId": task_id},
        )

    async def presence(self, session_id: str, user_id: str, attempt: int = 0) -> VoicePresence:
        room_id, _, _, bot_user_id = self._identity(session_id, user_id, attempt)
        response = await self._api.query(
            "GetRoomOnlineUsers",
            {"AppId": self._app_id, "RoomId": room_id, "Property": "false"},
        )
        result = response.get("Result")
        result = result if isinstance(result, dict) else {}

        def user_ids(value: object) -> set[str]:
            if not isinstance(value, list):
                return set()
            ids: set[str] = set()
            for item in value:
                if isinstance(item, str):
                    ids.add(item)
                elif isinstance(item, dict):
                    raw_id = item.get("UserId") or item.get("UserID")
                    if isinstance(raw_id, str):
                        ids.add(raw_id)
            return ids

        online_users = user_ids(result.get("VisibleUserList")) | user_ids(
            result.get("InvisibleUserList")
        )
        metadata = response.get("ResponseMetadata")
        request_id = metadata.get("RequestId") if isinstance(metadata, dict) else None
        return VoicePresence(
            bot_joined=bot_user_id in online_users,
            provider_request_id=str(request_id)[:128] if request_id is not None else None,
            online_user_ids=online_users,
        )

    async def send_function_result(
        self,
        session_id: str,
        user_id: str,
        tool_call_id: str,
        content: dict[str, Any],
        attempt: int = 0,
    ) -> VoiceTaskAcceptance:
        room_id, _, task_id, _ = self._identity(session_id, user_id, attempt)
        message = json.dumps(
            {
                "ToolCallID": tool_call_id,
                "Content": json.dumps(content, ensure_ascii=False, separators=(",", ":")),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        response = await self._api.call(
            "UpdateVoiceChat",
            {
                "AppId": self._app_id,
                "RoomId": room_id,
                "TaskId": task_id,
                "Command": "function",
                "Message": message,
            },
        )
        metadata = response.get("ResponseMetadata")
        request_id = metadata.get("RequestId") if isinstance(metadata, dict) else None
        return VoiceTaskAcceptance(
            provider_request_id=str(request_id)[:128] if request_id is not None else None
        )

    async def send_progress_message(
        self,
        session_id: str,
        user_id: str,
        message: str,
        attempt: int = 0,
    ) -> VoiceTaskAcceptance:
        room_id, _, task_id, _ = self._identity(session_id, user_id, attempt)
        response = await self._api.call(
            "UpdateVoiceChat",
            {
                "AppId": self._app_id,
                "RoomId": room_id,
                "TaskId": task_id,
                "Command": "ExternalTextToSpeech",
                "Message": message[:200],
                "InterruptMode": 0,
            },
        )
        metadata = response.get("ResponseMetadata")
        request_id = metadata.get("RequestId") if isinstance(metadata, dict) else None
        return VoiceTaskAcceptance(
            provider_request_id=str(request_id)[:128] if request_id is not None else None
        )
