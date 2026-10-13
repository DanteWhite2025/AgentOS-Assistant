"""通用 AgentRunner —— 单一 runner，按 profile 区分身份。

评审采纳（tech-stack-assessment.md §12.1）：不写四个子类，四类 agent 底层都是
"system prompt + 受控 tool 白名单 + 状态机"，用一个 AgentRunner + 不同 AgentProfile 配置。

一个文本回合的循环：LLM → (工具调用 → 执行 → 审计 → 回灌) → 直到 LLM 产出文本。
LLM 可见工具 = profile.tools ∩ session.scope.tools；执行再经 ToolExecutor 二次校验。
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import logging
import re
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.agents.llm import (
    LLMMessage,
    LLMProvider,
    LLMResult,
    LLMStreamDone,
    LLMTextDelta,
    LLMToolCall,
)
from app.audit.hashing import args_hash as compute_args_hash
from app.audit.store import AuditRecord, AuditStore
from app.policies.engine import PolicyViolationError
from app.runtime.coreference import anchor_search_query
from app.runtime.session import ConversationTurn, Session
from app.runtime.suggestion_split import SuggestionSplitter
from app.tools.agentos.trade_flow import StrategyBindingError, TradingReadinessError
from app.tools.executor import ToolExecutor
from app.usage.tool_providers import provider_for_tool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentProfile:
    """一个 agent 的身份配置。"""

    name: str  # avatar / monitor / strategy / fund_manager
    model: str
    system_prompt: str
    tools: tuple[str, ...]  # 该 agent 允许的工具白名单（scope 子集）


@dataclass(frozen=True)
class ExecutedTool:
    call_id: str
    name: str
    args_hash: str
    result: dict
    trade_draft_id: str | None


@dataclass(frozen=True)
class RunResult:
    text: str
    executed_tools: tuple[ExecutedTool, ...]
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class TurnTextDelta:
    """回合内的一段文本增量。"""

    delta: str


@dataclass(frozen=True)
class TurnStage:
    """回合内的阶段变化——「现在在干什么」。

    工具的开始与结束已经由 on_tool_start / on_tool_end 钩子覆盖，这里补的是
    钩子之间的两段静默：一轮刚开始时（模型还在判断要不要调工具）、以及工具
    全部返回后（模型在写最终答案）。这两段客户端此前完全看不见，只能干等。

    这是**状态**而不是事件流：后到的阶段覆盖先到的，多轮工具会重复报
    composing，客户端按「当前阶段」渲染即可。
    """

    stage: str


# 阶段名。客户端拿它拼 i18n key（stage.<name>），所以是对外契约的一部分，
# 改名等于破坏兼容。
STAGE_THINKING = "thinking"
STAGE_COMPOSING = "composing"


@dataclass(frozen=True)
class TurnDone:
    """回合结束，带完整结果。"""

    result: RunResult


TurnEvent = TurnTextDelta | TurnStage | TurnDone


# 交易草案工具：只创建 Draft，最终签名与广播权在用户手上。
_TRADE_DRAFT_TOOLS = frozenset(
    {
        "create_trade_draft",
        "create_spot_swap_draft",
        "create_perp_trade_draft",
        "agentos_create_trade_draft",
    }
)

# 有副作用的写工具。它们失败必须让用户知道——草案可能已经在 Core 侧建了，
# 偏好可能没写进库；沿用只读工具那套"就当没查到"的降级会让用户以为操作成功了。
# update_user_preference 走的是 memory_store.remember_profile 持久化写入，
# 同样属于这一档，不能因为它"看起来只是记个备注"就当只读处理。
_MUTATING_TOOLS = _TRADE_DRAFT_TOOLS | frozenset({"update_user_preference"})

_TRADE_TIMEOUT_TEXT = (
    "请求已发送但确认超时，由于需要保证交易安全，请稍后在界面查看状态。未确认的草稿不会自动成交。"
)
_TOOL_UNAVAILABLE_TEXT = "相关数据服务暂不可用，请稍后重试。"
_WRITE_FAILED_TEXT = "刚才这项修改没有保存成功，请稍后再试一次。"


def _write_failure_text(tool_name: str, *, timed_out: bool) -> str:
    """写工具失败时给用户的文案。

    交易草案与偏好写入的失败后果完全不同：前者状态不明、可能已在 Core 侧创建，
    必须引导用户去界面确认；后者只是没存上，说清楚"没保存成功"即可。共用一句
    "数据服务暂不可用"会让用户不知道自己的偏好到底记住了没有。
    """

    if tool_name not in _TRADE_DRAFT_TOOLS:
        return _WRITE_FAILED_TEXT
    return _TRADE_TIMEOUT_TEXT if timed_out else _TOOL_UNAVAILABLE_TEXT


@dataclass(frozen=True)
class ToolOutcome:
    """一次工具执行的结果，按「对用户的影响」分档，而不是按异常类型分。

    ``ok``        拿到数据，正常回灌给模型。
    ``no_data``   没拿到数据——超时、查不到、参数不对、上游异常都算。**不打断对话**：
                  把中性说明回灌给模型，让它如实告诉用户这次没查到并继续回答。
    ``blocked``   真正需要用户知道的阻塞：写工具失败、额度或杠杆超限。终止本轮，
                  ``user_text`` 就是最终回复。
    ``duplicate`` 模型重复调用同一个工具，属于防死循环保护，不是错误也不报错。

    ``reason`` 是内部错误码/原因，只进 blockers 与审计供排查，**绝不进用户文案**。

    ``call_id`` 让前端把终态对回它先前显示的那一行「正在查询…」。成功时也能从
    ``executed`` 里取到，但失败分支没有 ExecutedTool，所以单独留一个字段。
    """

    status: str
    call_id: str = ""
    executed: ExecutedTool | None = None
    output: Mapping[str, Any] | None = None
    tool_name: str = ""
    reason: str = ""
    user_text: str = ""
    model_text: str = ""


# 工具生命周期钩子。展示层用它把「正在查询…」显示出来再收成终态，所以两者
# 必须成对：发过 start 的调用一定要有 end，否则前端那一行永远转圈。
ToolStartHook = Callable[[str, str, Mapping[str, Any]], Awaitable[None]]
ToolEndHook = Callable[[ToolOutcome], Awaitable[None]]


def _finish(result: RunResult) -> list[TurnEvent]:
    """把一个终态收敛成事件序列。

    没有经过流式产出的文本（固定文案、被阻塞时的提示）也要作为增量吐一次，
    否则消费方会遇到"有的轮次有 delta、有的轮次只有终止事件"两种形态。
    已经流式吐过的文本不要重复——那种情况传空文本进来。
    """

    events: list[TurnEvent] = []
    if result.text:
        events.append(TurnTextDelta(delta=result.text))
    events.append(TurnDone(result=result))
    return events


def _log_titling_failure(task: asyncio.Task[None]) -> None:
    """把后台标题任务的异常取出来记一笔。

    不取的话 asyncio 会在任务被 GC 时打印 "exception was never retrieved"，
    形式比这条日志难读得多，而且脱离了 session 上下文。
    """

    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("会话标题任务异常，保留降级标题", exc_info=exc)


def _history_messages(turns: tuple[ConversationTurn, ...]) -> list[LLMMessage]:
    """把历史轮次摊成 user / assistant 交替的消息。

    只带文本，不带 tool_calls / tool 结果：那些数据属于当时那一轮，留在上下文
    里会被模型当成当前行情用（见 TECH_DESIGN.md §2）。
    """

    messages: list[LLMMessage] = []
    for turn in turns:
        messages.append(LLMMessage("user", turn.user_text))
        messages.append(LLMMessage("assistant", turn.assistant_text))
    return messages


def _tool_return_message(call: LLMToolCall, content: str, *, failed: bool = False) -> LLMMessage:
    """把一次工具调用的结果包成能与它配对的消息。

    ``tool_call_id`` 必须原样来自模型返回的那次调用——配对靠它字符串相等，
    自己造一个新 id 等于告诉模型「这个结果不属于你调的任何一次工具」。
    """

    return LLMMessage(
        "tool",
        content=content,
        tool_call_id=call.call_id,
        tool_name=call.name,
        tool_failed=failed,
    )


def _fallback_text(blockers: list[str], executed: list[ExecutedTool]) -> str:
    """步数耗尽或重复调用时的兜底回复。

    绝不回显 blockers 的内容——那里面是 TOOL_EXECUTION_ERROR 这类内部错误码，
    给运维排查用。旧实现直接 ``text = blockers[-1]``，等于把错误码当回复发给用户。
    """

    if executed and executed[-1].trade_draft_id:
        return "交易草稿已创建，正在等待你确认。"
    if blockers:
        return "这次没有查到相关信息，请稍后再试。"
    return "请提供更多参数或确认您的意图。"


# 工具结果回灌预算。原来统一硬切 500 字符：PR #60 把 Tavily 结果做到 5 条 ×
# 500 字，但模型只看得到前 500 个字符，而且是从 JSON 中间切断的破损转义串，
# 模型连"这里被截断了"都不知道 —— 联网搜到了却答得很水，一半原因在这里。
_TOOL_RESULT_DEFAULT_LIMIT = 2000
_TOOL_RESULT_LIMITS = {
    "ask_investment_expert": 6000,  # 证据包是本轮分析的全部原料，不能截
    "web_search": 4000,  # 5 条 × 500 字摘要 + 结构开销
    "get_balances": 1500,
    "get_positions": 1500,
    "get_account_summary": 1500,
    # executor 里这两个是同一账户查询的别名（app/tools/executor.py:261-295），
    # 不映射的话别名路径会悄悄漏回默认的 2000 上限。
    "agentos_get_balances": 1500,
    "agentos_get_positions": 1500,
}
_TRUNCATION_MARKER = "…（结果已截断）"


def _summarize(out: Mapping[str, Any], tool_name: str = "") -> str:
    limit = _TOOL_RESULT_LIMITS.get(tool_name, _TOOL_RESULT_DEFAULT_LIMIT)
    text = out.get("evidence_text")
    if not isinstance(text, str) or not text:
        text = json.dumps(out, ensure_ascii=False)
    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATION_MARKER


def _needs_market_type_clarification(text: str) -> bool:
    """Deterministically stop an ambiguous buy/sell request before any tool call."""

    normalized = text.lower()
    has_action = bool(re.search(r"(?:买|卖|buy|sell)", normalized))
    has_asset = bool(re.search(r"\b[A-Za-z][A-Za-z0-9]{1,9}\b", text))
    explicit_spot = any(value in normalized for value in ("现货", "spot", "swap", "兑换", "换成"))
    explicit_perp = any(
        value in normalized
        for value in ("永续", "perp", "合约", "杠杆", "仓位", "做多", "做空", "平仓", "平掉")
    )
    return has_action and has_asset and not explicit_spot and not explicit_perp


_TIME_SENSITIVE_PATTERN = re.compile(
    r"(?:最近|最新|今天|今日|本周|刚刚|新闻|动态|涨跌原因|为什么涨|为什么跌|latest|today|news|recent)",
    re.IGNORECASE,
)


def _looks_time_sensitive(text: str) -> bool:
    """粗粒度时效性判断，仅用于联网搜索开启时决定 Tavily 的 topic/time_range。

    这里故意用宽泛的词表——选错 topic（general vs news）代价很低，
    多给几个"今天/最近"类词触发 news 主题没有副作用。
    """

    return bool(_TIME_SENSITIVE_PATTERN.search(text))


_LIVE_DATA_REQUIRED_PATTERN = re.compile(
    r"(?:为什么涨|为什么跌|涨跌原因|突发|最新消息|最新新闻|刚刚|latest news|breaking news)",
    re.IGNORECASE,
)


def _requires_live_data(text: str) -> bool:
    """窄口径判断：问题是否明确要"解释一个具体的实时价格变动/突发资讯"。

    故意比 `_looks_time_sensitive` 窄很多——用来决定联网搜索关闭时要不要
    把 ask_investment_expert 从候选工具里拿掉。ask_investment_expert 的
    本职工作就是"宏观复盘/行情解读"，这类分析天然会提到"今天""最近"，
    如果沿用宽泛词表当排除条件，会把它自己的主战场也排除掉（尤其是它
    刚加了 Redis 缓存专门服务这类高频复盘问题）。只有问题明确指向
    "具体某次涨跌的原因"或"突发/最新资讯"时才排除——这类问题只靠训练记忆
    或旧缓存回答，本质是冒充最新分析；而"今天大盘复盘一下"这种泛化分析
    问题，ask_investment_expert 本来就能胜任，不该被一刀切排除。
    """

    return bool(_LIVE_DATA_REQUIRED_PATTERN.search(text))


_PRICE_QUERY_PATTERN = re.compile(
    r"(?:价格|多少钱|现价|实时价|当前价|最新价|报价|盘口|price|quote)",
    re.IGNORECASE,
)

_DEEP_RESEARCH_INTENT_PATTERN = re.compile(
    r"(?:为什么|原因|分析|走势|看法|复盘|预测|怎么看|建议|评估|点评|why|reason|analysis|trend|forecast)",
    re.IGNORECASE,
)


def _is_pure_price_query(text: str) -> bool:
    """窄口径判断：用户是否仅在单纯查询实时价格/报价，而非要求深度分析或涨跌归因。

    例如「BTC 永续合约当前价格是多少」「ETH 现在多少钱」属于纯查价，应物理排除
    投研分析工具（ask_investment_expert），避免大模型误选并强行套用「深度分析四步法」研报；
    而「BTC 为什么跌成这个价格」「分析一下 BTC 走势」带有深度分析意图，不属于纯查价。
    """

    if _DEEP_RESEARCH_INTENT_PATTERN.search(text):
        return False
    return bool(_PRICE_QUERY_PATTERN.search(text))


# 本轮一旦真的执行成功了证据类工具，就无条件追加一句深度分析激活指令——
# 不靠模型自己判断"这是不是深度分析场景"（话题分类的误判率远高于"消息列表
# 里有没有工具结果"这种客观事实），场景判断交给运行时的确定性检查。
_EVIDENCE_TOOLS = frozenset({"ask_investment_expert", "web_search"})
_DEEP_ANALYSIS_TRIGGER = (
    "【本轮已获得证据数据】你已经拿到 ask_investment_expert 证据包或 web_search 搜索结果，"
    "本轮必须按系统提示词中的「深度分析四步法」四段式输出，篇幅 500~800 字，"
    "每一条归因都必须挂上证据里的具体数字或事件，禁用套话负面清单里的句子。"
)
_NO_EVIDENCE_NOTICE = (
    "【本轮未获得有效证据】证据工具调用成功但没有查到任何内容，你手上没有实时行情或新闻数据。"
    "必须如实告诉用户这次没有查到相关信息，不得编造价格、涨跌幅、事件或来源，"
    "也不得用训练记忆冒充最新数据。"
)


def _has_usable_evidence(out: Mapping[str, Any]) -> bool:
    """判断证据工具的返回里是否真的有可引用的内容。

    「调用成功」不等于「查到了东西」：Tavily 什么都没搜到时返回的是
    ``{"answer": "", "results": []}``，证据包两路都没抓到时是 ``coverage``
    全 False——两者都不带 ``error`` 字段。只看 ``error`` 会把这类空结果当成
    有效证据，进而激活「必须写 500~800 字、每条归因挂具体数字」的四步法，
    等于逼模型在没有数据时硬编，正是四步法本身要消灭的问题。
    """

    if out.get("error"):
        return False
    # ask_investment_expert：证据包自带覆盖标记，行情与新闻只要有一路就算有证据
    # （另一路不可用时四步法仍能基于单边数据成立，不该退化成不分析）。
    coverage = out.get("coverage")
    if isinstance(coverage, Mapping):
        return any(bool(value) for value in coverage.values())
    # web_search：Tavily 与本地新闻库返回同构。answer 是 Tavily 的直答摘要，
    # 没有条目列表时它本身也是可引用内容，不能只看 results。
    if "results" in out or "answer" in out:
        return bool(out.get("results")) or bool(str(out.get("answer") or "").strip())
    # 其他形状：拿到渲染好的证据文本才算数。未知形状一律判定为无证据——
    # 宁可少写一次四步法，也不能激活它去逼模型编造。
    return bool(str(out.get("evidence_text") or "").strip())


def _check_trade_intent_completeness(text: str) -> str | None:
    """Ensure explicit perp trades have a price target or explicitly request current price."""
    normalized = text.lower().replace("lerverage", "leverage")
    is_perp_trade = bool(re.search(r"(?:做多|做空|long|short|平仓)", normalized)) and (
        bool(re.search(r"(?:杠杆|leverage|x|倍)", normalized))
        or bool(re.search(r"\b[a-z]{2,10}\b", normalized))
    )
    if not is_perp_trade:
        return None

    has_current_price = bool(
        re.search(r"(?:当前价|现价|市价|current price|market price)", normalized)
    )
    has_price_indicator = bool(
        re.search(r"(?:at|@|价格|price|限价|limit)\s*[:：]?\s*\d", normalized)
    )
    numbers = re.findall(r"\d+(?:\.\d+)?", normalized)
    has_multiple_numbers = len(numbers) >= 3

    if not has_current_price and not has_price_indicator and not has_multiple_numbers:
        return "当前只支持限价单。请告诉我目标价格，或者明确说“按当前价格构造 Draft”。"
    return None


class AgentRunner:
    def __init__(
        self,
        profile: AgentProfile,
        executor: ToolExecutor,
        llm: LLMProvider,
        audit: AuditStore,
        max_steps: int = 4,
        context_builder: Any = None,
        turn_timeout_seconds: float = 0.0,
        session_store: Any = None,
        conversation_store: Any = None,
        conversation_titler: Any = None,
        usage_recorder: Any = None,
        usage_provider: str = "anthropic",
    ) -> None:
        self._profile = profile
        self._executor = executor
        self._llm = llm
        self._audit = audit
        self._max_steps = max_steps
        # 整轮的时间上限。每一层各有各的超时（Tavily 5s、Core 10s + 重试、
        # LLM 120s），但没有任何一层管得住"加起来多久"。0 表示不限时。
        self._turn_timeout_seconds = turn_timeout_seconds
        # 长期记忆注入。缺省为 None 时保持既有单一 system prompt 行为不变。
        self._context_builder = context_builder
        # 回合结束后把这一轮写进会话历史。写在 runner 内部而不是两条 API 入口
        # 各写一次：那样 REST 与 WS 都得记得写，漏一条就是静默失效——正是这次
        # 「点追问 chip 答案讲了别人」的失效模式。缺省 None 时不写历史，仓库里
        # 大量直接构造 runner 的测试因此不受影响。
        self._session_store = session_store
        # 同一轮的耐久归档。与 _session_store 是两件事：那个是喂给下一轮模型的
        # 3 轮窗口（Redis，1 小时 TTL），这个是用户在「历史」里看到的完整记录
        # （PG，30 天）。缺省 None 时不归档。
        self._conversation_store = conversation_store
        # 首轮结束后给会话起个语义标题。缺省 None 时列表里就一直是首句截断值。
        self._conversation_titler = conversation_titler
        # 用量与成本记账。缺省 None 时不记账，仓库里大量直接构造 runner 的测试
        # 因此不受影响（与 _session_store / _conversation_store 同一模式）。
        self._usage_recorder = usage_recorder
        # 计价按 provider 归集。容器接线时由配置传入真实值。
        self._usage_provider = usage_provider
        # 持有 fire-and-forget 任务的强引用。不持有的话事件循环只保弱引用，
        # 任务可能在跑完前被 GC，表现为「标题偶尔生成不出来」且毫无日志。
        self._titler_tasks: set[asyncio.Task[None]] = set()

    @property
    def profile(self) -> AgentProfile:
        return self._profile

    async def _stream_llm(
        self, system: str, messages: tuple[LLMMessage, ...], tools: tuple[str, ...]
    ) -> AsyncGenerator[Any, None]:
        """调 provider 的流式接口，只实现了 complete 的 provider 自动退回。

        ``stream()`` 是后加到 LLMProvider 上的，仓库里（尤其测试里）有大量手写
        的 provider stub 只实现了 ``complete``。没有 ``stream`` 就退回一次性调用。

        降级路径**也要吐一次增量**：主路径（这一轮没调工具）拿到终止事件就直接
        收尾，不再补吐文本——因为正常情况下文本已经流过了。降级路径如果只给终止
        事件，那一轮就成了"有完整文本、零 delta"，消费方要面对两种形态。

        内层生成器用 aclosing 包住：``async for`` 提前退出**不会**自动关闭被迭代
        的生成器，漏了的话 provider 那边的 span 和 HTTP 连接会挂在原地等 GC。
        """

        stream_fn = getattr(self._llm, "stream", None)
        if stream_fn is None or not inspect.isasyncgenfunction(stream_fn):
            if stream_fn is not None:
                logger.warning(
                    "provider 的 stream 不是 async generator 函数，退回 complete: %r",
                    type(self._llm).__name__,
                )
            result = await self._llm.complete(system, messages, tools)
            if result.text:
                yield LLMTextDelta(delta=result.text)
            yield LLMStreamDone(result=result)
            return
        async with aclosing(stream_fn(system, messages, tools)) as events:
            async for event in events:
                yield event

    async def run_turn(
        self,
        session: Session,
        user_text: str,
        trace_id: str,
        now: datetime,
        *,
        user_bearer_token: str | None = None,
        idempotency_key: str | None = None,
        on_tool_start: ToolStartHook | None = None,
        on_tool_end: ToolEndHook | None = None,
        web_search: bool = False,
    ) -> RunResult:
        """跑完一整个回合，返回聚合结果。

        内部消费 run_turn_stream 收干净。保留这个签名是为了让既有调用方（REST
        /messages 的幂等与任务复用机制、以及大量测试）一行都不用改——那套机制
        依赖「一次 turn 有且只有一个可缓存、可重放的完整值」，与 async generator
        根本冲突：Task 包不了 generator，generator 也不能被两个消费者同时消费。
        """

        done: TurnDone | None = None
        async for event in self.run_turn_stream(
            session,
            user_text,
            trace_id,
            now,
            user_bearer_token=user_bearer_token,
            idempotency_key=idempotency_key,
            on_tool_start=on_tool_start,
            on_tool_end=on_tool_end,
            web_search=web_search,
        ):
            if isinstance(event, TurnDone):
                done = event
        if done is None:  # pragma: no cover - run_turn_stream 保证有终止事件
            raise RuntimeError("run_turn_stream 没有产出终止事件")
        return done.result

    def run_turn_stream(
        self,
        session: Session,
        user_text: str,
        trace_id: str,
        now: datetime,
        *,
        user_bearer_token: str | None = None,
        idempotency_key: str | None = None,
        on_tool_start: ToolStartHook | None = None,
        on_tool_end: ToolEndHook | None = None,
        web_search: bool = False,
    ) -> AsyncGenerator[TurnEvent, None]:
        """流式跑一个回合，带整轮时间上限。

        超时的处理见 app/runtime/turn_manager.with_turn_timeout：已经吐出去的
        内容照常保留，只是不再等下去。两条路径（WS 与 run_turn 聚合）都受益。
        """

        from app.runtime.turn_manager import with_turn_timeout

        # 在外层创建，让超时回调能读到「撞上预算之前已经做成了什么」。回合内部
        # 的局部变量对包装层不可见，自己拍一个空的 executed_tools 会把已经建好
        # 的交易草案从结果里抹掉。
        executed: list[ExecutedTool] = []
        blockers: list[str] = []

        def _on_timeout(text: str) -> RunResult:
            return RunResult(
                text=text,
                executed_tools=tuple(executed),
                blockers=(*blockers, "TURN_TIMEOUT"),
            )

        return self._persisting_history(
            with_turn_timeout(
                self._run_turn_events(
                    session,
                    user_text,
                    trace_id,
                    now,
                    executed=executed,
                    blockers=blockers,
                    user_bearer_token=user_bearer_token,
                    idempotency_key=idempotency_key,
                    on_tool_start=on_tool_start,
                    on_tool_end=on_tool_end,
                    web_search=web_search,
                ),
                timeout_seconds=self._turn_timeout_seconds,
                on_timeout=_on_timeout,
                trace_id=trace_id,
            ),
            session,
            user_text,
            now,
        )

    async def _record_llm_usage(
        self,
        *,
        session: Session,
        turn_id: str,
        step_index: int,
        result: LLMResult,
        now: datetime,
    ) -> None:
        """把这一步的 token 消耗记进用量流水。

        usage 为 None 时也要记（recorder 会落 usage_missing）——「空着」在成本
        核算里等于免费，会让 LLM 成本被系统性低估（spec §7.2）。
        """

        if self._usage_recorder is None:
            return
        usage = result.usage
        await self._usage_recorder.record_llm(
            turn_id=turn_id,
            step_index=step_index,
            user_id=session.user_id,
            session_id=session.session_id,
            agent_id=getattr(session, "agent_id", None),
            scene="chat",
            provider=self._usage_provider,
            model=result.response_model,
            input_tokens=None if usage is None else usage.input_tokens,
            output_tokens=None if usage is None else usage.output_tokens,
            cache_read_tokens=None if usage is None else usage.cache_read_tokens,
            cache_write_tokens=None if usage is None else usage.cache_write_tokens,
            occurred_at=now,
        )

    async def _record_tool_usage(
        self,
        *,
        session: Session,
        trace_id: str,
        turn_id: str,
        seq: int,
        call: Any,
        outcome: ToolOutcome,
        now: datetime,
    ) -> None:
        """把一次工具调用记进用量流水。

        记账收在 _execute_single 而不是 _run_tool 的六个 return 上，与 on_tool_end
        同一位置、同一理由：散着记，每加一个失败分支都得记得补一次，漏掉的那个
        就是账单里少掉的那笔。

        **调用发出即产生成本**：Tavily 这类按请求计费的供应商，超时与报错同样扣费，
        所以 ok / timeout / error 都要记。duplicate 例外——它在调 executor 之前就
        短路了，没有真实外部调用。
        """

        if self._usage_recorder is None or outcome.status == "duplicate":
            return
        await self._usage_recorder.record_external_api(
            trace_id=trace_id,
            tool_name=call.name,
            seq=seq,
            user_id=session.user_id,
            session_id=session.session_id,
            turn_id=turn_id,
            scene="chat",
            provider=provider_for_tool(call.name),
            occurred_at=now,
        )

    async def _persisting_history(
        self,
        events: AsyncGenerator[TurnEvent, None],
        session: Session,
        user_text: str,
        now: datetime,
    ) -> AsyncGenerator[TurnEvent, None]:
        """透传事件，并在终止事件之前把这一轮写进会话历史。

        写在 **yield TurnDone 之前**：消费方拿到终止事件后随时可能停止迭代，
        而 async for 提前退出会直接关掉这个生成器——写在之后就永远不执行。
        一次存储往返放在这里不影响首字延迟（那早就发生过了）。

        aclosing 是必须的：这条链底下套着 with_turn_timeout，再底下是 LLM 流
        （带 span 和 HTTP 连接）。不关的话上游还在生成、还在计费。
        """

        async with aclosing(events) as stream:
            async for event in stream:
                if isinstance(event, TurnDone):
                    await self._save_turn(session, user_text, event.result.text, now)
                yield event

    async def _save_turn(
        self, session: Session, user_text: str, answer: str, now: datetime
    ) -> None:
        """把这一轮写进两处历史。任何失败都只丢历史，绝不阻断回合。

        两处的职责不同，所以各写各的、互不拖累：会话窗口是喂给下一轮模型的
        3 轮上下文，耐久归档是用户在「历史」里翻的完整记录。一边挂了不该
        让另一边跟着丢。
        """

        if self._session_store is None and self._conversation_store is None:
            return
        # 追问区是给客户端渲染 chips 的，不是答案正文。留在历史里，下一轮模型
        # 会把它当正文读，甚至照着模仿上一轮的三条追问。
        splitter = SuggestionSplitter()
        emitted = splitter.feed(answer)
        tail, _ = splitter.finish()
        body = (emitted + tail).strip()
        if not body:
            # 空回合（超时且一个字都没吐）进历史，会让模型以为自己上一轮沉默了。
            return
        await self._archive_turn(session, user_text, body, now)
        await self._extend_session_window(session, user_text, body, now)

    async def _archive_turn(
        self, session: Session, user_text: str, body: str, now: datetime
    ) -> None:
        """写耐久归档。

        刻意排在会话窗口之前：下面那步会在会话过期时提前返回，而这一轮**确实
        发生过**，耐久记录不该因为运行时对象恰好过期就丢。
        """

        if self._conversation_store is None:
            return
        try:
            created = await self._conversation_store.record_turn(
                session.session_id, session.user_id, user_text, body, now
            )
        except Exception:
            logger.warning(
                "对话归档失败，本轮不进历史列表 session_id=%s",
                session.session_id,
                exc_info=True,
            )
            return
        if created:
            self._start_titling(session.session_id, user_text, body)

    def _start_titling(self, session_id: str, user_text: str, body: str) -> None:
        """只在建行那一轮起标题，且不等它。

        不等：统一网关单次实测 1.5~3 分钟，同步等于把回合拖到分钟级。
        只首轮：每轮都生成会把标题成本乘以轮数，还会让标题跟着最后一轮漂移
        ——用户眼里就是标题自己变来变去。
        """

        if self._conversation_titler is None:
            return
        task = asyncio.create_task(
            self._conversation_titler.generate(session_id, user_text, body)
        )
        self._titler_tasks.add(task)
        task.add_done_callback(self._titler_tasks.discard)
        task.add_done_callback(_log_titling_failure)

    async def _extend_session_window(
        self, session: Session, user_text: str, body: str, now: datetime
    ) -> None:
        """read-modify-write 会话的上下文窗口。"""

        if self._session_store is None:
            return
        try:
            latest = await self._session_store.get(session.session_id)
            if latest is None:
                # 会话已过期。凭手里的旧对象写回去等于把它复活，TTL 语义就乱了。
                return
            await self._session_store.update(
                latest.with_conversation_turn(now, ConversationTurn(user_text, body, now))
            )
        except Exception:
            logger.warning(
                "会话历史写入失败，本轮不进历史 session_id=%s", session.session_id, exc_info=True
            )

    async def _run_turn_events(
        self,
        session: Session,
        user_text: str,
        trace_id: str,
        now: datetime,
        *,
        executed: list[ExecutedTool],
        blockers: list[str],
        user_bearer_token: str | None = None,
        idempotency_key: str | None = None,
        on_tool_start: ToolStartHook | None = None,
        on_tool_end: ToolEndHook | None = None,
        web_search: bool = False,
    ) -> AsyncGenerator[TurnEvent, None]:
        """回合本体：文本增量逐个吐出，最后一个事件必定是 TurnDone。

        工具事件不走这条流——它们仍由 on_tool_start / on_tool_end 钩子推送，
        两条通道分离，WebSocket 侧不用在一个循环里分辨"这是文本还是工具"。

        所有出口都经 `_finish()` 收敛：其中两个（市场类型待澄清、交易意图不完整）
        在任何 LLM 调用之前就返回固定文案，它们同样要把文案作为增量吐出来，
        否则消费方得处理两种响应形态。
        """

        # 一轮的归并 id：本轮多步 LLM 调用与全部工具调用共用它，「一次聊天多少钱」
        # 靠它把一轮内的多笔成本加起来（spec D9）。刻意做成局部变量——不进任何
        # 函数签名，调用方一行都不用改。
        turn_id = "turn_" + uuid.uuid4().hex
        # 同名工具一轮内可能被调多次，seq 用来区分，否则第二次会撞上同一个
        # dedup_key 被当成重复丢掉，成本少算一半。
        tool_call_seq = itertools.count()

        import httpx

        # 排在两个「LLM 之前就返回」的早退出口之前：那两条路径也要有阶段事件，
        # 否则消费方要处理「这一轮压根没有阶段」的第三种形态。
        yield TurnStage(stage=STAGE_THINKING)

        scope = set(session.scope.tools)
        allowed = tuple(
            t for t in self._profile.tools if t in scope and (web_search or t != "web_search")
        )
        messages: list[LLMMessage] = [
            *_history_messages(session.turns),
            LLMMessage("user", user_text),
        ]
        executed_hashes: set[str] = set()
        deep_analysis_activated = False
        no_evidence_noticed = False

        if _needs_market_type_clarification(user_text):
            for event in _finish(
                RunResult(
                    text=("请确认你要交易现货还是永续合约；我不会根据资产名称替你选择交易类型。"),
                    executed_tools=(),
                )
            ):
                yield event
            return

        completeness_msg = _check_trade_intent_completeness(user_text)
        if completeness_msg:
            for event in _finish(RunResult(text=completeness_msg, executed_tools=())):
                yield event
            return

        system_prompt = self._profile.system_prompt
        if self._context_builder is not None:
            system_prompt = await self._context_builder.build_system_prompt(
                system_prompt,
                user_id=session.user_id,
                query=user_text,
            )
        if web_search:
            system_prompt += (
                "【本轮联网模式】用户已主动开启联网搜索。必须基于本轮提供的搜索结果回答，"
                "不得声称无法联网，也不得凭训练记忆替代实时结果。"
            )
        else:
            system_prompt += (
                "【本轮联网模式】用户未开启联网搜索，本轮不得调用或声称已调用互联网搜索。"
                "若问题必须依赖实时网络资料，请简短提示用户开启输入框旁的“联网搜索”。"
            )

        def _note_evidence(out: Mapping[str, Any]) -> None:
            """按本轮证据的有无，维护 system prompt 上那对互斥的深度分析指令。

            两句指令不能并存：一旦后续某个工具真的拿到了证据，必须撤掉先前
            追加的"未获得证据"提示，否则 prompt 里会同时挂着"如实说没查到"
            和"必须写 500~800 字四步法"，模型只能二选一地违背其中一条。
            """

            nonlocal system_prompt, deep_analysis_activated, no_evidence_noticed
            if _is_pure_price_query(user_text):
                # 纯价格查询不需要也不应当套用 500~800 字深度分析四步法研报模板
                return
            if _has_usable_evidence(out):
                if deep_analysis_activated:
                    return
                if no_evidence_noticed:
                    system_prompt = system_prompt.replace(_NO_EVIDENCE_NOTICE, "")
                    no_evidence_noticed = False
                system_prompt += _DEEP_ANALYSIS_TRIGGER
                deep_analysis_activated = True
            elif not deep_analysis_activated and not no_evidence_noticed:
                system_prompt += _NO_EVIDENCE_NOTICE
                no_evidence_noticed = True

        import asyncio

        async def _execute_single(call: Any) -> ToolOutcome:
            """执行一次工具，并保证生命周期钩子成对触发。

            终态统一在这里发，而不是散在 _run_tool 的六个 return 上——那样每加
            一个失败分支都得记得补一次通知，漏掉的那个就是前端永远转圈的那一行。
            duplicate 在 on_tool_start 之前就短路了，前端从没见过这次调用，所以
            它既没有 start 也不该有 end。
            """

            outcome = await _run_tool(call)
            if on_tool_end is not None and outcome.status != "duplicate":
                await _notify(on_tool_end, outcome)
            await self._record_tool_usage(
                session=session,
                trace_id=trace_id,
                turn_id=turn_id,
                seq=next(tool_call_seq),
                call=call,
                outcome=outcome,
                now=now,
            )
            return outcome

        async def _notify(hook: Any, *args: Any) -> None:
            """触发展示层钩子，异常一律吞掉只记日志。

            展示是锦上添花，绝不能因为它出问题就影响工具执行或整轮对话。
            并发批次尤其危险：asyncio.gather 不会取消兄弟协程，一个钩子抛异常
            会让 run_turn 立刻异常退出，而其余工具仍在后台跑完，再往一条已经
            收尾的连接上发消息——同一个 call_id 会收到两条状态冲突的结果。
            """

            try:
                await hook(*args)
            except Exception:
                logger.warning(
                    "tool_lifecycle_hook_failed trace_id=%s session_id=%s",
                    trace_id,
                    session.session_id,
                    exc_info=True,
                )

        async def _run_tool(call: Any) -> ToolOutcome:
            call_hash = compute_args_hash(call.arguments)
            call_fingerprint = f"{call.name}:{call_hash}"
            if call_fingerprint in executed_hashes:
                return ToolOutcome(status="duplicate", tool_name=call.name)
            executed_hashes.add(call_fingerprint)
            is_mutating = call.name in _MUTATING_TOOLS

            if on_tool_start is not None:
                await _notify(on_tool_start, call.call_id, call.name, call.arguments)

            try:
                out = await self._executor.execute(
                    call.name,
                    session.user_id,
                    call.arguments,
                    list(session.scope.tools),
                    user_bearer_token=user_bearer_token,
                    idempotency_key=idempotency_key,
                )
            except (httpx.TimeoutException, httpx.NetworkError):
                reason = "网络请求超时"
                await self._audit.record_tool_call(
                    AuditRecord(
                        trace_id=trace_id,
                        session_id=session.session_id,
                        tool_name=call.name,
                        args_hash=call_hash,
                        result_ref="timeout",
                        created_at=now,
                        status="rejected",
                    )
                )
                if is_mutating:
                    # 写工具超时状态不明——草案可能已经在 Core 侧创建，只能让用户
                    # 去界面确认，不能让模型接着往下编。
                    return ToolOutcome(
                        status="blocked",
                        call_id=call.call_id,
                        tool_name=call.name,
                        reason=reason,
                        user_text=_write_failure_text(call.name, timed_out=True),
                    )
                return ToolOutcome(
                    status="no_data",
                    call_id=call.call_id,
                    tool_name=call.name,
                    reason=reason,
                    model_text=(
                        f"[工具结果] {call.name} 请求超时，未获取到数据。"
                        "请如实告诉用户这次没有查到，不要编造数据或来源。"
                    ),
                )
            except (
                PolicyViolationError,
                TradingReadinessError,
                StrategyBindingError,
                ValueError,
                TypeError,
            ) as exc:
                reason = exc.reason if isinstance(exc, PolicyViolationError) else str(exc)
                await self._audit.record_tool_call(
                    AuditRecord(
                        trace_id=trace_id,
                        session_id=session.session_id,
                        tool_name=call.name,
                        args_hash=call_hash,
                        result_ref=reason,
                        created_at=now,
                        status="rejected",
                    )
                )
                if "额度" in reason or "LIMIT" in reason:
                    safe_text = "该请求超过 Core 当前允许的单笔交易额度。请求参数没有被修改，也没有创建 Draft。"
                elif "杠杆" in reason:
                    safe_text = (
                        "该请求超过 Core 当前允许的杠杆倍数。请求参数没有被修改，也没有创建 Draft。"
                    )
                else:
                    safe_text = f"请求被拒绝：{reason}"
                if is_mutating:
                    return ToolOutcome(
                        status="blocked",
                        call_id=call.call_id,
                        tool_name=call.name,
                        reason=reason,
                        user_text=safe_text,
                    )
                # 只读工具的校验失败大多是模型把参数填错了（"market_tickers
                # requires exactly one array field" 之类）。这类英文技术串对用户
                # 毫无意义，旧实现却把它拼进"请求被拒绝：{reason}"直接透出；
                # 回灌给模型才有用——它能据此改正下一次调用。
                return ToolOutcome(
                    status="no_data",
                    call_id=call.call_id,
                    tool_name=call.name,
                    reason=reason,
                    model_text=(
                        f"[工具结果] {call.name} 调用未成功：{reason}。"
                        "可以修正参数后再试一次，或者如实告诉用户这次没有查到。"
                    ),
                )
            except Exception:
                # str(exc) 可能带内部接口 URL 和底层 403 堆栈（如 httpx.HTTPStatusError）——
                # 这三个出口都会被 Mobile 端直接看到：聊天文本、SingleTurnResponse.blockers
                # （app/api/agent.py）、/history 的 result_ref（get_history 直接回显
                # AuditRecord.result_ref），所以原始异常只能进服务器日志，绝不能带出这三处。
                logger.exception(
                    "tool_execution_unexpected_error trace_id=%s session_id=%s tool_name=%s",
                    trace_id,
                    session.session_id,
                    call.name,
                )
                safe_reason = "TOOL_EXECUTION_ERROR"
                await self._audit.record_tool_call(
                    AuditRecord(
                        trace_id=trace_id,
                        session_id=session.session_id,
                        tool_name=call.name,
                        args_hash=call_hash,
                        result_ref=safe_reason,
                        created_at=now,
                        status="error",
                    )
                )
                if is_mutating:
                    return ToolOutcome(
                        status="blocked",
                        call_id=call.call_id,
                        tool_name=call.name,
                        reason=safe_reason,
                        user_text=_write_failure_text(call.name, timed_out=False),
                    )
                # model_text 同样只能用工具名和固定文案拼——原始异常里可能带内部
                # 接口 URL 与鉴权细节，回灌给模型等于让它有机会复述给用户。
                return ToolOutcome(
                    status="no_data",
                    call_id=call.call_id,
                    tool_name=call.name,
                    reason=safe_reason,
                    model_text=(
                        f"[工具结果] {call.name} 暂时无法获取数据。"
                        "请如实告诉用户这次没有查到，不要编造数据或来源。"
                    ),
                )

            draft_id = out.get("trade_draft_id")
            result_ref = draft_id or f"{call.name}:{session.session_id}"
            await self._audit.record_tool_call(
                AuditRecord(
                    trace_id=trace_id,
                    session_id=session.session_id,
                    tool_name=call.name,
                    args_hash=call_hash,
                    result_ref=result_ref,
                    created_at=now,
                )
            )
            return ToolOutcome(
                status="ok",
                call_id=call.call_id,
                executed=ExecutedTool(call.call_id, call.name, call_hash, out, draft_id),
                output=out,
                tool_name=call.name,
            )

        current_allowed = allowed
        if not web_search and "web_search" in scope and _requires_live_data(user_text):
            # 投研分析（ask_investment_expert）与联网搜索的 Prompt 触发词本来就
            # 有重叠（"宏观复盘"vs"为什么涨跌"），软提醒管不住模型偶尔选错。
            # 只在问题明确需要实时资讯（"为什么涨/跌""突发/最新消息"）时才
            # 拿掉 ask_investment_expert，逼模型走系统提示词里"请提示用户开启
            # 联网搜索"这条路径；泛化的复盘/分析问题（哪怕提到"今天""最近"）
            # 仍留给它处理，那正是它的本职工作。
            # "web_search" in scope 是必要前提：如果这个会话本来就没有联网
            # 搜索能力，排除掉 ask_investment_expert 只会让用户被指向一个
            # 根本不存在的开关，没有任何帮助。
            current_allowed = tuple(t for t in current_allowed if t != "ask_investment_expert")
        if _is_pure_price_query(user_text) and any(
            t in current_allowed for t in ("get_market_price", "market_tickers")
        ):
            # 纯查价格场景：必须且仅走行情专用工具（get_market_price/market_tickers），
            # 物理剔除投研证据抓取工具，避免大模型误选并强行套用深度分析四步法。
            current_allowed = tuple(t for t in current_allowed if t != "ask_investment_expert")
        if web_search:
            if "web_search" not in current_allowed:
                for event in _finish(
                    RunResult(
                        text="当前联网搜索不可用，请稍后重试。",
                        executed_tools=(),
                        blockers=("WEB_SEARCH_NOT_AVAILABLE",),
                    )
                ):
                    yield event
                return
            recent_query = _looks_time_sensitive(user_text)
            search_args: dict[str, Any] = {
                # 不能直接用 user_text：带指代的提问（"他一季度做空了哪些股票"）
                # 拿去检索，命中的是词频最高的那个人，而提示词还要求"必须基于
                # 本轮搜索结果回答"——模型有了上下文也会被错误证据带跑。
                "query": anchor_search_query(user_text, session.turns),
                "topic": "news" if recent_query else "general",
            }
            if recent_query:
                search_args["time_range"] = "week"
            forced_call = LLMToolCall("call_required_web_search", "web_search", search_args)
            outcome = await _execute_single(forced_call)
            if outcome.status == "blocked":
                for event in _finish(
                    RunResult(
                        text=outcome.user_text,
                        executed_tools=(),
                        blockers=(outcome.reason,),
                    )
                ):
                    yield event
                return
            if outcome.status == "ok" and outcome.executed is not None:
                output = outcome.output or {}
                executed.append(outcome.executed)
                forced_content = _summarize(output, outcome.tool_name)
                # Tavily 的 HTTP/网络失败返回的是 {"error": ...} 而不是抛异常，
                # status 仍然是 ok。不再为此直接报错终止——照常回灌，_note_evidence
                # 会判定为无证据并要求模型如实说没搜到。
                _note_evidence(output)
            else:
                # 搜索没成功也不阻塞：用户开着联网开关问了问题，模型该做的是
                # 如实说这次没搜到，而不是收到一句"联网搜索超时，请稍后重试"。
                blockers.append(outcome.reason or "WEB_SEARCH_INCOMPLETE")
                forced_content = (
                    outcome.model_text or "[工具结果] web_search 未能完成，没有获取到数据。"
                )
                _note_evidence({})
            # 这次搜索是运行时替模型决定的，但对模型来说它必须看起来像自己调的——
            # 否则历史里会出现一条没有来源的工具结果，配对关系又断了。
            messages.append(LLMMessage("assistant", tool_calls=(forced_call,)))
            messages.append(
                _tool_return_message(forced_call, forced_content, failed=outcome.status != "ok")
            )
            current_allowed = tuple(t for t in current_allowed if t != "web_search")
        for step in range(self._max_steps):
            # 乐观流式：这一轮会不会调工具，要等流结束才知道，所以文本增量边到
            # 边吐。模型在调工具前先说一句「我查一下行情」是常见行为，显示出来
            # 反而更自然（ChatGPT 也是这个表现）。
            #
            # 代价是：这类前言进了 delta 流，但不进最终的 RunResult.text（那里
            # 只有最后一轮的答案）。消费方要按 delta 累积来渲染完整回复，不能拿
            # RunResult.text 去覆盖已经显示的内容。
            result: LLMResult | None = None
            async with aclosing(
                self._stream_llm(system_prompt, tuple(messages), current_allowed)
            ) as chunks:
                async for chunk in chunks:
                    if isinstance(chunk, LLMTextDelta):
                        yield TurnTextDelta(delta=chunk.delta)
                    elif isinstance(chunk, LLMStreamDone):
                        result = chunk.result
            if result is None:  # pragma: no cover - provider 保证有终止事件
                raise RuntimeError("LLM 流没有产出终止事件")
            await self._record_llm_usage(
                session=session, turn_id=turn_id, step_index=step, result=result, now=now
            )

            if not result.tool_calls:
                yield TurnDone(
                    result=RunResult(
                        text=result.text,
                        executed_tools=tuple(executed),
                        blockers=tuple(blockers),
                    )
                )
                return

            tool_calls = result.tool_calls
            is_all_readonly = all(c.name not in _MUTATING_TOOLS for c in tool_calls)

            if is_all_readonly and len(tool_calls) > 1:
                # 只读工具并发执行（消灭多个分析/搜索工具串行等待耗时）
                raw_results = await asyncio.gather(*[_execute_single(c) for c in tool_calls])
            else:
                raw_results = []
                for c in tool_calls:
                    res = await _execute_single(c)
                    raw_results.append(res)
                    # 只有阻塞性结果才提前中断这一批；no_data 不影响后面的工具，
                    # 它们各自还有机会拿到数据。
                    if res.status in ("blocked", "duplicate"):
                        break

            # 并发批次里所有工具都已经执行完（副作用和审计都已落地），所以先把
            # 每一条结果都收进 executed/messages，再统一判断要不要终止——遇到
            # 终止态就地 return 会把排在它后面、其实已经成功的兄弟结果丢掉。
            halting: ToolOutcome | None = None
            # 本轮的调用与结果先攒起来，最后合成「一个 assistant 回合 + 一批结果」。
            # 模型这一轮是一次性请求了这些工具的，回灌时也该是一个 ModelResponse
            # 带多个 ToolCallPart，而不是拆成 N 个各自独立的 assistant 回合。
            calls_by_id = {call.call_id: call for call in tool_calls}
            turn_calls: list[LLMToolCall] = []
            turn_returns: list[LLMMessage] = []

            for outcome in raw_results:
                call = calls_by_id.get(outcome.call_id)
                if call is None and outcome.status in ("ok", "no_data"):
                    # 目前不可达（call_id 就是从这批 tool_calls 传下去的），但真发生时
                    # 后果很隐蔽：工具执行了、审计记了、_note_evidence 也照常注入了
                    # 「你已拿到证据」，唯独这次调用不会出现在发给模型的历史里——
                    # 正是本次改造要根治的「历史与 system prompt 不一致」的新变种。
                    logger.warning(
                        "tool_outcome_without_matching_call call_id=%s tool_name=%s trace_id=%s",
                        outcome.call_id,
                        outcome.tool_name,
                        trace_id,
                    )
                if outcome.status == "ok" and outcome.executed is not None:
                    out = outcome.output or {}
                    executed.append(outcome.executed)
                    if call is not None:
                        turn_calls.append(call)
                        turn_returns.append(
                            _tool_return_message(call, _summarize(out, outcome.tool_name))
                        )
                    # "工具调用成功"不等于"拿到了证据"：executor 的校验失败返回
                    # {"error": ...}、Tavily 什么都没搜到返回 {"results": []}、
                    # 证据包两路皆空返回 coverage 全 False——后两者都不带 error
                    # 字段。判空交给 _has_usable_evidence 统一处理。
                    if outcome.tool_name in _EVIDENCE_TOOLS:
                        _note_evidence(out)
                elif outcome.status == "no_data":
                    # 降级不等于静默：用户看不到报错，但 blockers 留痕供排查。
                    blockers.append(outcome.reason)
                    if call is not None:
                        turn_calls.append(call)
                        turn_returns.append(
                            _tool_return_message(call, outcome.model_text, failed=True)
                        )
                    if outcome.tool_name in _EVIDENCE_TOOLS:
                        _note_evidence({})
                elif outcome.status in ("blocked", "duplicate"):
                    if halting is None:
                        halting = outcome
                else:
                    logger.warning(
                        "tool_outcome_unhandled status=%s tool_name=%s trace_id=%s",
                        outcome.status,
                        outcome.tool_name,
                        trace_id,
                    )

            if turn_calls:
                messages.append(
                    LLMMessage("assistant", content=result.text, tool_calls=tuple(turn_calls))
                )
                messages.extend(turn_returns)

            if halting is not None:
                if halting.status == "blocked":
                    # blockers 累加而不是覆盖——同一轮里先失败的只读工具同样要留痕。
                    blockers.append(halting.reason)
                    for event in _finish(
                        RunResult(
                            text=halting.user_text,
                            executed_tools=tuple(executed),
                            blockers=tuple(blockers),
                        )
                    ):
                        yield event
                    return
                for event in _finish(
                    RunResult(
                        text=_fallback_text(blockers, executed),
                        executed_tools=tuple(executed),
                        blockers=tuple(blockers),
                    )
                ):
                    yield event
                return

            # 防套娃与防步数耗尽：
            # 1. 已经执行过分析类只读工具，后续轮次不再重复提供该工具，促使大模型直接生成总结
            executed_names = {c.name for c in tool_calls}
            current_allowed = tuple(t for t in current_allowed if t not in executed_names)
            # 2. 如果已到达最后一步（倒数第一轮），强制清空工具，强迫大模型基于已有数据做最终文字总结
            if step >= self._max_steps - 2:
                current_allowed = ()

            # 工具都回来了，下一圈 LLM 要开始写答案——这是整轮里最长的一段
            # 静默（工具卡已经收成完成态，正文还没开始）。放在 halting 的
            # return 之后：被阻塞的轮次不会再写答案，报「正在整理结果」是假的。
            yield TurnStage(stage=STAGE_COMPOSING)

        for event in _finish(
            RunResult(
                text=_fallback_text(blockers, executed),
                executed_tools=tuple(executed),
                blockers=tuple(blockers),
            )
        ):
            yield event
