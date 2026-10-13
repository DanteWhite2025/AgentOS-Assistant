"""WebRTC 实时语音网关：凭证分发、服务端风控下单、通话结账。

覆盖「数据面直连、控制面过服务端」的三条红线：
1. 凭证由服务端签发且标准化（provider / endpoint_url / 密钥 / 有效期 / 工具白名单）；
2. 直连通话里的 Tool Call 必须回到服务端过 scope 与 mandate 风控，越权一律拒绝；
3. 挂断结账幂等，且通话摘要写回同一条记忆流，供后续文字对话无缝衔接。
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from app.audit.hashing import args_hash
from app.config import get_settings
from app.container import VoiceProviderUnavailableError, build_container
from app.main import app, create_app
from app.memory.store import InMemoryMemoryStore
from app.policies.engine import PolicyEngine, PolicyViolationError
from app.policies.mandate import default_mandate
from app.runtime.context_builder import ContextBuilder
from app.tools.agentos.client import AgentOsStagingClient
from app.tools.agentos.trade_flow import TradingReadinessError
from app.tools.builtin import build_default_registry
from app.tools.executor import ToolExecutor
from app.voice.provider import (
    FakeVoiceProvider,
    OpenAIVoiceProvider,
    VolcRtcVoiceProvider,
)
from app.voice.registry import (
    UnknownVoiceProviderError,
    VoiceProviderContext,
    default_voice_provider_registry,
)
from app.voice.usage import (
    InMemoryVoiceUsageStore,
    VoiceUsageRecord,
    compute_billed_units,
)
from app.voice.volc_openapi import VolcRtcOpenAPI
from tests.core_fixtures import trading_readiness_payload

U1 = {"Authorization": "Bearer test-user:voice-u1"}
U2 = {"Authorization": "Bearer test-user:voice-u2"}

_NOW = datetime(2026, 8, 23, 9, 0, 0, tzinfo=UTC)


def _client(application=app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    )


def _fresh_app():
    """独立 app + 独立容器，避免测试之间共享 app.state.container。"""

    application = create_app()
    application.state.container = build_container(get_settings())
    return application


def _volc_provider(client: httpx.AsyncClient, **overrides) -> VolcRtcVoiceProvider:
    kwargs: dict = {
        "app_id": "123456789012345678901234",
        "app_key": "rtc-app-key",
        "api": VolcRtcOpenAPI(
            access_key_id="ak-test", secret_access_key="sk-test", client=client
        ),
        "token_ttl_seconds": 600,
        "voice_config": {"S2SConfig": {"Provider": "volcano"}},
        "welcome_message": "你好",
        "callback_url": "https://runtime.example.com/v1/agent/volc-callback",
        "callback_secret": "callback-secret-never-log-or-send-to-mobile",
        "function_callback_url": "https://runtime.example.com/v1/agent/volc-function-callback",
        "function_callback_secret": "function-secret-never-log-or-send-to-mobile",
        "ark_model": "doubao-seed-1-6",
        "ark_endpoint_id": "ep-custom-test",
        "tool_definitions": (),
        "system_messages": ("基础人设。",),
    }
    kwargs.update(overrides)
    return VolcRtcVoiceProvider(**kwargs)


# --------------------------------------------------------------------------
# 1. 凭证签发标准化
# --------------------------------------------------------------------------


async def test_openai_provider_issues_standardized_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"value": "ek_openai_secret", "expires_at": int(_NOW.timestamp()) + 60},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIVoiceProvider("sk-test", client, instructions="基础人设。")
        cred = await provider.issue_credentials(
            "sess_openai", "u1", ("get_positions", "market_tickers"), "gpt-realtime", _NOW
        )

    assert cred.provider == "openai"
    assert cred.endpoint_url == "https://api.openai.com/v1/realtime/calls"
    assert cred.client_secret == "ek_openai_secret"
    assert cred.auth_token == "ek_openai_secret"
    assert cred.expires_at == datetime.fromtimestamp(int(_NOW.timestamp()) + 60, tz=UTC)
    assert cred.allowed_tools == ("get_positions", "market_tickers")


async def test_openai_provider_injects_recent_context_into_realtime_instructions() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"value": "ek_openai_secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIVoiceProvider("sk-test", client, instructions="基础人设。")
        await provider.issue_credentials(
            "sess_openai",
            "u1",
            (),
            "gpt-realtime",
            _NOW,
            context_summary="<recent_context>\n- 用户刚用文字问过 BTC 仓位\n</recent_context>",
        )

    instructions = payloads[0]["session"]["instructions"]
    assert instructions.startswith("基础人设。")
    assert "用户刚用文字问过 BTC 仓位" in instructions


async def test_volc_provider_issues_standardized_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Result": "ok", "ResponseMetadata": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _volc_provider(client)
        cred = await provider.issue_credentials(
            "sess_volc", "u1", ("market_tickers",), "", _NOW
        )

    assert cred.provider == "volc_rtc"
    assert cred.auth_token == cred.token
    assert cred.auth_token and cred.auth_token.startswith("001123456789012345678901234")
    assert cred.allowed_tools == ("market_tickers",)
    # 火山 RTC 由 SDK 用 app_id/room_id/token 直接进房，没有客户端可见的 HTTP 入口。
    assert cred.endpoint_url is None
    assert cred.expires_at.timestamp() == _NOW.timestamp() + 600


async def test_volc_provider_appends_context_summary_to_system_messages() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"Result": "ok", "ResponseMetadata": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = _volc_provider(client)
        await provider.start(
            "sess_volc",
            "u1",
            context_summary="<recent_context>\n- 用户刚用文字问过 ETH 仓位\n</recent_context>",
        )

    system_messages = json.loads(requests[0].content)["Config"]["LLMConfig"]["SystemMessages"]
    assert system_messages[0] == "基础人设。"
    assert "用户刚用文字问过 ETH 仓位" in system_messages[-1]


async def test_fake_provider_exposes_the_same_standardized_fields() -> None:
    cred = await FakeVoiceProvider().issue_credentials(
        "sess_fake", "u1", ("get_positions",), "gpt-realtime", _NOW
    )
    assert cred.provider == "fake"
    assert cred.endpoint_url == "https://api.openai.com/v1/realtime/calls"
    assert cred.auth_token == cred.client_secret
    assert cred.allowed_tools == ("get_positions",)


# --------------------------------------------------------------------------
# 2. Provider 注册表：按配置分发 + 插件化扩展
# --------------------------------------------------------------------------


def _context() -> VoiceProviderContext:
    return VoiceProviderContext(
        settings=get_settings(),
        tool_registry=build_default_registry(),
        instructions="基础人设。",
    )


def test_voice_provider_registry_dispatches_configured_provider() -> None:
    registry = default_voice_provider_registry()
    assert set(registry.names()) >= {"fake", "openai", "volc_rtc", "gemini_live"}
    assert isinstance(registry.build("fake", _context()), FakeVoiceProvider)


def test_voice_provider_registry_rejects_unknown_provider() -> None:
    with pytest.raises(UnknownVoiceProviderError):
        default_voice_provider_registry().build("grok_voice", _context())


def test_voice_provider_registry_plugin_registration_is_immutable() -> None:
    base = default_voice_provider_registry()
    extended = base.with_provider("grok_voice", lambda _ctx: FakeVoiceProvider())

    assert "grok_voice" in extended.names()
    assert "grok_voice" not in base.names()
    assert isinstance(extended.build("grok_voice", _context()), FakeVoiceProvider)


# --------------------------------------------------------------------------
# 3. POST /api/voice/tools/execute —— 服务端风控执行
# --------------------------------------------------------------------------


async def _create_voice_session(client: httpx.AsyncClient, headers: dict) -> dict:
    response = await client.post("/v1/agent/sessions", json={"modality": "both"}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def test_execute_tool_returns_standard_tool_result() -> None:
    async with _client() as client:
        session = await _create_voice_session(client, U1)
        response = await client.post(
            "/api/voice/tools/execute",
            json={
                "session_id": session["session_id"],
                "tool_call_id": "call_1",
                "name": "get_positions",
                "arguments": {},
            },
            headers=U1,
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tool_call_id"] == "call_1"
    assert body["name"] == "get_positions"
    assert body["status"] == "ok"
    assert body["output"] is not None
    assert body["trace_id"]


async def test_execute_tool_rejects_tool_outside_session_scope() -> None:
    async with _client() as client:
        session = await _create_voice_session(client, U1)
        response = await client.post(
            "/api/voice/tools/execute",
            json={
                "session_id": session["session_id"],
                "tool_call_id": "call_2",
                "name": "withdraw_all_funds",
                "arguments": {"to": "0xattacker"},
            },
            headers=U1,
        )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "TOOL_NOT_IN_SCOPE"


async def test_execute_tool_rejects_another_users_session() -> None:
    async with _client() as client:
        session = await _create_voice_session(client, U1)
        response = await client.post(
            "/api/voice/tools/execute",
            json={
                "session_id": session["session_id"],
                "tool_call_id": "call_3",
                "name": "get_positions",
                "arguments": {},
            },
            headers=U2,
        )

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "SESSION_NOT_FOUND"


async def test_execute_tool_requires_authentication() -> None:
    async with _client() as client:
        session = await _create_voice_session(client, U1)
        response = await client.post(
            "/api/voice/tools/execute",
            json={
                "session_id": session["session_id"],
                "tool_call_id": "call_4",
                "name": "get_positions",
                "arguments": {},
            },
        )

    assert response.status_code == 401, response.text


async def test_execute_tool_writes_an_audit_record_even_when_rejected() -> None:
    application = _fresh_app()
    container = application.state.container
    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        session_id = session["session_id"]
        await client.post(
            "/api/voice/tools/execute",
            json={
                "session_id": session_id,
                "tool_call_id": "call_5",
                "name": "withdraw_all_funds",
                "arguments": {},
            },
            headers=U1,
        )

    records = await container.audit_store.list_by_session(session_id)
    assert [r.status for r in records] == ["rejected"]
    assert records[0].tool_name == "withdraw_all_funds"


# --------------------------------------------------------------------------
# 4. POST /api/voice/session/end —— 结账 + 摘要写回
# --------------------------------------------------------------------------


def test_billed_units_charge_per_minute_and_per_token_block() -> None:
    # 61 秒 → 2 个计时单元；1200 tokens → 2 个 token 单元。
    assert compute_billed_units(61.0, audio_tokens=900, text_tokens=300) == 4
    assert compute_billed_units(0.0, audio_tokens=0, text_tokens=0) == 0


async def test_in_memory_usage_store_is_idempotent_per_session() -> None:
    store = InMemoryVoiceUsageStore()
    record = VoiceUsageRecord(
        session_id="sess_x",
        user_id="u1",
        provider="openai",
        duration_seconds=120.0,
        audio_tokens=1000,
        text_tokens=0,
        billed_units=compute_billed_units(120.0, audio_tokens=1000, text_tokens=0),
        recorded_at=_NOW,
    )
    first = await store.settle(record)
    second = await store.settle(record)

    assert first.duplicate is False
    assert second.duplicate is True
    assert first.total_billed_units == second.total_billed_units
    assert len(await store.list_by_user("u1")) == 1


async def test_session_end_settles_usage_and_writes_summary_to_memory() -> None:
    application = _fresh_app()
    container = application.state.container
    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        session_id = session["session_id"]
        response = await client.post(
            "/api/voice/session/end",
            json={
                "session_id": session_id,
                "provider": "openai",
                "duration_seconds": 90,
                "usage_tokens": {"audio_tokens": 1500, "text_tokens": 500},
                "transcript_summary": "用户询问 BTC 走势，确认了一笔多单草案。",
            },
            headers=U1,
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["session_id"] == session_id
    assert body["provider"] == "openai"
    assert body["duplicate"] is False
    # 90s → 2 个计时单元；2000 tokens → 2 个 token 单元。
    assert body["billed_units"] == "4"
    assert body["total_billed_units"] == "4"
    assert body["memory_episode_id"]

    usage = await container.voice_usage_store.list_by_user("voice-u1")
    assert [r.session_id for r in usage] == [session_id]

    episodes = await container.memory_store.recall_episodes("voice-u1")
    assert episodes[0].summary == "用户询问 BTC 走势，确认了一笔多单草案。"
    assert episodes[0].metadata["source"] == "voice"
    assert episodes[0].metadata["session_id"] == session_id

    audit = await container.audit_store.list_by_session(session_id)
    assert [r.tool_name for r in audit] == ["voice_session_end"]
    # 审计的 args_hash 必须是冻结规范算出来的 SHA-256，不能是自拼的可读串。
    assert audit[0].args_hash == args_hash(
        {
            "provider": "openai",
            "duration_seconds": "90.0",
            "audio_tokens": 1500,
            "text_tokens": 500,
        }
    )


async def test_session_end_is_idempotent_and_never_double_charges() -> None:
    application = _fresh_app()
    container = application.state.container
    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        payload = {
            "session_id": session["session_id"],
            "provider": "openai",
            "duration_seconds": 90,
            "usage_tokens": {"audio_tokens": 1500, "text_tokens": 500},
            "transcript_summary": "用户询问 BTC 走势。",
        }
        first = await client.post("/api/voice/session/end", json=payload, headers=U1)
        second = await client.post("/api/voice/session/end", json=payload, headers=U1)

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert second.json()["total_billed_units"] == "4"

    # 摘要也只写一次，避免记忆流被重复挂断请求灌满。
    episodes = await container.memory_store.recall_episodes("voice-u1")
    assert len(episodes) == 1


async def test_session_end_rejects_another_users_session() -> None:
    async with _client() as client:
        session = await _create_voice_session(client, U1)
        response = await client.post(
            "/api/voice/session/end",
            json={
                "session_id": session["session_id"],
                "provider": "openai",
                "duration_seconds": 10,
                "usage_tokens": {"audio_tokens": 0, "text_tokens": 0},
            },
            headers=U2,
        )

    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"


async def test_session_end_does_not_short_circuit_the_volc_fallback_teardown() -> None:
    """结账只做记账，不得把会话标成已清理 —— 否则 DELETE 会跳过 StopVoiceChat。"""

    stopped: list[str] = []

    class SpyProvider(FakeVoiceProvider):
        """带 task_id 的 Fake —— 只有存在 provider 任务，DELETE 才会走 Stop 分支。"""

        async def issue_credentials(self, *args, **kwargs):
            cred = await super().issue_credentials(*args, **kwargs)
            return replace(cred, task_id="task_fallback", room_id="room_fallback")

        async def stop(self, session_id: str, user_id: str, attempt: int = 0) -> None:
            stopped.append(session_id)

    application = _fresh_app()
    application.state.container.voice_provider = SpyProvider()
    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        session_id = session["session_id"]
        await client.post(f"/v1/agent/sessions/{session_id}/voice/start", headers=U1)
        await client.post(
            "/api/voice/session/end",
            json={
                "session_id": session_id,
                "provider": "volc_rtc",
                "duration_seconds": 30,
                "usage_tokens": {"audio_tokens": 100, "text_tokens": 100},
            },
            headers=U1,
        )
        deleted = await client.delete(f"/v1/agent/sessions/{session_id}", headers=U1)

    assert deleted.status_code == 204
    assert stopped == [session_id]


# --------------------------------------------------------------------------
# 5. 文字 ↔ 语音连贯：新会话凭证带上工具白名单与入口
# --------------------------------------------------------------------------


async def test_session_credentials_expose_allowed_tools_and_endpoint() -> None:
    async with _client() as client:
        session = await _create_voice_session(client, U1)

    voice = session["voice"]
    assert voice["endpoint_url"] == "https://api.openai.com/v1/realtime/calls"
    assert "get_positions" in voice["allowed_tools"]


# --------------------------------------------------------------------------
# 6. 文字 → 语音：接通前把最近上下文注入 Realtime Session
# --------------------------------------------------------------------------


async def test_context_builder_renders_recent_voice_and_text_context() -> None:
    store = InMemoryMemoryStore()
    await store.remember_profile("u1", "preference", "risk", {"level": "conservative"})
    await store.remember_episode("u1", "用户刚用文字问过 BTC 仓位", metadata={"source": "text"})
    rendered = await ContextBuilder(store).build_voice_context("u1")

    assert rendered.startswith("<recent_context>")
    assert rendered.endswith("</recent_context>")
    assert "用户刚用文字问过 BTC 仓位" in rendered
    assert "conservative" in rendered


async def test_context_builder_returns_empty_when_nothing_to_carry_over() -> None:
    assert await ContextBuilder(InMemoryMemoryStore()).build_voice_context("u1") == ""
    assert await ContextBuilder(None).build_voice_context("u1") == ""


async def test_creating_a_voice_session_injects_recent_context_into_credentials() -> None:
    seen: list[str] = []

    class RecordingProvider(FakeVoiceProvider):
        async def issue_credentials(self, *args, **kwargs):
            seen.append(kwargs.get("context_summary", ""))
            return await super().issue_credentials(*args, **kwargs)

    application = _fresh_app()
    container = application.state.container
    container.voice_provider = RecordingProvider()
    await container.memory_store.remember_episode(
        "voice-u1", "上一通电话确认了 ETH 多单草案", metadata={"source": "voice"}
    )

    async with _client(application) as client:
        await _create_voice_session(client, U1)

    assert "上一通电话确认了 ETH 多单草案" in seen[0]


async def test_starting_voice_chat_injects_recent_context_into_the_provider_task() -> None:
    seen: list[str] = []

    class RecordingProvider(FakeVoiceProvider):
        async def start(self, session_id, user_id, attempt=0, context_summary=""):
            seen.append(context_summary)
            return await super().start(session_id, user_id, attempt)

    application = _fresh_app()
    container = application.state.container
    container.voice_provider = RecordingProvider()
    await container.memory_store.remember_episode(
        "voice-u1", "用户偏好只做 BTC 永续", metadata={"source": "text"}
    )

    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        response = await client.post(
            f"/v1/agent/sessions/{session['session_id']}/voice/start", headers=U1
        )

    assert response.status_code == 200, response.text
    assert "用户偏好只做 BTC 永续" in seen[0]


# --------------------------------------------------------------------------
# 7. 配置装配：Provider 入口与计费口径可配
# --------------------------------------------------------------------------


async def test_openai_provider_honours_a_custom_base_url() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, json={"value": "ek_openai_secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIVoiceProvider("sk-test", client, base_url="https://relay.example.com/")
        cred = await provider.issue_credentials("s", "u1", (), "gpt-realtime", _NOW)

    assert urls == ["https://relay.example.com/v1/realtime/client_secrets"]
    assert cred.endpoint_url == "https://relay.example.com/v1/realtime/calls"


def test_settings_expose_voice_gateway_knobs() -> None:
    settings = get_settings()
    assert settings.openai_base_url == "https://api.openai.com"
    assert settings.voice_billing_seconds_per_unit == 60
    assert settings.voice_billing_tokens_per_unit == 1000
    assert settings.voice_quota_units_per_user == 0


async def test_session_end_reports_remaining_quota_when_configured() -> None:
    store = InMemoryVoiceUsageStore(quota_units_per_user=10)
    settlement = await store.settle(
        VoiceUsageRecord(
            session_id="sess_q",
            user_id="u1",
            provider="openai",
            duration_seconds=180.0,
            audio_tokens=0,
            text_tokens=0,
            billed_units=compute_billed_units(180.0),
            recorded_at=_NOW,
        )
    )
    assert settlement.total_billed_units == 3
    assert settlement.remaining_units == 7


# --------------------------------------------------------------------------
# 8. 错误分支：上游失败必须映射成受控错误，绝不把原始 body 抛给客户端
# --------------------------------------------------------------------------


class _RaisingExecutor:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def execute(self, *args, **kwargs):
        raise self._exc


async def _execute_with_failing_executor(exc: Exception) -> httpx.Response:
    application = _fresh_app()
    application.state.container.executor = _RaisingExecutor(exc)
    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        return await client.post(
            "/api/voice/tools/execute",
            json={
                "session_id": session["session_id"],
                "tool_call_id": "call_err",
                "name": "get_positions",
                "arguments": {},
            },
            headers=U1,
        )


async def test_execute_tool_maps_invalid_arguments_to_400() -> None:
    response = await _execute_with_failing_executor(ValueError("symbol 不能为空"))
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_REQUEST"


async def test_execute_tool_maps_core_timeout_to_upstream_timeout() -> None:
    response = await _execute_with_failing_executor(httpx.ReadTimeout("core timeout"))
    assert response.status_code == 504
    assert response.json()["code"] == "TOOL_UPSTREAM_TIMEOUT"


async def test_execute_tool_maps_trading_readiness_conflict_to_409() -> None:
    response = await _execute_with_failing_executor(TradingReadinessError("账户尚未就绪"))
    assert response.status_code == 409
    assert response.json()["code"] == "POLICY_VIOLATION"


async def test_execute_tool_maps_policy_violation_to_403() -> None:
    response = await _execute_with_failing_executor(PolicyViolationError("超出单笔限额"))
    assert response.status_code == 403
    assert response.json()["code"] == "POLICY_VIOLATION"


def test_openai_factory_requires_an_http_client() -> None:
    with pytest.raises(ValueError, match="http client"):
        default_voice_provider_registry().build("openai", _context())


def test_billing_rejects_non_positive_unit_sizes() -> None:
    with pytest.raises(ValueError, match="positive"):
        compute_billed_units(60.0, seconds_per_unit=0)


# --------------------------------------------------------------------------
# 9. 主备容灾：按需向 /token 领备用 provider 的票
# --------------------------------------------------------------------------


class _AltProvider(FakeVoiceProvider):
    """可观测的备用 provider；带 task_id 才会真的走 Stop 分支。"""

    def __init__(self) -> None:
        self.issued: list[str] = []
        self.started: list[str] = []
        self.stopped: list[str] = []

    async def issue_credentials(self, *args, **kwargs):
        self.issued.append(args[0])
        cred = await super().issue_credentials(*args, **kwargs)
        return replace(cred, provider="volc_rtc", task_id="task_alt", room_id="room_alt")

    async def start(self, session_id, user_id, attempt=0, context_summary=""):
        self.started.append(session_id)
        return await super().start(session_id, user_id, attempt)

    async def stop(self, session_id: str, user_id: str, attempt: int = 0) -> None:
        self.stopped.append(session_id)


def _app_with_alternate(name: str = "openai") -> tuple[object, _AltProvider]:
    application = _fresh_app()
    alternate = _AltProvider()
    enabled = application.state.container.settings.enabled_voice_provider_names
    application.state.container.settings.enabled_voice_providers = ",".join(
        dict.fromkeys((*enabled, name))
    )
    application.state.container.voice_providers[name] = alternate
    return application, alternate


async def test_container_returns_the_primary_provider_by_default() -> None:
    container = build_container(get_settings())
    assert container.voice_provider_for(None) is container.voice_provider
    assert (
        container.voice_provider_for(container.settings.realtime_voice_provider)
        is container.voice_provider
    )


async def test_container_reports_an_unbuildable_fallback_provider() -> None:
    container = build_container(get_settings())
    with pytest.raises(VoiceProviderUnavailableError):
        # 未启用的 provider 必须在触发任何上游请求前被拒绝。
        container.voice_provider_for("openai")


async def test_container_rejects_a_cached_but_disabled_provider() -> None:
    container = build_container(get_settings())
    container.voice_providers["openai"] = _AltProvider()

    with pytest.raises(VoiceProviderUnavailableError):
        container.voice_provider_for("openai")


async def test_token_endpoint_issues_a_fallback_ticket_from_the_requested_provider() -> None:
    application, alternate = _app_with_alternate("openai")
    container = application.state.container
    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        session_id = session["session_id"]
        response = await client.post(
            f"/v1/agent/sessions/{session_id}/token",
            json={"want": ["voice"], "provider": "openai"},
            headers=U1,
        )

    assert response.status_code == 200, response.text
    assert response.json()["voice"]["provider"] == "volc_rtc"
    assert alternate.issued == [session_id]
    stored = await container.session_store.get(session_id)
    assert stored.voice_provider_name == "openai"


async def test_token_endpoint_defaults_to_the_configured_provider() -> None:
    application, alternate = _app_with_alternate("openai")
    container = application.state.container
    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        response = await client.post(
            f"/v1/agent/sessions/{session['session_id']}/token",
            json={"want": ["voice"]},
            headers=U1,
        )

    assert response.status_code == 200, response.text
    assert alternate.issued == []
    stored = await container.session_store.get(session["session_id"])
    assert stored.voice_provider_name == container.settings.realtime_voice_provider


async def test_token_endpoint_keeps_the_session_provider_when_refresh_omits_it() -> None:
    application, alternate = _app_with_alternate("openai")
    container = application.state.container
    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        session_id = session["session_id"]
        selected = await client.post(
            f"/v1/agent/sessions/{session_id}/token",
            json={"want": ["voice"], "provider": "openai"},
            headers=U1,
        )
        refreshed = await client.post(
            f"/v1/agent/sessions/{session_id}/token",
            json={"want": ["voice"]},
            headers=U1,
        )

    assert selected.status_code == 200, selected.text
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["voice"]["provider"] == "volc_rtc"
    assert alternate.issued == [session_id, session_id]
    stored = await container.session_store.get(session_id)
    assert stored.voice_provider_name == "openai"


async def test_voice_start_and_stop_follow_the_session_provider() -> None:
    application, alternate = _app_with_alternate("openai")
    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        session_id = session["session_id"]
        await client.post(
            f"/v1/agent/sessions/{session_id}/token",
            json={"want": ["voice"], "provider": "openai"},
            headers=U1,
        )
        started = await client.post(f"/v1/agent/sessions/{session_id}/voice/start", headers=U1)
        deleted = await client.delete(f"/v1/agent/sessions/{session_id}", headers=U1)

    assert started.status_code == 200, started.text
    assert deleted.status_code == 204
    assert alternate.started == [session_id]
    assert alternate.stopped == [session_id]


async def test_token_endpoint_refuses_to_switch_while_a_provider_task_is_live() -> None:
    application, _ = _app_with_alternate("openai")
    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        session_id = session["session_id"]
        await client.post(f"/v1/agent/sessions/{session_id}/voice/start", headers=U1)
        response = await client.post(
            f"/v1/agent/sessions/{session_id}/token",
            json={"want": ["voice"], "provider": "openai"},
            headers=U1,
        )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "POLICY_VIOLATION"


async def test_token_endpoint_reports_an_unavailable_fallback_provider() -> None:
    application = _fresh_app()
    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        response = await client.post(
            f"/v1/agent/sessions/{session['session_id']}/token",
            json={"want": ["voice"], "provider": "openai"},
            headers=U1,
        )

    assert response.status_code == 503, response.text
    assert response.json()["code"] == "INTERNAL"


# --------------------------------------------------------------------------
# 10. 私有账户工具：USER Bearer Token 必须透传到下游
# --------------------------------------------------------------------------

_CORE_USER_TOKEN = "test-user:voice-u1"
_CORE_AGENT_TOKEN = "agent-token-never-return"


def _app_with_real_executor() -> tuple[object, list[httpx.Request], httpx.AsyncClient]:
    """装配真 ToolExecutor + AgentOS 客户端，让 USER 凭据门禁真的生效。"""

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/me/trading-readiness":
            # 红线：私有账户查询必须带调用者本人的 USER Token，不能用 AGENT Token 冒名。
            assert request.headers["Authorization"] == f"Bearer {_CORE_USER_TOKEN}"
            return httpx.Response(200, json=trading_readiness_payload())
        if request.url.path == "/internal/v1/markets":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "instrument_id": "perp_hyperliquid_testnet_btc",
                            "market_type": "PERP",
                            "venue": "HYPERLIQUID",
                            "environment": "TESTNET",
                            "display_symbol": "BTC-PERP",
                            "venue_symbol": "BTC",
                            "base_asset": "BTC",
                            "quote_asset": "USDC",
                            "trade_supported": True,
                            "supported_order_types": ["LIMIT"],
                            "price_decimals": 2,
                            "size_decimals": 5,
                            "minimum_quantity": "0.00001",
                            "minimum_notional": "1",
                            "maximum_leverage": "2",
                            "status": "ACTIVE",
                            "stale": False,
                            "as_of": "2026-08-16T00:00:00Z",
                        }
                    ],
                    "meta": {"stale": False, "next_cursor": None},
                    "request_id": "req_catalog",
                },
            )
        if request.url.path == "/api/v1/me/agent-trade-intents":
            assert request.headers["Authorization"] == f"Bearer {_CORE_USER_TOKEN}"
            body = json.loads(request.content)
            assert body["agent_id"] == "agent-test-001"
            assert body["source"] == "VOICE"
            assert "strategy_id" not in body
            assert "mandate_id" not in body
            return httpx.Response(
                201,
                json={
                    "data": {"id": "aint_1", "status": "PENDING_USER"},
                    "request_id": "req_draft",
                },
            )
        raise AssertionError(f"unexpected Core route: {request.url.path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    application = _fresh_app()
    container = application.state.container
    container.executor = ToolExecutor(
        container.registry,
        container.core_client,
        PolicyEngine(default_mandate()),
        agentos_client=AgentOsStagingClient("https://core.example", _CORE_AGENT_TOKEN, client),
        expected_agent_id="agent-test-001",
    )
    return application, requests, client


async def test_voice_tool_execute_forwards_the_user_bearer_token_downstream() -> None:
    application, requests, http = _app_with_real_executor()
    try:
        async with _client(application) as client:
            session = await _create_voice_session(client, U1)
            response = await client.post(
                "/api/voice/tools/execute",
                json={
                    "session_id": session["session_id"],
                    "tool_call_id": "call_readiness",
                    "name": "get_trading_readiness",
                    "arguments": {},
                },
                headers=U1,
            )
    finally:
        await http.aclose()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["output"]["daily_notional_remaining_minor"] == "49500000"
    assert [r.url.path for r in requests] == ["/api/v1/me/trading-readiness"]


async def test_voice_tool_execute_sends_source_voice_in_intent_body() -> None:
    application, requests, http = _app_with_real_executor()
    try:
        async with _client(application) as client:
            session = await _create_voice_session(client, U1)
            response = await client.post(
                "/api/voice/tools/execute",
                json={
                    "session_id": session["session_id"],
                    "tool_call_id": "call_draft",
                    "name": "create_trade_draft",
                    "arguments": {
                        "symbol": "BTC",
                        "side": "buy",
                        "order_type": "limit",
                        "notional_usdc": "10",
                        "price": "100000",
                        "leverage": 1,
                    },
                },
                headers={**U1, "Idempotency-Key": "idem_123"},
            )
    finally:
        await http.aclose()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["output"]["trade_intent_id"] == "aint_1"
    assert _CORE_AGENT_TOKEN not in response.text
    assert _CORE_USER_TOKEN not in response.text

    writes = [request for request in requests if request.method == "POST"]
    assert [request.url.path for request in writes] == ["/api/v1/me/agent-trade-intents"]
    assert all(request.url.path != "/api/v1/me/trading-readiness" for request in requests)


async def test_voice_tool_execute_never_leaks_credentials_in_the_response() -> None:
    application, _requests, http = _app_with_real_executor()
    try:
        async with _client(application) as client:
            session = await _create_voice_session(client, U1)
            response = await client.post(
                "/api/voice/tools/execute",
                json={
                    "session_id": session["session_id"],
                    "tool_call_id": "call_readiness",
                    "name": "get_trading_readiness",
                    "arguments": {},
                },
                headers=U1,
            )
    finally:
        await http.aclose()

    assert _CORE_AGENT_TOKEN not in response.text
    assert _CORE_USER_TOKEN not in response.text


# --------------------------------------------------------------------------
# 11. Azure OpenAI Realtime 凭证签发
# --------------------------------------------------------------------------


async def _issue_with_base_url(base_url: str) -> httpx.Request:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"value": "ek_secret"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIVoiceProvider("secret-key", client, base_url=base_url)
        await provider.issue_credentials("s", "u1", (), "gpt-realtime-2.1", _NOW)
    return captured[0]


async def test_azure_endpoint_authenticates_with_the_api_key_header() -> None:
    request = await _issue_with_base_url("https://my-res.openai.azure.com/openai")

    assert request.headers.get("api-key") == "secret-key"
    # Azure 不认 Bearer 形式的 API Key，混发只会让请求更难排查。
    assert "authorization" not in {k.lower() for k in request.headers}
    assert str(request.url) == (
        "https://my-res.openai.azure.com/openai/v1/realtime/client_secrets"
    )
    assert json.loads(request.content)["session"]["model"] == "gpt-realtime-2.1"


async def test_public_openai_endpoint_still_authenticates_with_bearer() -> None:
    request = await _issue_with_base_url("https://api.openai.com")

    assert request.headers["Authorization"] == "Bearer secret-key"
    assert "api-key" not in {k.lower() for k in request.headers}


# --------------------------------------------------------------------------
# 12. 可观测性：失败路径必须留下能定位的日志（但不落参数原文）
# --------------------------------------------------------------------------


async def test_tool_failure_logs_enough_to_diagnose(caplog) -> None:
    """线上 503 只有 uvicorn 访问行，错误码/工具名/trace_id 全都查不到。"""

    caplog.set_level(logging.WARNING, logger="app.api.tool_execution")
    response = await _execute_with_failing_executor(ValueError("symbol 不能为空"))

    assert response.status_code == 400
    records = [r for r in caplog.records if r.name == "app.api.tool_execution"]
    assert records, "失败路径必须留日志"
    message = records[0].getMessage()
    assert "tool_execution_failed" in message
    assert "get_positions" in message           # tool_name
    assert "INVALID_REQUEST" in message         # error_code
    assert response.json()["trace_id"] in message
    assert "args_hash=" in message


async def test_tool_failure_log_never_contains_argument_values(caplog) -> None:
    """红线：只落 args_hash 与键名，绝不落参数原文（金额/账户标识都在里面）。"""

    caplog.set_level(logging.WARNING, logger="app.api.tool_execution")
    application = _fresh_app()
    application.state.container.executor = _RaisingExecutor(ValueError("boom"))
    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        await client.post(
            "/api/voice/tools/execute",
            json={
                "session_id": session["session_id"],
                "tool_call_id": "call_secret",
                "name": "create_trade_draft",
                "arguments": {"symbol": "BTC", "notional_usdc": "123456.78"},
            },
            headers=U1,
        )

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "123456.78" not in logged
    assert "args_hash=" in logged
    # 键名不敏感，且足以判断是哪种调用形态。
    assert "arg_keys=notional_usdc,symbol" in logged
