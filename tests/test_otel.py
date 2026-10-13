"""OTel 埋点测试：验证请求真的产生 span（InMemorySpanExporter，无需 collector）。"""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("opentelemetry")


async def test_fastapi_instrumentation_emits_span() -> None:
    from fastapi import FastAPI
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    app = FastAPI()

    @app.get("/ping")
    async def ping() -> dict:
        return {"ok": True}

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/ping")
        assert resp.status_code == 200

        spans = exporter.get_finished_spans()
        assert len(spans) >= 1  # 请求产生了 trace
        assert any("ping" in (span.name or "").lower() for span in spans)
    finally:
        FastAPIInstrumentor.uninstrument_app(app)


def test_build_tracer_provider_with_custom_exporter() -> None:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app.telemetry.otel import build_tracer_provider

    provider = build_tracer_provider("yesup-agent-runtime", exporter=InMemorySpanExporter())
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("unit"):
        pass
    provider.force_flush()
    assert provider is not None


def test_setup_otel_instruments_asyncpg_for_db_spans() -> None:
    """DB 查询必须出 span。

    耗时大头常在 Postgres —— 本地实测 /v1/agent/news 单次 564~1273ms，几乎全在
    DB 上。不接 asyncpg，链路树就在这里断一层，看到的只是「端点很慢」，看不到
    慢在哪条查询。
    """

    from fastapi import FastAPI
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor

    import app.telemetry.otel as otel_module
    from app.config import Settings

    instrumented_app = FastAPI()
    settings = Settings(env="local", provider_mode="fake", otel_enabled=True)

    # setup_otel 用模块级 _configured 做一次性守卫，测试里要显式复位再还原。
    original_configured = otel_module._configured
    otel_module._configured = False
    try:
        otel_module.setup_otel(instrumented_app, settings)
        assert AsyncPGInstrumentor().is_instrumented_by_opentelemetry
    finally:
        AsyncPGInstrumentor().uninstrument()
        HTTPXClientInstrumentor().uninstrument()
        LoggingInstrumentor().uninstrument()
        FastAPIInstrumentor.uninstrument_app(instrumented_app)
        otel_module._configured = original_configured


# --------------------------------------------------------------------------
# 深度链路：Redis 自动埋点 / Tool 专项 span / LLM GenAI 语义
# --------------------------------------------------------------------------


def _in_memory_tracer():
    """独立 provider + InMemory exporter，不碰全局 provider（只能设一次）。"""

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return provider.get_tracer("test"), exporter


def test_setup_otel_instruments_redis_for_session_and_lock_spans() -> None:
    """Session 读写、轮次锁、限流都压在 Redis 上，不埋这层就分不清慢在哪。"""

    from fastapi import FastAPI
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    import app.telemetry.otel as otel_module
    from app.config import Settings

    instrumented_app = FastAPI()
    settings = Settings(env="local", provider_mode="fake", otel_enabled=True)

    original_configured = otel_module._configured
    otel_module._configured = False
    try:
        otel_module.setup_otel(instrumented_app, settings)
        assert RedisInstrumentor().is_instrumented_by_opentelemetry
    finally:
        RedisInstrumentor().uninstrument()
        AsyncPGInstrumentor().uninstrument()
        HTTPXClientInstrumentor().uninstrument()
        LoggingInstrumentor().uninstrument()
        FastAPIInstrumentor.uninstrument_app(instrumented_app)
        otel_module._configured = original_configured


def test_tool_span_carries_name_session_and_args_hash(monkeypatch) -> None:
    from app.telemetry import spans

    tracer, exporter = _in_memory_tracer()
    monkeypatch.setattr(spans, "_TRACER", tracer)

    with spans.tool_span("get_positions", session_id="sess_1", args_hash="ab12") as span:
        spans.set_tool_status(span, "ok")

    (recorded,) = exporter.get_finished_spans()
    assert recorded.name == "tool.get_positions"
    assert recorded.attributes[spans.TOOL_NAME] == "get_positions"
    assert recorded.attributes[spans.TOOL_SESSION_ID] == "sess_1"
    assert recorded.attributes[spans.TOOL_ARGS_HASH] == "ab12"
    assert recorded.attributes[spans.TOOL_STATUS] == "ok"


def test_tool_span_maps_audit_status_to_the_span_vocabulary(monkeypatch) -> None:
    """审计写 tool_error，链路收敛成 error；rejected 两边一致。"""

    from app.telemetry import spans

    tracer, exporter = _in_memory_tracer()
    monkeypatch.setattr(spans, "_TRACER", tracer)

    for audit_status, expected in (("tool_error", "error"), ("rejected", "rejected")):
        with spans.tool_span("create_trade_draft", session_id="s", args_hash="h") as span:
            spans.set_tool_status(span, audit_status)

    statuses = [s.attributes[spans.TOOL_STATUS] for s in exporter.get_finished_spans()]
    assert statuses == ["error", "rejected"]


def test_tool_span_never_records_raw_arguments(monkeypatch) -> None:
    """参数原文里有金额与账户标识 —— 与审计同一条红线，链路只准落 hash。"""

    from app.telemetry import spans

    tracer, exporter = _in_memory_tracer()
    monkeypatch.setattr(spans, "_TRACER", tracer)

    with spans.tool_span("create_trade_draft", session_id="s", args_hash="deadbeef") as span:
        spans.set_tool_status(span, "ok")

    (recorded,) = exporter.get_finished_spans()
    rendered = " ".join(f"{k}={v}" for k, v in recorded.attributes.items())
    assert "deadbeef" in rendered
    for leak in ("instrument_id", "notional", "price", "leverage", "user_id"):
        assert leak not in rendered, leak


def test_llm_span_uses_genai_semantics(monkeypatch) -> None:
    from app.agents.llm import LLMUsage
    from app.telemetry import spans

    tracer, exporter = _in_memory_tracer()
    monkeypatch.setattr(spans, "_TRACER", tracer)

    usage = LLMUsage(input_tokens=5608, output_tokens=124, cache_read_tokens=0, cache_write_tokens=0)
    with spans.llm_span("openai:gpt-5") as span:
        spans.record_llm_usage(span, response_model="gpt-5-2026", usage=usage)

    (recorded,) = exporter.get_finished_spans()
    assert recorded.name == "chat openai:gpt-5"
    assert recorded.attributes[spans.GEN_AI_OPERATION_NAME] == "chat"
    assert recorded.attributes[spans.GEN_AI_REQUEST_MODEL] == "openai:gpt-5"
    assert recorded.attributes[spans.GEN_AI_RESPONSE_MODEL] == "gpt-5-2026"
    assert recorded.attributes[spans.GEN_AI_USAGE_INPUT_TOKENS] == 5608
    assert recorded.attributes[spans.GEN_AI_USAGE_OUTPUT_TOKENS] == 124


def test_llm_span_omits_usage_when_the_provider_reports_none(monkeypatch) -> None:
    """拿不到真实用量就空着 —— 与 LLMResult.usage 同一条红线，绝不编数字。"""

    from app.telemetry import spans

    tracer, exporter = _in_memory_tracer()
    monkeypatch.setattr(spans, "_TRACER", tracer)

    with spans.llm_span("openai:gpt-5") as span:
        spans.record_llm_usage(span, response_model=None, usage=None)

    (recorded,) = exporter.get_finished_spans()
    assert spans.GEN_AI_USAGE_INPUT_TOKENS not in recorded.attributes
    assert spans.GEN_AI_USAGE_OUTPUT_TOKENS not in recorded.attributes
    assert spans.GEN_AI_RESPONSE_MODEL not in recorded.attributes


def test_usage_attached_after_the_span_closed_is_silently_lost(monkeypatch) -> None:
    """踩过的坑：span 关掉后 set_attribute 是静默 no-op，用量会悄悄丢还不报错。

    这条测试把这个行为钉住 —— 它是 llm.py 里「必须在 with 块内挂属性」那段
    注释的依据；哪天有人把 record_llm_usage 挪到块外，靠的就是这个认知。
    """

    from app.agents.llm import LLMUsage
    from app.telemetry import spans

    tracer, exporter = _in_memory_tracer()
    monkeypatch.setattr(spans, "_TRACER", tracer)

    with spans.llm_span("openai:gpt-5") as span:
        pass
    spans.record_llm_usage(span, response_model="x", usage=LLMUsage(input_tokens=1, output_tokens=2))

    (recorded,) = exporter.get_finished_spans()
    assert spans.GEN_AI_USAGE_INPUT_TOKENS not in recorded.attributes


async def test_tool_span_is_emitted_on_the_real_execution_path(monkeypatch) -> None:
    """助手单测只证明助手对；这条证明它真的接进了受控执行路径。

    走完整的 POST /api/voice/tools/execute，而不是直接调助手 —— 接线漏了、
    或者哪天有人把 with 拆掉，单测照样绿，只有这条会红。
    """

    import httpx

    from app.main import app as main_app
    from app.telemetry import spans

    tracer, exporter = _in_memory_tracer()
    monkeypatch.setattr(spans, "_TRACER", tracer)

    headers = {"Authorization": "Bearer test-user:otel-u1"}
    transport = httpx.ASGITransport(app=main_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/v1/agent/sessions", json={"modality": "both"}, headers=headers
        )
        assert created.status_code == 201, created.text
        response = await client.post(
            "/api/voice/tools/execute",
            json={
                "session_id": created.json()["session_id"],
                "tool_call_id": "call_otel",
                "name": "get_positions",
                "arguments": {},
            },
            headers=headers,
        )
    assert response.status_code == 200, response.text

    tool_spans = [s for s in exporter.get_finished_spans() if s.name.startswith("tool.")]
    assert len(tool_spans) == 1, [s.name for s in exporter.get_finished_spans()]
    (recorded,) = tool_spans
    assert recorded.name == "tool.get_positions"
    assert recorded.attributes[spans.TOOL_NAME] == "get_positions"
    assert recorded.attributes[spans.TOOL_STATUS] == "ok"
    assert recorded.attributes[spans.TOOL_SESSION_ID].startswith("sess_")
    assert recorded.attributes[spans.TOOL_ARGS_HASH]
