"""业务语义的 span 助手。

``setup_otel()`` 负责的是**自动埋点**（FastAPI / httpx / asyncpg / redis）——
那些库自己知道该记什么。这里放的是只有业务才知道语义的两类 span：

* ``tool.{name}`` —— 一次受控工具执行，带上审计口径的 status
* ``chat {model}`` —— 一次 LLM 推理，挂 GenAI 标准语义属性

OTel 没启用时全是 no-op：``trace.get_tracer()`` 在没有 provider 时返回代理
tracer，start_as_current_span 产出非记录 span。所以调用点**不需要判断开关**，
也不需要 try/except 包住。

tracer 在模块加载时取一次即可：拿到的是 ProxyTracer，真正的 provider 由
``setup_otel()`` 之后设置，代理会自动跟上，不存在「取早了拿到 no-op」的问题。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace

_TRACER = trace.get_tracer(__name__)

# --- 受控工具 ------------------------------------------------------------

TOOL_NAME = "tool.name"
TOOL_SESSION_ID = "tool.session_id"
TOOL_ARGS_HASH = "tool.args_hash"
TOOL_STATUS = "tool.status"

# 审计表用的 status 口径与链路口径不完全一样：审计里写 tool_error，链路上按
# GenAI/通用惯例收敛成 error。这里做一次映射，避免两边各自演化后对不上。
_AUDIT_STATUS_TO_SPAN = {
    "ok": "ok",
    "rejected": "rejected",
    "tool_error": "error",
}


@contextmanager
def tool_span(tool_name: str, *, session_id: str, args_hash: str) -> Iterator[Any]:
    """把一次受控工具执行包成独立 span。

    只记 args_hash 不记参数原文 —— 与审计同一条红线（app/audit/hashing.py：
    只落 hash 与指针）。参数里有金额和账户标识，链路是给人看的，不能落原文。
    """

    with _TRACER.start_as_current_span(f"tool.{tool_name}") as span:
        span.set_attribute(TOOL_NAME, tool_name)
        span.set_attribute(TOOL_SESSION_ID, session_id)
        span.set_attribute(TOOL_ARGS_HASH, args_hash)
        yield span


def set_tool_status(span: Any, audit_status: str) -> None:
    """按审计口径回填 span 的 tool.status。未知状态原样落，不吞掉。"""

    span.set_attribute(TOOL_STATUS, _AUDIT_STATUS_TO_SPAN.get(audit_status, audit_status))


# --- LLM GenAI 语义 ------------------------------------------------------

GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
# 缓存两项不在 stable semconv 里，但对成本判断很关键（缓存读只要 1/10 价），
# 沿用 incubating 的命名，不自造。
GEN_AI_USAGE_CACHE_READ_TOKENS = "gen_ai.usage.cache_read_input_tokens"
GEN_AI_USAGE_CACHE_WRITE_TOKENS = "gen_ai.usage.cache_creation_input_tokens"


def request_model_name(model: Any) -> str:
    """模型可能是 "openai:gpt-5" 这样的字符串，也可能是 Model 实例（测试用）。"""

    if isinstance(model, str):
        return model
    return str(getattr(model, "model_name", None) or type(model).__name__)


@contextmanager
def llm_span(model: Any) -> Iterator[Any]:
    """一次 LLM 推理。span 名按 GenAI semconv 约定为 ``chat {model}``。"""

    request_model = request_model_name(model)
    with _TRACER.start_as_current_span(f"chat {request_model}") as span:
        span.set_attribute(GEN_AI_OPERATION_NAME, "chat")
        span.set_attribute(GEN_AI_REQUEST_MODEL, request_model)
        yield span


def record_llm_usage(span: Any, *, response_model: str | None, usage: Any) -> None:
    """把 token 用量挂到 span 上。

    ``usage`` 为 None 时只记 response_model —— 与 LLMResult 的红线一致：拿不到
    真实值就空着，绝不编一个数字进去（llm.py 的 LLMResult.usage 注释）。
    """

    if response_model:
        span.set_attribute(GEN_AI_RESPONSE_MODEL, response_model)
    if usage is None:
        return
    span.set_attribute(GEN_AI_USAGE_INPUT_TOKENS, usage.input_tokens)
    span.set_attribute(GEN_AI_USAGE_OUTPUT_TOKENS, usage.output_tokens)
    if usage.cache_read_tokens:
        span.set_attribute(GEN_AI_USAGE_CACHE_READ_TOKENS, usage.cache_read_tokens)
    if usage.cache_write_tokens:
        span.set_attribute(GEN_AI_USAGE_CACHE_WRITE_TOKENS, usage.cache_write_tokens)
