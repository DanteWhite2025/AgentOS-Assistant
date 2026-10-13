"""文本推理 Provider（可插拔）。

LLMProvider 抽象；FakeLLM 自测用（启发式）；PydanticAILLM 真实（需 pydantic-ai + key）。
语音不走这里（语音是 Realtime，单独处理）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from app.telemetry.spans import llm_span, record_llm_usage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMMessage:
    """一条对话消息。

    ``tool_calls`` 与 ``tool_call_id`` 是让 assistant 的工具调用和它的结果能配对
    起来的关键。没有它们，工具结果只能伪装成又一条用户消息——模型看不到「是我
    自己调的这个工具」，拿不到 provider 对 tool role 的专门对齐，多轮工具调用时
    也无法归因（容易重复调同一个工具）。
    """

    role: str  # system / user / assistant / tool
    content: str = ""
    # assistant 消息：模型本轮请求的工具调用（可能多个，并发执行）
    tool_calls: tuple[LLMToolCall, ...] = ()
    # tool 消息：这条结果回应的是哪一次调用
    tool_call_id: str = ""
    tool_name: str = ""
    # 这次工具是否失败（超时、查不到、上游异常）。映射成 ToolReturnPart 的
    # outcome 字段，部分 provider 有原生错误通道，比让模型读文案可靠。
    tool_failed: bool = False


@dataclass(frozen=True)
class LLMUsage:
    """一次调用的 token 消耗。

    命名刻意不叫 TokenUsage —— 计费侧另有同名类型，重名会在将来合并时踩坑。
    cache 两项对成本影响很大（缓存读只要 1/10 价），所以单独留位而不是合进 input。
    """

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True)
class LLMResult:
    text: str
    tool_calls: tuple[LLMToolCall, ...] = ()
    # 本轮的 token 消耗。拿不到真实值时保持 None —— 计费与成本分析宁可空着，
    # 也不能编一个数字进去。FakeLLM 永远是 None。
    usage: LLMUsage | None = None
    # 实际响应的模型名。按模型定价必须知道真实值：网关有多模型与 fallback，
    # config 里的配置值不可靠。带默认值 → 现有构造点零影响。
    response_model: str | None = None


@dataclass(frozen=True)
class LLMTextDelta:
    """一段文本增量。"""

    delta: str


@dataclass(frozen=True)
class LLMStreamDone:
    """流结束，带完整结果。

    工具调用和 usage 只在这里给：参数是逐块的残缺 JSON，中途拼不出完整对象；
    usage 由 provider 逐 chunk 累加，最终值在最后一个 chunk 里。
    """

    result: LLMResult


LLMStreamEvent = LLMTextDelta | LLMStreamDone


class LLMProvider(Protocol):
    async def complete(
        self, system: str, messages: tuple[LLMMessage, ...], tools: tuple[str, ...]
    ) -> LLMResult: ...

    def stream(
        self, system: str, messages: tuple[LLMMessage, ...], tools: tuple[str, ...]
    ) -> AsyncIterator[LLMStreamEvent]:
        """逐 token 产出，最后一个事件必定是 LLMStreamDone。

        不是 async 函数——async generator 函数调用直接返回迭代器，不需要 await。
        """
        ...


_TOOL_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("现货", "get_balances"),
    ("余额", "get_balances"),
    ("资产", "get_balances"),
    ("balance", "get_balances"),
    ("持仓", "get_positions"),
    ("仓位", "get_positions"),
    ("position", "get_positions"),
    ("账户", "get_account_summary"),
    ("行情", "market_tickers"),
    ("价格", "market_tickers"),
    ("price", "market_tickers"),
    ("风险", "get_risk_summary"),
)


def _match_tool(text: str, tools: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for keyword, tool in _TOOL_KEYWORDS:
        if keyword in lowered and tool in tools:
            return tool
    return None


class FakeLLM:
    """启发式假 LLM：命中关键词且工具在白名单则先发 tool_call；已有 tool 结果则给文本回复。"""

    async def complete(
        self, system: str, messages: tuple[LLMMessage, ...], tools: tuple[str, ...]
    ) -> LLMResult:
        has_tool_result = any(m.role == "tool" for m in messages)
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        if not has_tool_result:
            wanted = _match_tool(last_user, tools)
            if wanted:
                return LLMResult(text="", tool_calls=(LLMToolCall("call_fake_1", wanted, {}),))
        tool_ctx = next((m.content for m in messages if m.role == "tool"), "")
        if tool_ctx:
            return LLMResult(text=f"根据查询结果：{tool_ctx}")
        return LLMResult(text=f"（Fake LLM）收到：{last_user}")

    async def stream(
        self, system: str, messages: tuple[LLMMessage, ...], tools: tuple[str, ...]
    ) -> AsyncIterator[LLMStreamEvent]:
        """把 complete 的结果切成几段吐出来。

        Fake 没有真实的 token 流，切几段只是为了让消费方在自测时也能走到
        「收到多个 delta」的分支——真流式的价值在 PydanticAILLM 那一侧。
        """

        result = await self.complete(system, messages, tools)
        for index in range(0, len(result.text), 8):
            yield LLMTextDelta(delta=result.text[index : index + 8])
        yield LLMStreamDone(result=result)


class PydanticAILLM:
    """真实文本推理（Pydantic AI），用 direct.model_request 做单步调用，保持 LLMProvider 单步语义。

    model 可为模型名字符串（如 "openai:gpt-5"，运行时需 provider key）
    或 pydantic-ai Model 实例（测试用 FunctionModel，无需 key）。
    """

    def __init__(self, model: Any, registry: Any = None, max_output_tokens: int | None = None) -> None:
        self._model = model
        self._registry = registry
        # 实测踩过的坑：某些开放式问题（如"深度投研分析"）不设上限时，模型可能
        # 一次吐上万 output token、耗时数分钟，比任何合理的客户端超时都长，且
        # 没有别的熔断手段能提前拦住它。None 保持历史行为不变（不设上限）。
        self._max_output_tokens = max_output_tokens

    async def complete(
        self, system: str, messages: tuple[LLMMessage, ...], tools: tuple[str, ...]
    ) -> LLMResult:
        # 延迟导入，避免未装 pydantic-ai 时影响 FakeLLM 路径
        from pydantic_ai.direct import model_request
        from pydantic_ai.models import ModelRequestParameters

        history = _build_history(system, messages)
        function_tools = self._function_tools(tools)
        # 推理本身单独成 span：链路上要能一眼看出这一轮里模型花了多久、烧了多少
        # token。挂 GenAI 标准语义属性，而不是自造字段名。
        model_settings = self._model_settings()
        with llm_span(self._model) as span:
            response = await model_request(
                self._model,
                history,
                model_settings=model_settings,
                model_request_parameters=ModelRequestParameters(
                    function_tools=function_tools, allow_text_output=True
                ),
            )
            # 必须在 span 结束前挂属性：span 关掉之后 set_attribute 是静默 no-op，
            # 用量会悄悄丢，不报错。
            usage = _extract_usage(response)
            record_llm_usage(
                span, response_model=getattr(response, "model_name", None), usage=usage
            )

        _log_usage(getattr(response, "model_name", "unknown"), usage)
        return _to_result(response, usage=usage)

    async def stream(
        self, system: str, messages: tuple[LLMMessage, ...], tools: tuple[str, ...]
    ) -> AsyncIterator[LLMStreamEvent]:
        """真流式：把 token 增量逐个吐出来，最后给完整结果。

        与 complete 共用消息映射和工具定义，只是把 model_request 换成
        model_request_stream。三个顺序上的硬约束：

        1. **span 必须包住整个流**。写成「span 里建请求、span 外消费」的话，
           span 在第一个 token 之前就关了，之后 record_llm_usage 是静默 no-op，
           用量会悄悄丢——llm.py 里那条老注释警告的坑，被流式放大了。
        2. **usage 只有流跑完才准确**，provider 逐 chunk 累加，最终值在最后一个
           usage-only chunk 里。提前 break 会缺最后一块。
        3. **取消时要显式 cancel 上游**。只让 CancelledError 传播的话连接会关，
           但 provider 那边还在继续生成、继续计费。

        对调用方的契约：消费到 ``LLMStreamDone`` 为止的话什么都不用做，span 和
        连接在你看到它之前就已经关好了。只有**在收到终止事件之前就 break**
        （比如用户中途取消）才需要 aclose()——那时生成器停在某个 delta 上，
        用量记录还没跑。
        """

        from pydantic_ai import TextPart
        from pydantic_ai.direct import model_request_stream
        from pydantic_ai.messages import PartDeltaEvent, PartStartEvent, TextPartDelta
        from pydantic_ai.models import ModelRequestParameters

        history = _build_history(system, messages)
        function_tools = self._function_tools(tools)
        model_settings = self._model_settings()

        with llm_span(self._model) as span:
            async with model_request_stream(
                self._model,
                history,
                model_settings=model_settings,
                model_request_parameters=ModelRequestParameters(
                    function_tools=function_tools, allow_text_output=True
                ),
            ) as stream:
                try:
                    async for event in stream:
                        # 首块内容在 PartStartEvent 里，不是 delta——只处理 delta
                        # 会把开头几个字吃掉。
                        if isinstance(event, PartStartEvent):
                            if isinstance(event.part, TextPart) and event.part.content:
                                yield LLMTextDelta(delta=event.part.content)
                        # 末尾那个非空判断和 PartStartEvent 分支是同一个道理：
                        # provider 会发空 content_delta（心跳、角色声明、思考
                        # 占位），漏出去的后果是下游把「首字到了」判早——数字人
                        # 提前切 Speaking，「已思考 N 秒」也会算小。
                        elif (
                            isinstance(event, PartDeltaEvent)
                            and isinstance(event.delta, TextPartDelta)
                            and event.delta.content_delta
                        ):
                            yield LLMTextDelta(delta=event.delta.content_delta)
                except asyncio.CancelledError:
                    # 掐掉上游的生成与计费再往上抛。cancel 本身失败不能盖住原始的
                    # 取消信号——但也不能悄悄咽下去，否则某个 provider 的
                    # close_stream 真出了问题会永远看不见。
                    try:
                        await stream.cancel()
                    except Exception:
                        logger.warning("取消上游流失败，可能仍在计费", exc_info=True)
                    raise
                finally:
                    # 中断路径同样要记用量：被打断的那些轮次也烧了 token，
                    # 不记的话这部分成本完全不可见。get() 只读已累积的内存状态，
                    # 在退出 async with 之前调用是安全的。
                    response = stream.get()
                    usage = _extract_usage(response)
                    record_llm_usage(
                        span,
                        response_model=getattr(response, "model_name", None),
                        usage=usage,
                    )
                    _log_usage(getattr(response, "model_name", "unknown"), usage)

        # 终止事件必须 yield 在 with 块**之外**。放在里面的话，消费方收到它就
        # break（最自然的写法，因为它被文档为"最后一个事件"）会让生成器停在那个
        # yield 上，span 和 HTTP 连接都不关。清理只能等 GC，而 GC 跑在另一个
        # contextvars.Context 里，detach 会失败——后果不只是"晚一点关"，而是后续
        # 同一个 task 里开的无关 span 被错误地挂成这个悬空 span 的子节点，整棵
        # trace 树错乱。挪出来之后，span 关闭与消费方怎么写彻底无关。
        yield LLMStreamDone(result=_to_result(response, usage=usage))

    def _function_tools(self, tools: tuple[str, ...]) -> list[Any]:
        from pydantic_ai import ToolDefinition

        function_tools = []
        for name in tools:
            tool = self._registry.get(name) if self._registry is not None else None
            desc = tool.description if tool and tool.description else f"受控工具 {name}"
            params = (
                tool.parameters
                if tool and tool.parameters
                else {"type": "object", "additionalProperties": True}
            )
            function_tools.append(
                ToolDefinition(name=name, description=desc, parameters_json_schema=params)
            )
        return function_tools

    def _model_settings(self) -> Any:
        from pydantic_ai.settings import ModelSettings

        if self._max_output_tokens is None:
            return None
        return ModelSettings(max_tokens=self._max_output_tokens)


def _parse_tool_args(raw: Any) -> dict[str, Any]:
    """把 ToolCallPart.args 解析成 dict。

    流式下参数是逐块拼起来的 JSON 字符串，只有拼完整才能解析——所以这个函数
    的调用点必须在流结束之后，不能放进 delta 循环里。
    """

    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}
    return raw or {}


def _to_result(response: Any, *, usage: LLMUsage | None = None) -> LLMResult:
    """把 pydantic-ai 的 ModelResponse 收敛成 LLMResult。流式与非流式共用。

    调用方通常已经为 span 算过一次 usage，传进来复用——两次调用理论上等价，
    但没必要算两遍，也免得将来两个取值悄悄分叉。
    """

    from pydantic_ai import TextPart, ToolCallPart

    text_parts: list[str] = []
    tool_calls: list[LLMToolCall] = []
    for part in response.parts:
        if isinstance(part, ToolCallPart):
            tool_calls.append(
                LLMToolCall(
                    call_id=part.tool_call_id,
                    name=part.tool_name,
                    arguments=_parse_tool_args(part.args),
                )
            )
        elif isinstance(part, TextPart):
            text_parts.append(part.content)
    return LLMResult(
        text="".join(text_parts),
        tool_calls=tuple(tool_calls),
        usage=usage if usage is not None else _extract_usage(response),
        response_model=getattr(response, "model_name", None),
    )


def _log_usage(response_model: str, usage: LLMUsage | None) -> None:
    if usage is None:
        return
    # 成本可见性的唯一来源：没有这行，花了多少钱只能靠月底账单倒推。
    logger.info(
        "llm_usage model=%s input=%d output=%d cache_read=%d cache_write=%d",
        response_model,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
    )


def _build_history(system: str, messages: tuple[LLMMessage, ...]) -> list[Any]:
    """把扁平的消息序列映射成 pydantic-ai 的 ModelRequest / ModelResponse 交替结构。

    这是原生 tool 协议的落点。改造前所有内容被压进**一条** ModelRequest：system、
    用户问题、以及伪装成 `UserPromptPart("[工具结果] ...")` 的工具结果，assistant
    回合整个消失（旧映射循环只处理 user 和 tool 两种 role）。落到 OpenAI wire 上
    就是连续两条 role:"user"，模型不知道是自己调用了工具。

    现在按 role 切分：assistant 消息开一个 ModelResponse（携带 ToolCallPart），
    随后的 tool 结果进下一个 ModelRequest（ToolReturnPart，用同一个 tool_call_id
    配对）。system 只出现在第一条 ModelRequest 里。
    """

    from pydantic_ai import ModelRequest, ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.messages import SystemPromptPart, ToolReturnPart, UserPromptPart

    history: list[Any] = []
    pending: list[Any] = [SystemPromptPart(content=system)]

    def _flush() -> None:
        nonlocal pending
        if pending:
            history.append(ModelRequest(parts=pending))
            pending = []

    for message in messages:
        if message.role == "user":
            pending.append(UserPromptPart(content=message.content))
        elif message.role == "assistant":
            _flush()
            response_parts: list[Any] = [
                ToolCallPart(
                    tool_name=call.name,
                    args=call.arguments,
                    tool_call_id=call.call_id,
                )
                for call in message.tool_calls
            ]
            if message.content:
                response_parts.insert(0, TextPart(content=message.content))
            if response_parts:
                history.append(ModelResponse(parts=response_parts))
        elif message.role == "tool":
            if not message.tool_call_id:
                # 配不上任何调用的结果不该存在，但真出现时别造一个孤儿
                # ToolReturnPart——那会让 provider 直接拒掉整个请求。降级成
                # 普通文本，至少内容还能到模型手里。
                logger.warning(
                    "工具结果缺少 tool_call_id，降级为文本回灌 tool_name=%s",
                    message.tool_name or "unknown",
                )
                pending.append(UserPromptPart(content=f"[工具结果] {message.content}"))
                continue
            pending.append(
                ToolReturnPart(
                    tool_name=message.tool_name or "unknown_tool",
                    content=message.content,
                    tool_call_id=message.tool_call_id,
                    # 失败结果标成 failed 而不是靠 content 里的文字暗示：
                    # 部分 provider 会把它映射成原生错误通道（Anthropic 的
                    # is_error、Bedrock 的 status='error'），模型对这条信号的
                    # 理解比读一句「暂时无法获取数据」可靠得多。
                    outcome="failed" if message.tool_failed else "success",
                )
            )

    _flush()
    return history


def _extract_usage(response: Any) -> LLMUsage | None:
    """从 pydantic-ai 响应里取 token 用量。

    字段缺失或为 None 时返回 None 而不是塞 0：0 会被下游当成「这轮免费」，
    比缺值更危险。
    """

    raw = getattr(response, "usage", None)
    if raw is None:
        return None
    input_tokens = getattr(raw, "input_tokens", None)
    output_tokens = getattr(raw, "output_tokens", None)
    if input_tokens is None or output_tokens is None:
        return None
    return LLMUsage(
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        cache_read_tokens=int(getattr(raw, "cache_read_tokens", 0) or 0),
        cache_write_tokens=int(getattr(raw, "cache_write_tokens", 0) or 0),
    )
