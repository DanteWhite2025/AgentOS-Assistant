from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from app.voice.callback_auth import derive_callback_signature
from app.voice.provider import VolcRtcVoiceProvider
from app.voice.volc_openapi import VolcRtcOpenAPI
from app.voice.volc_rtc_token import generate_rtc_token


def test_rtc_token_matches_official_node_demo_vector() -> None:
    token = generate_rtc_token(
        app_id="123456789012345678901234",
        app_key="test-app-key",
        room_id="room_test",
        user_id="user_test",
        issued_at=datetime.fromtimestamp(1_700_000_000, UTC),
        expires_at=datetime.fromtimestamp(1_700_000_600, UTC),
        nonce=42,
    )
    assert token == (
        "001123456789012345678901234QgAqAAAAAPFTZVjzU2UJAHJvb21fdGVzdAkAdXNlcl90"
        "ZXN0BQAAAAAAAAABAAAAAAACAAAAAAADAAAAAAAEAAAAAAAgAHsnA/mWFXjOIopPmXADhAjiMW"
        "wsh91XTQ6/93an8wtW"
    )


async def test_openapi_signature_matches_official_node_sdk_vector() -> None:
    async with httpx.AsyncClient() as client:
        api = VolcRtcOpenAPI(
            access_key_id="ak-test",
            secret_access_key="sk-test",
            client=client,
        )
        _, _, headers = api.signed_request(
            "StartVoiceChat",
            {
                "AppId": "123456789012345678901234",
                "RoomId": "room_test",
                "TaskId": "task_test",
            },
            datetime(2026, 8, 7, 1, 2, 3, tzinfo=UTC),
        )
    assert headers["X-Content-Sha256"] == (
        "cf859533e2ac70a360ab76240df9feddf9db401448638513ed0caf843c0b4c75"
    )
    assert headers["Authorization"] == (
        "HMAC-SHA256 Credential=ak-test/20260807/cn-north-1/rtc/request, "
        "SignedHeaders=host;x-content-sha256;x-date, "
        "Signature=78a52a6ddead104a8910408a89e5fb180f6072d0963dbe8506a683af093a150d"
    )


async def test_volc_provider_issues_token_and_controls_voice_task() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"Result": "ok", "ResponseMetadata": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = VolcRtcVoiceProvider(
            app_id="123456789012345678901234",
            app_key="rtc-app-key",
            api=VolcRtcOpenAPI(
                access_key_id="ak-test",
                secret_access_key="sk-test",
                client=client,
            ),
            token_ttl_seconds=600,
            voice_config={"S2SConfig": {"Provider": "volcano"}},
            welcome_message="你好",
            callback_url="https://runtime.example.com/v1/agent/volc-callback",
            callback_secret="callback-secret-never-log-or-send-to-mobile",
            function_callback_url=("https://runtime.example.com/v1/agent/volc-function-callback"),
            function_callback_secret="function-secret-never-log-or-send-to-mobile",
            ark_model="doubao-seed-1-6",
            ark_endpoint_id="ep-custom-test",
            tool_definitions=(
                {
                    "type": "function",
                    "function": {
                        "name": "get_market_price",
                        "description": "current BTC perpetual market price",
                        "parameters": {
                            "type": "object",
                            "properties": {"symbol": {"type": "string"}},
                            "required": ["symbol"],
                            "additionalProperties": False,
                        },
                    },
                },
            ),
            system_messages=("Never guess current prices.",),
        )
        now = datetime(2026, 8, 7, tzinfo=UTC)
        diagnostics = provider.safe_diagnostics("sess_1", "u1")
        credential = await provider.issue_credentials("sess_1", "u1", (), "", now)
        acceptance = await provider.start("sess_1", "u1")
        await provider.send_function_result(
            "sess_1",
            "u1",
            "call_market",
            {"symbol": "BTC", "mark_price": "67340.25"},
        )
        await provider.send_progress_message("sess_1", "u1", "正在查询，请稍候。")
        await provider.stop("sess_1", "u1")

    assert credential.provider == "volc_rtc"
    assert credential.token and credential.token.startswith("001123456789012345678901234")
    assert credential.expires_at.timestamp() == now.timestamp() + 600
    assert acceptance.provider_result == "ok"
    assert diagnostics["mobile_and_bot_differ"] is True
    assert diagnostics["s2s_configured"] is True
    assert diagnostics["s2s_asr_configured"] is True
    assert diagnostics["s2s_tts_configured"] is True
    assert diagnostics["standalone_asr_configured"] is False
    assert diagnostics["standalone_tts_configured"] is False
    assert diagnostics["llm_mode"] == "ArkV3"
    assert diagnostics["callback"] == ("https://runtime.example.com/v1/agent/volc-callback")
    assert diagnostics["function_callback"] == (
        "https://runtime.example.com/v1/agent/volc-function-callback"
    )
    assert diagnostics["provider_entitlement_verified"] is False
    diagnostic_text = json.dumps(diagnostics)
    for secret in (
        "rtc-app-key",
        "ak-test",
        "sk-test",
        "callback-secret-never-log-or-send-to-mobile",
        "function-secret-never-log-or-send-to-mobile",
        "ep-custom-test",
    ):
        assert secret not in diagnostic_text
    assert len(requests) == 4
    assert requests[0].url.params["Action"] == "StartVoiceChat"
    assert requests[1].url.params["Action"] == "UpdateVoiceChat"
    assert requests[2].url.params["Action"] == "UpdateVoiceChat"
    assert requests[3].url.params["Action"] == "StopVoiceChat"
    start_body = json.loads(requests[0].content)
    assert start_body["AgentConfig"]["TargetUserId"] == [credential.user_id]
    assert start_body["AppId"] == credential.app_id
    assert start_body["RoomId"] == credential.room_id
    assert start_body["TaskId"] == credential.task_id
    assert start_body["AgentConfig"]["UserId"] == credential.bot_user_id
    assert start_body["AgentConfig"]["EnableConversationStateCallback"] is True
    assert start_body["Config"]["S2SConfig"]["OutputMode"] == 1
    assert "ASRConfig" not in start_body["Config"]
    assert "TTSConfig" not in start_body["Config"]
    assert start_body["Config"]["S2SConfig"]["ProviderParams"]["asr"] == {
        "extra": {"end_smooth_window_ms": 800}
    }
    assert start_body["Config"]["S2SConfig"]["ProviderParams"]["tts"] == {
        "speaker": "zh_female_vv_jupiter_bigtts",
        "audio_config": {
            "channel": 1,
            "format": "pcm_s16le",
            "sample_rate": 24000,
        },
    }
    assert start_body["Config"]["LLMConfig"]["Mode"] == "ArkV3"
    assert start_body["Config"]["LLMConfig"]["ModelName"] == "doubao-seed-1-6"
    assert start_body["Config"]["LLMConfig"]["EndPointId"] == "ep-custom-test"
    assert start_body["Config"]["LLMConfig"]["Tools"][0]["function"]["name"] == "get_market_price"
    assert start_body["Config"]["FunctionCallingConfig"] == {
        "ServerMessageUrl": (
            "https://runtime.example.com/v1/agent/volc-function-callback?session_id=sess_1"
        ),
        "ServerMessageSignature": derive_callback_signature(
            "function-secret-never-log-or-send-to-mobile",
            purpose="function",
            session_id="sess_1",
            task_id=credential.task_id,
        ),
    }
    assert start_body["AgentConfig"]["ServerMessageURLForRTS"] == (
        "https://runtime.example.com/v1/agent/volc-callback?session_id=sess_1"
    )
    assert start_body["AgentConfig"]["ServerMessageSignatureForRTS"] == (
        derive_callback_signature(
            "callback-secret-never-log-or-send-to-mobile",
            purpose="state",
            session_id="sess_1",
            task_id=credential.task_id,
        )
    )
    for secret in (
        "sk-test",
        "callback-secret-never-log-or-send-to-mobile",
        "function-secret-never-log-or-send-to-mobile",
    ):
        assert secret not in requests[0].content.decode()
    assert requests[0].headers["Authorization"].startswith("HMAC-SHA256 Credential=ak-test/")
    update_body = json.loads(requests[1].content)
    assert update_body["Command"] == "function"
    assert json.loads(update_body["Message"]) == {
        "ToolCallID": "call_market",
        "Content": '{"symbol":"BTC","mark_price":"67340.25"}',
    }
    progress_body = json.loads(requests[2].content)
    assert progress_body["Command"] == "ExternalTextToSpeech"
    assert progress_body["Message"] == "正在查询，请稍候。"
    assert progress_body["InterruptMode"] == 0


async def test_room_presence_requires_exact_bot_identity_in_same_app_and_room() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "ResponseMetadata": {"RequestId": "presence-request"},
                "Result": {
                    "RoomExists": True,
                    "VisibleUserList": ["a-different-bot"],
                    "InvisibleUserList": [],
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = VolcRtcVoiceProvider(
            app_id="123456789012345678901234",
            app_key="rtc-app-key",
            api=VolcRtcOpenAPI(access_key_id="ak-test", secret_access_key="sk-test", client=client),
            token_ttl_seconds=600,
            voice_config={"S2SConfig": {"Provider": "volcano"}},
            welcome_message="你好",
            callback_url="https://runtime.example.com/v1/agent/volc-callback",
            callback_secret="callback-secret-never-log-or-send-to-mobile",
            function_callback_url=("https://runtime.example.com/v1/agent/volc-function-callback"),
            function_callback_secret="function-secret-never-log-or-send-to-mobile",
            ark_model="doubao-seed-1-6",
            ark_endpoint_id="ep-custom-test",
            tool_definitions=(),
            system_messages=(),
        )
        presence = await provider.presence("sess_1", "u1")

    assert presence.bot_joined is False
    assert presence.provider_request_id == "presence-request"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url.params["Action"] == "GetRoomOnlineUsers"
    assert requests[0].url.params["Version"] == "2023-11-01"
    credential = await provider.issue_credentials(
        "sess_1", "u1", (), "", datetime(2026, 8, 7, tzinfo=UTC)
    )
    assert requests[0].url.params["AppId"] == credential.app_id
    assert requests[0].url.params["RoomId"] == credential.room_id
    assert credential.user_id != credential.bot_user_id
