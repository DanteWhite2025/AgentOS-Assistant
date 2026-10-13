"""OpenTelemetry 接线。

otel_enabled=True 时启用：FastAPI/httpx 自动埋点 + 日志注入 trace_id。
配了 OTLP endpoint 就导出到 collector；否则打到控制台（本地可见，非外部依赖）。
GenAI semconv 未 Stable，gen_ai.* 语义后续封装在本模块（tech-stack-assessment §7）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from app.config import Settings

_configured = False


def build_tracer_provider(service_name: str, otlp_endpoint: str = "", exporter: Any = None):
    """构造 TracerProvider。exporter 显式传入时优先（测试用 InMemorySpanExporter）。"""
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter is None:
        if otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        else:
            exporter = ConsoleSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def setup_otel(app: FastAPI, settings: Settings) -> None:
    """按需接入 FastAPI / httpx 自动埋点 + 日志 trace 关联。otel_enabled=False 时 no-op。"""
    global _configured
    if not settings.otel_enabled or _configured:
        return

    from opentelemetry import trace
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.logging import LoggingInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    provider = build_tracer_provider(settings.app_name, settings.otel_exporter_otlp_endpoint)
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)  # core/OpenAI 出站调用埋点
    # DB 埋点打在 asyncpg 驱动层，不是 SQLAlchemy 层：SQLAlchemy async 底下走的
    # 就是 asyncpg，补这一层链路树才不会在「端点很慢」处断掉，能看到慢在哪条查询。
    AsyncPGInstrumentor().instrument(tracer_provider=provider)
    # Session 读写、轮次锁、限流都压在 Redis 上：会话卡顿时首先要能分清是
    # Redis 慢还是下游慢，这一层不埋就只能靠猜。
    RedisInstrumentor().instrument(tracer_provider=provider)
    LoggingInstrumentor().instrument(set_logging_format=True)  # 日志注入 otelTraceID/otelSpanID
    _configured = True
