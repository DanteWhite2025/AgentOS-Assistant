"""语音结算接入用量记账的接线回归。

关键点：记账不得改变 /session/end 既有的响应体与幂等语义。
app / client / 会话创建一律复用 test_voice_webrtc_gateway 的既有搭建方式。
"""

from datetime import UTC, datetime
from decimal import Decimal

from app.usage.fx import FxTable
from app.usage.models import UsageRecord
from app.usage.pricing import PriceBook, PriceEntry
from app.usage.recorder import UsageRecorder
from app.usage.store import InMemoryUsageStore
from tests.test_voice_webrtc_gateway import U1, _client, _create_voice_session, _fresh_app

FROM = datetime(2026, 1, 1, tzinfo=UTC)
WINDOW = (datetime(2020, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC))

_PAYLOAD = {
    "provider": "openai",
    "duration_seconds": 90,
    "usage_tokens": {"audio_tokens": 1500, "text_tokens": 500},
}


def _voice_book() -> PriceBook:
    return PriceBook(
        [
            PriceEntry("openai", "voice", "duration_minute", Decimal("0.06"), "USD", FROM, "seed"),
            PriceEntry("openai", "voice", "input_tokens", Decimal(1), "USD", FROM, "seed"),
            PriceEntry("openai", "voice", "output_tokens", Decimal(2), "USD", FROM, "seed"),
        ]
    )


def _attach_recorder(application) -> InMemoryUsageStore:
    store = InMemoryUsageStore()
    application.state.container.usage_recorder = UsageRecorder(store, _voice_book(), FxTable([]))
    return store


async def test_session_end_records_voice_usage_once() -> None:
    """同一 session 重复结算只产生一条用量流水，金额按原始量纲算。"""
    application = _fresh_app()
    store = _attach_recorder(application)
    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        payload = {"session_id": session["session_id"], **_PAYLOAD}
        first = await client.post("/api/voice/session/end", json=payload, headers=U1)
        second = await client.post("/api/voice/session/end", json=payload, headers=U1)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True

    rows = await store.list_for_user("voice-u1", *WINDOW)
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "voice"
    assert row.scene == "voice"
    assert row.provider == "openai"
    assert row.session_id == session["session_id"]
    # 存原始量纲，不存 billed_units（spec D10）
    assert row.quantity.duration_seconds == Decimal(90)
    assert row.quantity.input_tokens == 1500
    assert row.quantity.output_tokens == 500
    # 1.5min*0.06 + 1500/1e6*1 + 500/1e6*2 = 0.09 + 0.0015 + 0.001
    assert row.amount_usd == Decimal("0.09250000")
    assert row.status == "ok"


async def test_billed_units_response_still_uses_quota_semantics() -> None:
    """成本记账不得改动既有配额口径：billed_units 仍是 4（2 时长单元 + 2 token 单元）。"""
    application = _fresh_app()
    _attach_recorder(application)
    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        response = await client.post(
            "/api/voice/session/end",
            json={"session_id": session["session_id"], **_PAYLOAD},
            headers=U1,
        )

    assert response.status_code == 200, response.text
    assert response.json()["billed_units"] == "4"


async def test_session_end_unchanged_when_recorder_absent() -> None:
    """recorder 关掉时 /session/end 行为与接入前完全一致。

    容器现在默认装了 recorder（Task 12 接线），所以这里显式置 None 来验证
    降级路径——真实的降级场景是 fake 模式或将来配置关闭记账。
    """
    application = _fresh_app()
    application.state.container.usage_recorder = None
    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        response = await client.post(
            "/api/voice/session/end",
            json={"session_id": session["session_id"], **_PAYLOAD},
            headers=U1,
        )

    assert response.status_code == 200, response.text
    assert response.json()["billed_units"] == "4"


async def test_session_end_succeeds_even_if_recording_fails() -> None:
    """记账挂了不能让用户挂断失败（spec §6.3）。"""

    class BoomStore:
        async def append(self, record: UsageRecord) -> bool:
            raise RuntimeError("db down")

    application = _fresh_app()
    recorder = UsageRecorder(BoomStore(), _voice_book(), FxTable([]))  # type: ignore[arg-type]
    application.state.container.usage_recorder = recorder
    async with _client(application) as client:
        session = await _create_voice_session(client, U1)
        response = await client.post(
            "/api/voice/session/end",
            json={"session_id": session["session_id"], **_PAYLOAD},
            headers=U1,
        )

    assert response.status_code == 200, response.text
    assert recorder.failure_count == 1
