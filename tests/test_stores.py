"""Redis/PG store 实现测试（fakeredis + sqlite，无需真实环境）。

验证"配置即切换"：store 代码用假后端先跑通，真实 Redis/PG 地址就绪后行为一致。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.audit.store import AuditRecord
from app.runtime.session import Scope, Session, TextTurnResult


def _now() -> datetime:
    return datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


def _session() -> Session:
    return Session(
        session_id="sess_1",
        user_id="u1",
        modality="both",
        scope=Scope(agents=("avatar",), tools=("get_positions",)),
        state="created",
        created_at=_now(),
        last_activity_at=_now(),
        voice_error_code="S2S_NOT_ENABLED",
        voice_run_stage="failed",
        voice_trace_id="trc_1",
        voice_app_id="app_1",
        voice_room_id="room_1",
        voice_user_id="user_1",
        voice_task_id="task_1",
        voice_previous_task_ids=("task_0",),
        voice_bot_user_id="bot_1",
        voice_task_creation_result="accepted",
        voice_provider_request_id="start_request_1",
        voice_presence_request_id="presence_request_1",
        voice_callback_trace_id="callback_trace_1",
        voice_callback_auth_version="v1-derived",
        voice_original_failure_stage="preParamCheck",
        voice_provider_error_code="1000002",
        voice_provider_error_message="resource not granted",
        voice_first_failed_at=_now(),
        voice_start_requested_at=_now(),
        voice_start_accepted_at=_now(),
        voice_mobile_joined_at=_now(),
        voice_bot_joined_at=_now(),
        voice_task_started_at=_now(),
        voice_callback_received_at=_now(),
        voice_first_provider_callback_at=_now(),
        voice_callback_schema_hash="schema_hash_1",
        voice_callback_received_count=4,
        voice_callback_accepted_count=2,
        voice_callback_duplicate_count=1,
        voice_callback_stale_count=1,
        voice_user_audio_status="OBSERVED",
        voice_asr_started_at=_now(),
        voice_user_speech_started_at=_now(),
        voice_user_speech_ended_at=_now(),
        voice_llm_started_at=_now(),
        voice_llm_completed_at=_now(),
        voice_assistant_first_token_at=_now(),
        voice_function_call_started_at=_now(),
        voice_function_call_completed_at=_now(),
        voice_function_call_failed_at=_now(),
        voice_tts_started_at=_now(),
        voice_tts_first_packet_at=_now(),
        voice_stopped_at=_now(),
        voice_cleanup_status="success",
        voice_user_transcript_at=_now(),
        voice_assistant_transcript_at=_now(),
        voice_provider_audio_started_at=_now(),
        voice_interrupted_at=_now(),
        voice_callback_event_ids=("event_1",),
        voice_tool_results=(("call_1", '{"symbol":"BTC"}'),),
        voice_tool_delivered=("call_1",),
        text_turn_results=(
            TextTurnResult(
                operation_hash="b" * 64,
                request_hash="c" * 64,
                response_json='{"text":"ok","tool_calls":[],"blockers":[],"trace_id":"trc_1"}',
                created_at=_now(),
            ),
        ),
    )


async def test_redis_session_store_roundtrip() -> None:
    from fakeredis import aioredis as fake_aioredis

    from app.runtime.redis_session_store import RedisSessionStore

    store = RedisSessionStore(fake_aioredis.FakeRedis(), ttl_seconds=60)
    await store.create(_session())
    got = await store.get("sess_1")
    assert got is not None
    assert got.user_id == "u1"
    assert got.scope.tools == ("get_positions",)
    assert got.voice_error_code == "S2S_NOT_ENABLED"
    assert got.voice_task_id == "task_1"
    assert got.voice_previous_task_ids == ("task_0",)
    assert got.voice_tool_result("call_1") == '{"symbol":"BTC"}'
    assert got.voice_tool_delivered == ("call_1",)
    assert got.voice_task_creation_result == "accepted"
    assert got.voice_provider_request_id == "start_request_1"
    assert got.voice_presence_request_id == "presence_request_1"
    assert got.voice_callback_trace_id == "callback_trace_1"
    assert got.voice_callback_auth_version == "v1-derived"
    assert got.voice_original_failure_stage == "preParamCheck"
    assert got.voice_provider_error_code == "1000002"
    assert got.voice_provider_error_message == "resource not granted"
    assert got.voice_first_failed_at == _now()
    assert got.voice_start_requested_at == _now()
    assert got.voice_start_accepted_at == _now()
    assert got.voice_mobile_joined_at == _now()
    assert got.voice_bot_joined_at == _now()
    assert got.voice_task_started_at == _now()
    assert got.voice_callback_received_at == _now()
    assert got.voice_first_provider_callback_at == _now()
    assert got.voice_callback_schema_hash == "schema_hash_1"
    assert got.voice_callback_received_count == 4
    assert got.voice_callback_accepted_count == 2
    assert got.voice_callback_duplicate_count == 1
    assert got.voice_callback_stale_count == 1
    assert got.voice_user_audio_status == "OBSERVED"
    assert got.voice_asr_started_at == _now()
    assert got.voice_user_speech_started_at == _now()
    assert got.voice_user_speech_ended_at == _now()
    assert got.voice_llm_started_at == _now()
    assert got.voice_llm_completed_at == _now()
    assert got.voice_assistant_first_token_at == _now()
    assert got.voice_function_call_started_at == _now()
    assert got.voice_function_call_completed_at == _now()
    assert got.voice_function_call_failed_at == _now()
    assert got.voice_tts_started_at == _now()
    assert got.voice_tts_first_packet_at == _now()
    assert got.voice_stopped_at == _now()
    assert got.voice_cleanup_status == "success"
    assert got.voice_user_transcript_at == _now()
    assert got.voice_assistant_transcript_at == _now()
    assert got.voice_provider_audio_started_at == _now()
    assert got.voice_interrupted_at == _now()
    assert got.voice_callback_event_ids == ("event_1",)
    assert got.text_turn_result("b" * 64) is not None
    assert got.text_turn_result("b" * 64).request_hash == "c" * 64

    await store.update(got.touch(_now(), state="text_active"))
    assert (await store.get("sess_1")).state == "text_active"

    await store.delete("sess_1")
    assert await store.get("sess_1") is None


async def test_redis_session_store_missing() -> None:
    from fakeredis import aioredis as fake_aioredis

    from app.runtime.redis_session_store import RedisSessionStore

    assert await RedisSessionStore(fake_aioredis.FakeRedis()).get("nope") is None


async def test_pg_audit_store_roundtrip() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.audit.pg_store import PostgresAuditStore

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    store = PostgresAuditStore(engine)
    await store.create_schema()

    await store.record_tool_call(
        AuditRecord("t1", "sess_1", "get_positions", "a" * 64, "ref1", _now())
    )
    await store.record_tool_call(
        AuditRecord("t2", "sess_1", "create_trade_draft", "b" * 64, "td_1", _now())
    )

    records = await store.list_by_session("sess_1")
    assert len(records) == 2
    assert records[0].tool_name == "get_positions"
    assert records[0].status == "ok"  # status 列往返
    assert records[1].result_ref == "td_1"
    assert not await store.list_by_session("other")

    await engine.dispose()


async def test_postgres_strategy_store_roundtrip() -> None:
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.strategy.container import StrategyContainer, StrategyState
    from app.strategy.pg_store import PostgresStrategyStore, _to_decimal

    assert _to_decimal("2E+4") == Decimal(20000)
    assert _to_decimal(None) == Decimal(0)

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    store = PostgresStrategyStore(engine)
    await store.create_schema()

    s = StrategyContainer(
        strategy_id="s1",
        user_id="u1",
        agent_id="a1",
        name="Grid Strategy 1",
        template_id="tpl_grid",
        runtime_env="paper",
        allocation=Decimal(1000),
        capital_capacity=Decimal(5000),
        strategy_capacity=Decimal(10000),
        max_leverage=Decimal(3),
        allowed_assets=("BTC", "ETH"),
        take_profit_pct=Decimal("0.15"),
        stop_loss_pct=Decimal("0.05"),
        created_at=_now(),
        updated_at=_now(),
    )

    await store.create(s)
    got = await store.get("s1")
    assert got is not None
    assert got.name == "Grid Strategy 1"
    assert got.allocation == Decimal(1000)
    assert got.allowed_assets == ("BTC", "ETH")
    assert got.state == StrategyState.ACTIVE
    # 止盈止损往返
    assert got.take_profit_pct == Decimal("0.15")
    assert got.stop_loss_pct == Decimal("0.05")

    # Update
    paused = s.pause(_now())
    await store.update(paused)
    got_paused = await store.get("s1")
    assert got_paused is not None
    assert got_paused.state == StrategyState.PAUSED
    assert got_paused.version == 2

    # List by agent & user
    by_agent = await store.list_by_agent("u1", "a1")
    assert len(by_agent) == 1
    assert by_agent[0].strategy_id == "s1"

    by_user = await store.list_by_user("u1")
    assert len(by_user) == 1

    # 全量扫描（净值采样器遍历所有归属方时用）
    assert [s.strategy_id for s in await store.list_all()] == ["s1"]

    # Delete
    await store.delete("s1")
    assert await store.get("s1") is None

    await engine.dispose()
