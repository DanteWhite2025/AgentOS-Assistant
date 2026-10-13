"""Gemini Live 原生音频 Provider。

链路形态与 OpenAI Realtime 同类（客户端直连、音频不过 Runtime），但有两处
实测出来的差异，测试按这两点设防：

1. 传输是 WebSocket 的 **BidiGenerateContentConstrained**（v1beta）。
   把短期票喂给标准的 BidiGenerateContent 会被拒成「unregistered callers」——
   看起来像凭证问题，其实是端点选错了。
2. Constrained 端点**强制执行签票时锁定的 setup**：实测客户端篡改
   systemInstruction 无效。所以人设与工具白名单锁在票里，客户端改不了。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.api.agent import _gemini_provider_error_response, _voice_schema
from app.config import Settings, get_settings
from app.tools.builtin import build_default_registry
from app.voice.gemini_live import (
    CONSTRAINED_WS_URL,
    GEMINI_LIVE_MODEL,
    GEMINI_LIVE_VOICE_NAME,
    GeminiLiveProviderError,
    GeminiLiveVoiceProvider,
    to_gemini_function_declarations,
)
from app.voice.registry import VoiceProviderContext, default_voice_provider_registry

_NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
_TOKEN = "auth_tokens/3d15c67736f7ee84ba983748f5d8c9964c1115ec9799e59079afdc4b2cd4a838"


def _provider(
    client: httpx.AsyncClient, **overrides
) -> GeminiLiveVoiceProvider:
    kwargs: dict = {
        "api_key": "gemini-key-never-send-to-client",
        "client": client,
        "instructions": "必须始终使用简体中文回答。",
        "registry": build_default_registry(),
        "model": GEMINI_LIVE_MODEL,
    }
    kwargs.update(overrides)
    return GeminiLiveVoiceProvider(**kwargs)


def _mint_ok(captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"name": _TOKEN})

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------
# 1. 工具 schema 转换
# --------------------------------------------------------------------------


def test_function_declarations_drop_keywords_gemini_rejects() -> None:
    """实测：parameters 里带 additionalProperties 会让签票直接 400。"""

    specs = [
        {
            "type": "function",
            "name": "market_tickers",
            "description": "查询行情",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "filter": {
                        "type": "object",
                        "properties": {"venue": {"type": "string"}},
                        "additionalProperties": False,
                    },
                },
                "required": ["symbols"],
                "additionalProperties": False,
                "$schema": "https://json-schema.org/draft/2020-12/schema",
            },
        }
    ]
    declarations = to_gemini_function_declarations(specs)

    assert declarations == [
        {
            "name": "market_tickers",
            "description": "查询行情",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "filter": {
                        "type": "object",
                        "properties": {"venue": {"type": "string"}},
                    },
                },
                "required": ["symbols"],
            },
        }
    ]


def test_function_declarations_survive_the_real_registry() -> None:
    """全量内置工具转换后不得残留 Gemini 不认的关键字。"""

    registry = build_default_registry()
    declarations = to_gemini_function_declarations(registry.get_function_specs())

    assert len(declarations) == len(registry.all_tools())
    rendered = json.dumps(declarations)
    assert "additionalProperties" not in rendered
    assert "$schema" not in rendered
    assert all(d["name"] and d["description"] for d in declarations)


# --------------------------------------------------------------------------
# 2. 凭证签发
# --------------------------------------------------------------------------


async def test_issue_credentials_returns_the_standard_shape() -> None:
    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_mint_ok(captured)) as client:
        cred = await _provider(client).issue_credentials(
            "sess_g",
            "u1",
            ("market_tickers", "get_positions", "create_perp_trade_draft"),
            "ignored-model",
            _NOW,
        )

    assert cred.provider == "gemini_live"
    assert cred.client_secret == _TOKEN
    assert cred.auth_token == _TOKEN
    # create_perp_trade_draft 不在受控白名单内，必须被服务端滤掉。
    assert cred.allowed_tools == ("market_tickers", "get_positions")
    assert cred.model == GEMINI_LIVE_MODEL
    assert cred.expires_at == datetime(2026, 8, 23, 12, 30, tzinfo=UTC)
    assert cred.new_session_expires_at == datetime(2026, 8, 23, 12, 1, tzinfo=UTC)
    assert cred.endpoint_url == CONSTRAINED_WS_URL
    assert cred.endpoint_url.startswith("wss://")


async def test_endpoint_url_never_embeds_the_token() -> None:
    """短期票是密钥，URL 会进日志/崩溃上报，绝不能拼进去。"""

    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_mint_ok(captured)) as client:
        cred = await _provider(client).issue_credentials("sess_g", "u1", (), "", _NOW)

    assert "access_token" not in (cred.endpoint_url or "")
    assert _TOKEN not in (cred.endpoint_url or "")


async def test_mint_request_locks_model_modality_and_transcription() -> None:
    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_mint_ok(captured)) as client:
        await _provider(client).issue_credentials("sess_g", "u1", (), "", _NOW)

    assert str(captured[0].url) == (
        "https://generativelanguage.googleapis.com/v1beta/auth_tokens"
    )
    assert captured[0].headers["x-goog-api-key"] == "gemini-key-never-send-to-client"
    body = json.loads(captured[0].content)
    assert body["uses"] == 1
    assert body["expireTime"] == "2026-08-23T12:30:00Z"
    # 拿到票要立刻连；开新会话的窗口故意留得很短。
    assert body["newSessionExpireTime"] == "2026-08-23T12:01:00Z"
    assert "liveConnectConstraints" not in body
    setup = body["bidiGenerateContentSetup"]
    assert setup["model"] == f"models/{GEMINI_LIVE_MODEL}"
    assert setup["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert setup["generationConfig"]["speechConfig"] == {
        "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": GEMINI_LIVE_VOICE_NAME}}
    }
    # 双向转写：现有 /history 字幕证据链依赖它。
    assert setup["inputAudioTranscription"] == {}
    assert setup["outputAudioTranscription"] == {}


async def test_mint_request_locks_the_persona_and_tool_whitelist() -> None:
    """Constrained 端点会强制执行这份 setup，客户端改不了 —— 风控的落点。"""

    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_mint_ok(captured)) as client:
        await _provider(client).issue_credentials(
            "sess_g",
            "u1",
            (
                "market_tickers",
                "get_market_price",
                "get_balances",
                "get_positions",
                "create_trade_draft",
            ),
            "",
            _NOW,
        )

    setup = json.loads(captured[0].content)["bidiGenerateContentSetup"]
    assert setup["systemInstruction"]["parts"][0]["text"].startswith(
        "必须始终使用简体中文回答。"
    )
    names = {
        d["name"] for tool in setup["tools"] for d in tool["functionDeclarations"]
    }
    # Session scope 还要与 Runtime 受控白名单取交集，客户端不能扩大。
    assert names == {
        "market_tickers",
        "get_market_price",
        "get_balances",
        "get_positions",
        "create_trade_draft",
    }


async def test_mint_request_rejects_tools_outside_the_controlled_whitelist() -> None:
    """白名单是服务端权威边界：AgentOS 直连写工具与记忆工具都不许上票。"""

    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_mint_ok(captured)) as client:
        cred = await _provider(client).issue_credentials(
            "sess_g",
            "u1",
            (
                "agentos_create_trade_draft",
                "create_perp_trade_draft",
                "agentos_get_balances",
                "agentos_get_positions",
                "get_orders",
            ),
            "",
            _NOW,
        )

    assert cred.allowed_tools == ()
    assert "tools" not in json.loads(captured[0].content)["bidiGenerateContentSetup"]


async def test_controlled_tool_declarations_carry_the_registry_schema() -> None:
    """受控写工具要带上真实参数 schema，否则模型没法起草交易。"""

    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_mint_ok(captured)) as client:
        await _provider(client).issue_credentials(
            "sess_g",
            "u1",
            ("create_trade_draft", "get_trading_readiness", "get_balances"),
            "",
            _NOW,
        )

    setup = json.loads(captured[0].content)["bidiGenerateContentSetup"]
    declarations = {
        d["name"]: d for tool in setup["tools"] for d in tool["functionDeclarations"]
    }
    assert set(declarations) == {
        "create_trade_draft",
        "get_balances",
    }
    draft_params = declarations["create_trade_draft"]["parameters"]
    draft_description = declarations["create_trade_draft"]["description"]
    assert "唯一入口" in draft_description
    assert "trade_intent_id" in draft_description
    assert "PENDING_USER" in draft_description
    assert draft_params["required"] == ["symbol", "side", "order_type", "price", "leverage"]
    assert "leverage" in draft_params["properties"]
    # Gemini 的 OpenAPI 子集不认 additionalProperties，必须已被剔除。
    assert "additionalProperties" not in draft_params
    assert "additionalProperties" not in declarations["get_balances"]["parameters"]
    # 旧 delegated readiness 即使出现在 Session scope 中，也不得签进
    # 用户钱包直签流程的 Gemini Token。
    assert "get_trading_readiness" not in declarations


async def test_mint_request_injects_the_recent_context() -> None:
    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_mint_ok(captured)) as client:
        await _provider(client).issue_credentials(
            "sess_g",
            "u1",
            (),
            "",
            _NOW,
            context_summary="<recent_context>\n- 用户刚用文字问过 BTC 仓位\n</recent_context>",
        )

    text = json.loads(captured[0].content)["bidiGenerateContentSetup"][
        "systemInstruction"
    ]["parts"][0]["text"]
    assert text.startswith("必须始终使用简体中文回答。")
    assert "用户刚用文字问过 BTC 仓位" in text


async def test_no_tools_key_when_the_session_scope_is_empty() -> None:
    """空的 functionDeclarations 会被上游拒；没有工具就整个字段不发。"""

    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_mint_ok(captured)) as client:
        await _provider(client).issue_credentials("sess_g", "u1", (), "", _NOW)

    assert "tools" not in json.loads(captured[0].content)["bidiGenerateContentSetup"]


# --------------------------------------------------------------------------
# 3. 失败路径
# --------------------------------------------------------------------------


async def test_mint_failure_surfaces_as_a_safe_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "quota"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GeminiLiveProviderError) as caught:
            await _provider(client).issue_credentials("sess_g", "u1", (), "", _NOW)
    assert caught.value.upstream_status == 429
    assert caught.value.reason == "auth_token_rate_limited"
    assert caught.value.retryable is True


async def test_mint_response_without_a_token_name_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(GeminiLiveProviderError) as caught:
            await _provider(client).issue_credentials("sess_g", "u1", (), "", _NOW)
    assert caught.value.reason == "auth_token_missing_name"


async def test_api_key_never_leaks_into_the_credential() -> None:
    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_mint_ok(captured)) as client:
        cred = await _provider(client).issue_credentials("sess_g", "u1", (), "", _NOW)

    assert "gemini-key-never-send-to-client" not in json.dumps(
        {k: v for k, v in vars(cred).items() if isinstance(v, str | tuple | type(None))},
        default=str,
    )


# --------------------------------------------------------------------------
# 4. 生命周期：客户端直连，服务端没有可控任务
# --------------------------------------------------------------------------


async def test_lifecycle_calls_are_inert_because_the_client_owns_the_session() -> None:
    captured: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_mint_ok(captured)) as client:
        provider = _provider(client)
        acceptance = await provider.start("sess_g", "u1")
        presence = await provider.presence("sess_g", "u1")
        await provider.stop("sess_g", "u1")
        await provider.send_function_result("sess_g", "u1", "call_1", {"ok": True})
        await provider.send_progress_message("sess_g", "u1", "稍候")

    assert acceptance.provider_request_id is None
    # Runtime 看不到 Mobile 持有的 WSS，不能伪造 setupComplete。
    assert presence.bot_joined is False
    # 除了签票，生命周期调用不打任何上游请求。
    assert captured == []


# --------------------------------------------------------------------------
# 5. 注册表接入
# --------------------------------------------------------------------------


def test_registry_registers_gemini_live() -> None:
    assert "gemini_live" in default_voice_provider_registry().names()


async def test_registry_builds_gemini_live_from_settings() -> None:
    async with httpx.AsyncClient(transport=_mint_ok([])) as client:
        provider = default_voice_provider_registry().build(
            "gemini_live",
            VoiceProviderContext(
                settings=get_settings(),
                tool_registry=build_default_registry(),
                instructions="基础人设。",
                http_client=client,
            ),
        )
    assert isinstance(provider, GeminiLiveVoiceProvider)


def test_gemini_factory_requires_an_http_client() -> None:
    with pytest.raises(ValueError, match="http client"):
        default_voice_provider_registry().build(
            "gemini_live",
            VoiceProviderContext(
                settings=get_settings(), tool_registry=build_default_registry()
            ),
        )


def test_settings_expose_gemini_knobs() -> None:
    """断言声明的缺省值，不读环境 —— 否则开发者本地 .env 一改测试就红，
    而 CI 因为没有 .env 照样绿，本地与 CI 结论不一致。"""

    defaults = {name: field.default for name, field in Settings.model_fields.items()}
    assert defaults["gemini_base_url"] == "https://generativelanguage.googleapis.com"
    assert defaults["gemini_live_model"] == GEMINI_LIVE_MODEL
    assert defaults["gemini_token_ttl_seconds"] == 1800
    assert defaults["gemini_session_start_window_seconds"] == 60


# --------------------------------------------------------------------------
# 6. Mobile 契约与受控错误
# --------------------------------------------------------------------------


def _mobile_main_accepts(payload: dict, *, now: datetime = _NOW) -> bool:
    """Mirror Mobile main's fail-closed Gemini VoiceCredentials gate."""

    endpoint = str(payload.get("endpoint_url") or "")
    model = str(payload.get("model") or "")
    return bool(
        payload.get("provider") == "gemini_live"
        and model == GEMINI_LIVE_MODEL
        and endpoint == CONSTRAINED_WS_URL
        and "v1beta.GenerativeService.BidiGenerateContentConstrained" in endpoint
        and "?" not in endpoint
        and payload.get("client_secret")
        and payload.get("client_secret_expires_at")
        and payload.get("new_session_expires_at")
        and datetime.fromisoformat(payload["client_secret_expires_at"])
        > now
        and datetime.fromisoformat(payload["new_session_expires_at"])
        > now
    )


async def test_runtime_voice_schema_is_accepted_by_mobile_main_contract() -> None:
    async with httpx.AsyncClient(transport=_mint_ok([])) as client:
        credential = await _provider(client).issue_credentials(
            "sess_g",
            "u1",
            ("market_tickers", "get_market_price", "create_trade_draft"),
            "",
            _NOW,
        )
    payload = _voice_schema(credential).model_dump(mode="json")

    assert _mobile_main_accepts(payload)
    assert payload["allowed_tools"] == [
        "market_tickers",
        "get_market_price",
        "create_trade_draft",
    ]
    assert payload["client_secret_expires_at"] == "2026-08-23T12:30:00Z"
    assert payload["new_session_expires_at"] == "2026-08-23T12:01:00Z"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("endpoint_url", CONSTRAINED_WS_URL.replace("v1beta", "v1alpha")),
        ("model", "gemini-2.5-flash-native-audio-preview-12-2025"),
        ("new_session_expires_at", None),
        ("new_session_expires_at", "2026-08-23T11:59:59Z"),
    ],
)
async def test_mobile_contract_fails_closed_for_old_or_incomplete_credentials(
    field: str, value: object
) -> None:
    async with httpx.AsyncClient(transport=_mint_ok([])) as client:
        credential = await _provider(client).issue_credentials(
            "sess_g", "u1", ("market_tickers",), "", _NOW
        )
    payload = _voice_schema(credential).model_dump(mode="json")
    payload[field] = value

    assert _mobile_main_accepts(payload) is False


async def test_provider_rejects_the_legacy_model_and_nonofficial_endpoint() -> None:
    async with httpx.AsyncClient(transport=_mint_ok([])) as client:
        with pytest.raises(ValueError, match="unsupported Gemini Live model"):
            _provider(client, model="gemini-2.5-flash-native-audio-preview-12-2025")
        with pytest.raises(ValueError, match="official Google v1beta"):
            _provider(client, base_url="https://relay.example/v1alpha")


@pytest.mark.parametrize(
    ("upstream_status", "expected_status", "expected_code"),
    [
        (401, 503, "INTERNAL"),
        (409, 409, "IDEMPOTENCY_CONFLICT"),
        (429, 429, "RATE_LIMITED"),
        (503, 503, "INTERNAL"),
    ],
)
def test_gemini_control_plane_errors_are_stable_and_do_not_log_out_the_user(
    upstream_status: int, expected_status: int, expected_code: str
) -> None:
    error = GeminiLiveProviderError(
        upstream_status,
        "auth_token_rejected",
        retryable=upstream_status != 401,
    )
    response = _gemini_provider_error_response(error)
    payload = json.loads(response.body)

    assert response.status_code == expected_status
    assert payload["code"] == expected_code
    assert payload["details"]["credential_refresh_required"] is False
    assert _TOKEN not in json.dumps(payload)
