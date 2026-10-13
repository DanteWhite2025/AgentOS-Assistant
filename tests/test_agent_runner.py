"""AgentRunner 单轮闭环测试（LLM → 工具 → 审计 → 文本）。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from app.agents.llm import FakeLLM
from app.agents.runner import (
    AgentProfile,
    AgentRunner,
    _looks_time_sensitive,
    _requires_live_data,
    _summarize,
)
from app.audit.store import InMemoryAuditStore
from app.runtime.session import Scope, Session
from app.tools.builtin import build_default_registry
from app.tools.core_client import FakeCoreClient
from app.tools.executor import ToolExecutor, ToolNotAllowedError


def _now() -> datetime:
    return datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)


def _session(tools: tuple[str, ...]) -> Session:
    return Session(
        session_id="sess_1",
        user_id="u1",
        modality="text",
        scope=Scope(agents=("avatar",), tools=tools),
        state="text_active",
        created_at=_now(),
        last_activity_at=_now(),
    )


def _runner(profile_tools: tuple[str, ...], audit: InMemoryAuditStore) -> AgentRunner:
    executor = ToolExecutor(build_default_registry(), FakeCoreClient())
    profile = AgentProfile("avatar", "fake", "你是数字人", profile_tools)
    return AgentRunner(profile, executor, FakeLLM(), audit)


async def test_run_turn_readonly_tool_and_audit() -> None:
    audit = InMemoryAuditStore()
    runner = _runner(("get_positions",), audit)
    result = await runner.run_turn(_session(("get_positions",)), "帮我看下持仓", "trace1", _now())

    assert result.executed_tools and result.executed_tools[0].name == "get_positions"
    assert result.text  # 最终有文本回复

    recs = await audit.list_by_session("sess_1")
    assert len(recs) == 1
    assert recs[0].tool_name == "get_positions"
    assert len(recs[0].args_hash) == 64  # SHA-256 hex


async def test_run_turn_no_tool_when_out_of_scope() -> None:
    # profile 有 get_positions 但 session scope 为空 → LLM 看不到工具，不执行
    audit = InMemoryAuditStore()
    runner = _runner(("get_positions",), audit)
    result = await runner.run_turn(_session(()), "看下持仓", "trace1", _now())
    assert not result.executed_tools
    assert not await audit.list_by_session("sess_1")


async def test_web_search_is_disabled_by_default_and_forced_when_requested() -> None:
    from app.agents.llm import LLMResult

    class _SearchExecutor:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            self.calls.append((tool_name, args))
            return {
                "source": "TAVILY_SEARCH",
                "query": args["query"],
                "answer": "黄金与比特币受到美元和风险偏好变化影响。",
                "results": [],
            }

    class _CapturingLLM:
        def __init__(self) -> None:
            self.tools_seen: list[tuple[str, ...]] = []
            self.tool_context_seen: list[bool] = []

        async def complete(self, system, messages, tools):
            self.tools_seen.append(tools)
            self.tool_context_seen.append(any(m.role == "tool" for m in messages))
            return LLMResult(text="基于联网结果：美元走强后风险资产回落。")

    audit = InMemoryAuditStore()
    executor = _SearchExecutor()
    llm = _CapturingLLM()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("web_search",))
    runner = AgentRunner(profile, executor, llm, audit)
    session = _session(("web_search",))

    offline = await runner.run_turn(session, "解释黄金是什么", "trace_off", _now())
    online = await runner.run_turn(
        session,
        "查一下最近黄金和比特币为什么冲高回落",
        "trace_on",
        _now(),
        web_search=True,
    )

    assert offline.text
    assert executor.calls == [
        (
            "web_search",
            {
                "query": "查一下最近黄金和比特币为什么冲高回落",
                "topic": "news",
                "time_range": "week",
            },
        )
    ]
    assert llm.tools_seen == [(), ()]
    assert llm.tool_context_seen == [False, True]
    assert online.executed_tools[0].name == "web_search"
    assert online.text.startswith("基于联网结果")


def test_looks_time_sensitive_matches_recency_and_price_reason_phrasing() -> None:
    cases = (
        "黄金最近为什么涨了",
        "比特币为什么跌了",
        "今天有什么宏观数据",
        "最新的美联储讲话",
        "latest Fed comments",
        "any recent news on this",
    )
    for text in cases:
        assert _looks_time_sensitive(text), f"{text!r} 应判定为时效性问题"


def test_looks_time_sensitive_does_not_match_generic_questions() -> None:
    cases = (
        "帮我看下持仓",
        "解释一下什么是杠杆",
        "美联储加息的历史规律是什么",
    )
    for text in cases:
        assert not _looks_time_sensitive(text), f"{text!r} 不应判定为时效性问题"


def test_requires_live_data_is_narrower_than_looks_time_sensitive() -> None:
    # ask_investment_expert 的本职工作是宏观复盘，这类问题天然会提到
    # "今天""最近"，不该被当成"必须联网"的信号——否则会把它自己的主战场
    # 排除掉（它才刚加了 Redis 缓存专门服务这类高频复盘问题）。
    market_review_cases = (
        "今天大盘走势复盘一下",
        "最近黄金的宏观驱动因素是什么",
        "本周美股表现如何",
    )
    for text in market_review_cases:
        assert _looks_time_sensitive(text), f"{text!r} 应判定为泛化时效性问题"
        assert not _requires_live_data(text), f"{text!r} 不该判定为必须联网"

    # 但明确问"具体某次涨跌的原因"或要突发/最新资讯时，两者都该判定为真
    live_data_cases = (
        "黄金和比特币为什么涨了又跌了",
        "比特币为什么跌了",
        "有什么突发消息吗",
        "最新消息是什么",
    )
    for text in live_data_cases:
        assert _requires_live_data(text), f"{text!r} 应判定为必须联网"


async def test_market_review_query_keeps_investment_expert_even_with_recency_words() -> None:
    """核心回归：修复前的宽泛词表会把 ask_investment_expert 自己的主战场
    （提到"今天""最近"的市场复盘问题）也一并排除，这里验证不会再发生。"""
    from app.agents.llm import LLMResult

    class _CapturingLLM:
        def __init__(self) -> None:
            self.tools_seen: list[tuple[str, ...]] = []

        async def complete(self, system, messages, tools):
            self.tools_seen.append(tools)
            return LLMResult(text="今天大盘整体呈现震荡走势。")

    audit = InMemoryAuditStore()
    llm = _CapturingLLM()
    profile = AgentProfile(
        "avatar", "fake", "你是数字人", ("ask_investment_expert", "web_search")
    )
    executor = ToolExecutor(build_default_registry(), FakeCoreClient())
    runner = AgentRunner(profile, executor, llm, audit)
    session = _session(("ask_investment_expert", "web_search"))

    await runner.run_turn(
        session, "今天大盘走势复盘一下", "trace1", _now(), web_search=False
    )

    assert "ask_investment_expert" in llm.tools_seen[0]


async def test_live_data_query_keeps_investment_expert_when_web_search_not_in_scope() -> None:
    """会话根本没有联网搜索能力时，排除 ask_investment_expert 只会把用户
    指向一个不存在的开关——这种情况下不该排除，让它尽力回答。"""
    from app.agents.llm import LLMResult

    class _CapturingLLM:
        def __init__(self) -> None:
            self.tools_seen: list[tuple[str, ...]] = []

        async def complete(self, system, messages, tools):
            self.tools_seen.append(tools)
            return LLMResult(text="尽力给出分析。")

    audit = InMemoryAuditStore()
    llm = _CapturingLLM()
    # profile 里两个工具都有，但 session scope 只给 ask_investment_expert
    profile = AgentProfile(
        "avatar", "fake", "你是数字人", ("ask_investment_expert", "web_search")
    )
    executor = ToolExecutor(build_default_registry(), FakeCoreClient())
    runner = AgentRunner(profile, executor, llm, audit)
    session = _session(("ask_investment_expert",))

    await runner.run_turn(
        session, "黄金和比特币为什么涨了又跌了", "trace1", _now(), web_search=False
    )

    assert "ask_investment_expert" in llm.tools_seen[0]


async def test_time_sensitive_query_excludes_investment_expert_when_web_search_off() -> None:
    """核心回归：投研分析(不联网) vs 联网搜索的 Prompt 触发词有重叠——

    "黄金比特币为什么涨跌"这类问题在没开联网搜索时，不该被 ask_investment_
    expert 静默接管拿旧缓存/训练记忆冒充最新分析，而是应该被引导去提示
    用户开启联网搜索。这里验证的是代码层的确定性排除，不是指望模型自己
    读 Prompt 领会。
    """
    from app.agents.llm import LLMResult

    class _CapturingLLM:
        def __init__(self) -> None:
            self.tools_seen: list[tuple[str, ...]] = []

        async def complete(self, system, messages, tools):
            self.tools_seen.append(tools)
            return LLMResult(text="请开启联网搜索后再试。")

    audit = InMemoryAuditStore()
    llm = _CapturingLLM()
    profile = AgentProfile(
        "avatar", "fake", "你是数字人", ("ask_investment_expert", "web_search")
    )
    executor = ToolExecutor(build_default_registry(), FakeCoreClient())
    runner = AgentRunner(profile, executor, llm, audit)
    session = _session(("ask_investment_expert", "web_search"))

    await runner.run_turn(
        session, "黄金和比特币最近为什么涨跌", "trace1", _now(), web_search=False
    )

    assert "ask_investment_expert" not in llm.tools_seen[0]


async def test_non_time_sensitive_query_keeps_investment_expert_available() -> None:
    """不是所有问题都该被一刀切——没有时效性触发词的深度研判问题，
    ask_investment_expert 依然应该在候选工具里。"""
    from app.agents.llm import LLMResult

    class _CapturingLLM:
        def __init__(self) -> None:
            self.tools_seen: list[tuple[str, ...]] = []

        async def complete(self, system, messages, tools):
            self.tools_seen.append(tools)
            return LLMResult(text="好的，我来分析一下。")

    audit = InMemoryAuditStore()
    llm = _CapturingLLM()
    profile = AgentProfile(
        "avatar", "fake", "你是数字人", ("ask_investment_expert", "web_search")
    )
    executor = ToolExecutor(build_default_registry(), FakeCoreClient())
    runner = AgentRunner(profile, executor, llm, audit)
    session = _session(("ask_investment_expert", "web_search"))

    await runner.run_turn(
        session, "请分析一下加息周期对成长股估值的历史影响规律", "trace1", _now(),
        web_search=False,
    )

    assert "ask_investment_expert" in llm.tools_seen[0]


async def test_time_sensitive_query_does_not_exclude_investment_expert_when_web_search_on() -> None:
    """web_search=True 时强制搜索已经先拿到了实时数据，不需要再排除
    ask_investment_expert——留给模型做补充分析是安全的。"""
    from app.agents.llm import LLMResult

    class _SearchExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return {"source": "TAVILY_SEARCH", "query": args["query"], "answer": "ok", "results": []}

    class _CapturingLLM:
        def __init__(self) -> None:
            self.tools_seen: list[tuple[str, ...]] = []

        async def complete(self, system, messages, tools):
            self.tools_seen.append(tools)
            return LLMResult(text="基于联网结果分析如下。")

    audit = InMemoryAuditStore()
    executor = _SearchExecutor()
    llm = _CapturingLLM()
    profile = AgentProfile(
        "avatar", "fake", "你是数字人", ("ask_investment_expert", "web_search")
    )
    runner = AgentRunner(profile, executor, llm, audit)
    session = _session(("ask_investment_expert", "web_search"))

    await runner.run_turn(
        session, "黄金和比特币最近为什么涨跌", "trace1", _now(), web_search=True
    )

    assert "ask_investment_expert" in llm.tools_seen[0]


async def test_run_turn_notifies_on_tool_start_before_executing() -> None:
    """on_tool_start(call_id, name, arguments) 必须在真正执行工具之前触发（用于 WS 侧实时推送

    "正在调用哪个工具"，而不是等整轮跑完才批量补发）。
    """
    audit = InMemoryAuditStore()
    runner = _runner(("get_positions",), audit)
    notified: list[tuple[str, str]] = []

    async def on_tool_start(call_id: str, name: str, _arguments) -> None:
        notified.append((call_id, name))

    result = await runner.run_turn(
        _session(("get_positions",)),
        "帮我看下持仓",
        "trace1",
        _now(),
        on_tool_start=on_tool_start,
    )

    assert len(notified) == 1
    assert notified[0][1] == "get_positions"
    # call_id 必须和最终 executed_tools 里的对得上，WS 侧要靠它关联 tool.result
    assert notified[0][0] == result.executed_tools[0].call_id


async def test_run_turn_notifies_in_order_for_multiple_distinct_tool_calls() -> None:
    """一轮里依次调用多个不同工具时，on_tool_start 触发的顺序必须和实际执行顺序一致

    ——WS 侧的 tool.invoked/tool.result 关联全靠这个顺序保证。
    """
    from app.agents.llm import LLMResult, LLMToolCall

    class _TwoToolCallsLLM:
        async def complete(self, system, messages, tools):
            tool_msgs = [m for m in messages if m.role == "tool"]
            if len(tool_msgs) >= 2:
                return LLMResult(text="done")
            return LLMResult(
                text="",
                tool_calls=(LLMToolCall("c1", "get_positions", {}), LLMToolCall("c2", "get_risk_summary", {})),
            )

    audit = InMemoryAuditStore()
    executor = ToolExecutor(build_default_registry(), FakeCoreClient())
    profile = AgentProfile("avatar", "fake", "你是数字人", ("get_positions", "get_risk_summary"))
    runner = AgentRunner(profile, executor, _TwoToolCallsLLM(), audit)
    notified: list[tuple[str, str]] = []

    async def on_tool_start(call_id: str, name: str, _arguments) -> None:
        notified.append((call_id, name))

    result = await runner.run_turn(
        _session(("get_positions", "get_risk_summary")),
        "帮我看下持仓和风险",
        "trace1",
        _now(),
        on_tool_start=on_tool_start,
    )

    assert notified == [("c1", "get_positions"), ("c2", "get_risk_summary")]
    assert [t.name for t in result.executed_tools] == ["get_positions", "get_risk_summary"]


async def test_run_turn_does_not_notify_for_duplicate_tool_call() -> None:
    """同一指纹的重复调用会被短路（不实际执行），不该误发一条 on_tool_start。"""
    from app.agents.llm import LLMResult, LLMToolCall

    class _DuplicateCallLLM:
        async def complete(self, system, messages, tools):
            if any(m.role == "tool" for m in messages):
                return LLMResult(text="done")
            return LLMResult(
                text="",
                tool_calls=(
                    LLMToolCall("c1", "get_positions", {}),
                    LLMToolCall("c2", "get_positions", {}),
                ),
            )

    audit = InMemoryAuditStore()
    executor = ToolExecutor(build_default_registry(), FakeCoreClient())
    profile = AgentProfile("avatar", "fake", "你是数字人", ("get_positions",))
    runner = AgentRunner(profile, executor, _DuplicateCallLLM(), audit)
    notified: list[tuple[str, str]] = []

    async def on_tool_start(call_id: str, name: str, _arguments) -> None:
        notified.append((call_id, name))

    await runner.run_turn(
        _session(("get_positions",)), "帮我看下持仓", "trace1", _now(), on_tool_start=on_tool_start
    )

    assert notified == [("c1", "get_positions")]


async def test_executor_blocks_out_of_scope() -> None:
    executor = ToolExecutor(build_default_registry(), FakeCoreClient())
    with pytest.raises(ToolNotAllowedError):
        await executor.execute("get_positions", "u1", {}, [])  # scope 空 → 拒绝


async def test_executor_blocks_forged_tool() -> None:
    executor = ToolExecutor(build_default_registry(), FakeCoreClient())
    with pytest.raises(ToolNotAllowedError):
        # 客户端伪造工具名，即使自称在 scope 里也拒绝（未注册）
        await executor.execute("evil_tool", "u1", {}, ["evil_tool"])


async def test_tool_execution_exception_never_leaks_internal_details_to_user() -> None:
    """工具执行遇到未预期异常（例如 Core 403 状态码堆栈、内部接口 URL）时，
    对外文本、blockers 和审计记录都必须只留安全的通用错误，原始异常只进服务器
    日志——这三个出口都会被 Mobile 端直接看到：聊天文本、
    SingleTurnResponse.blockers（app/api/agent.py）、/history 的 result_ref
    （app/api/agent.py::get_history 直接回显 AuditRecord.result_ref）。"""
    from app.agents.llm import LLMResult, LLMToolCall

    leaking_message = (
        "403 Forbidden: GET https://core-internal.agentos-app.app/internal/v1/"
        "account-summary Traceback (most recent call last): ... "
        "AGENT_TOKEN insufficient scope"
    )

    class _ExplodingExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            raise RuntimeError(leaking_message)

    class _ScriptedLLM:
        def __init__(self) -> None:
            self.messages_seen: list[tuple] = []

        async def complete(self, system, messages, tools):
            self.messages_seen.append(messages)
            if any(m.role == "tool" for m in messages):
                return LLMResult(text="暂时取不到余额，你可以稍后再问我。")
            return LLMResult(text="", tool_calls=(LLMToolCall("c1", "get_balances", {}),))

    audit = InMemoryAuditStore()
    llm = _ScriptedLLM()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("get_balances",))
    runner = AgentRunner(profile, _ExplodingExecutor(), llm, audit)

    result = await runner.run_turn(
        _session(("get_balances",)), "我的余额是多少", "trace_leak", _now()
    )

    leak_markers = ("core-internal", "403", "AGENT_TOKEN")
    for marker in leak_markers:
        assert marker not in result.text
    for blocker in result.blockers:
        for marker in leak_markers:
            assert marker not in blocker
    # 第四个出口：降级后原始异常绝不能顺着"回灌给模型"这条新路径漏出去——
    # 模型看得到的东西随时可能被它复述给用户。
    for message in _tool_messages(llm.messages_seen[-1]):
        for marker in leak_markers:
            assert marker not in message

    records = await audit.list_by_session("sess_1")
    assert len(records) == 1
    assert "core-internal" not in records[0].result_ref
    assert "403" not in records[0].result_ref
    assert "AGENT_TOKEN" not in records[0].result_ref


# ---- T0: _summarize 曾经把所有工具结果硬切 500 字符（从 JSON 中间截断，结尾是破损转义
# 串），模型看不到完整证据 —— "联网了答案还是很水" 的一半根因在这里。 ----


def test_summarize_prefers_evidence_text_when_present() -> None:
    out = {"source": "MARKET_EVIDENCE", "evidence_text": "BTC 现价 92,400，24h -3.2%", "market": {}}
    assert _summarize(out) == "BTC 现价 92,400，24h -3.2%"


def test_summarize_does_not_truncate_when_within_limit() -> None:
    out = {"data": "short"}
    text = _summarize(out)
    assert "…（结果已截断）" not in text
    assert text == json.dumps(out, ensure_ascii=False)


def test_oversized_tool_result_is_truncated_with_explicit_marker() -> None:
    out = {"data": "x" * 3000}
    text = _summarize(out)
    assert text.endswith("…（结果已截断）")
    assert len(text) == 2000 + len("…（结果已截断）")


def test_summarize_does_not_truncate_exactly_at_the_limit() -> None:
    # 精确等于上限时不截断——防 <=/< 边界写反的回归。
    payload_text = json.dumps({"data": "x"}, ensure_ascii=False)
    padded = {"data": "x" * (2000 - len(payload_text) + 1)}
    text = _summarize(padded)
    assert len(text) == 2000
    assert "…（结果已截断）" not in text


def test_summarize_falls_back_to_json_when_evidence_text_is_empty_string() -> None:
    out = {"evidence_text": "", "data": "fallback"}
    text = _summarize(out)
    assert text == json.dumps(out, ensure_ascii=False)


def test_summarize_falls_back_to_json_when_evidence_text_is_not_a_string() -> None:
    out = {"evidence_text": {"nested": True}, "data": "fallback"}
    text = _summarize(out)
    assert text == json.dumps(out, ensure_ascii=False)


def test_summarize_uses_default_limit_for_unknown_tool_name() -> None:
    out = {"data": "x" * 3000}
    text = _summarize(out, "some_unregistered_tool")
    assert len(text) == 2000 + len("…（结果已截断）")


def test_account_tool_result_uses_tighter_limit() -> None:
    out = {"data": "x" * 3000}
    text = _summarize(out, "get_balances")
    assert len(text) == 1500 + len("…（结果已截断）")


def test_account_tool_alias_names_also_use_tighter_limit() -> None:
    out = {"data": "x" * 3000}
    for alias in ("agentos_get_balances", "agentos_get_positions"):
        text = _summarize(out, alias)
        assert len(text) == 1500 + len("…（结果已截断）"), alias


def test_summarize_uses_wider_limit_for_evidence_tools() -> None:
    out = {"data": "x" * 8000}
    expert_text = _summarize(out, "ask_investment_expert")
    search_text = _summarize(out, "web_search")
    assert len(expert_text) == 6000 + len("…（结果已截断）")
    assert len(search_text) == 4000 + len("…（结果已截断）")


async def test_tool_result_is_not_truncated_to_500_chars() -> None:
    """回归 T0：PR #60 把 Tavily/证据结果做到 2000+ 字符，但 runner 曾经只把前 500
    个字符喂给模型 —— 联网确实搜到了，但搜到的东西大半没送达模型。"""

    from app.agents.llm import LLMResult, LLMToolCall

    big_payload = {"source": "MARKET_EVIDENCE", "evidence_text": "证据条目：" + "X" * 3000}

    class _EvidenceExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return big_payload

    class _CapturingLLM:
        def __init__(self) -> None:
            self.messages_seen: list[tuple] = []

        async def complete(self, system, messages, tools):
            self.messages_seen.append(messages)
            if any(m.role == "tool" for m in messages):
                return LLMResult(text="已生成分析")
            return LLMResult(
                text="", tool_calls=(LLMToolCall("c1", "ask_investment_expert", {}),)
            )

    audit = InMemoryAuditStore()
    llm = _CapturingLLM()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("ask_investment_expert",))
    runner = AgentRunner(profile, _EvidenceExecutor(), llm, audit)

    result = await runner.run_turn(
        _session(("ask_investment_expert",)), "分析一下黄金和比特币的宏观驱动因素", "trace_t0", _now()
    )

    tool_message = next(m for m in llm.messages_seen[-1] if m.role == "tool")
    assert len(tool_message.content) > 2000
    assert "证据条目" in tool_message.content
    assert result.text == "已生成分析"


# ---- T6: runner 侧深度分析激活句。不靠模型自己判断"这是不是深度分析场景"——
# 本轮一旦真的执行成功了证据类工具（ask_investment_expert/web_search），就
# 无条件追加一句激活指令，场景判断交给运行时的确定性检查。 ----


async def test_evidence_tool_result_activates_deep_analysis_instruction() -> None:
    from app.agents.llm import LLMResult, LLMToolCall

    class _EchoExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return {"source": "MARKET_EVIDENCE", "evidence_text": "BTC 现价 92,400"}

    class _CapturingLLM:
        def __init__(self) -> None:
            self.systems_seen: list[str] = []

        async def complete(self, system, messages, tools):
            self.systems_seen.append(system)
            if any(m.role == "tool" for m in messages):
                return LLMResult(text="已生成分析")
            return LLMResult(
                text="", tool_calls=(LLMToolCall("c1", "ask_investment_expert", {}),)
            )

    audit = InMemoryAuditStore()
    llm = _CapturingLLM()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("ask_investment_expert",))
    runner = AgentRunner(profile, _EchoExecutor(), llm, audit)

    await runner.run_turn(
        _session(("ask_investment_expert",)), "分析一下比特币", "trace_t6a", _now()
    )

    assert len(llm.systems_seen) == 2
    assert "本轮已获得证据数据" not in llm.systems_seen[0]
    assert "本轮已获得证据数据" in llm.systems_seen[1]


async def test_evidence_tool_error_result_does_not_activate_deep_analysis() -> None:
    """回归：executor 对校验失败（如 symbols 超过 8 个）返回的是 {"error": ...}
    而不是抛异常，status 仍是 "ok"。这种情况下没有真实证据，绝不能激活四步法——
    否则等于逼模型在没有数据支撑时硬编 500~800 字，正是这次要消灭的套话问题。"""
    from app.agents.llm import LLMResult, LLMToolCall

    class _FailingExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return {"error": "symbols 必须是最多 8 个字符串"}

    class _CapturingLLM:
        def __init__(self) -> None:
            self.systems_seen: list[str] = []

        async def complete(self, system, messages, tools):
            self.systems_seen.append(system)
            if any(m.role == "tool" for m in messages):
                return LLMResult(text="抱歉，暂时无法完成这项分析。")
            return LLMResult(
                text="", tool_calls=(LLMToolCall("c1", "ask_investment_expert", {}),)
            )

    audit = InMemoryAuditStore()
    llm = _CapturingLLM()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("ask_investment_expert",))
    runner = AgentRunner(profile, _FailingExecutor(), llm, audit)

    await runner.run_turn(
        _session(("ask_investment_expert",)), "多资产深度分析", "trace_t6e", _now()
    )

    assert len(llm.systems_seen) == 2
    assert all("本轮已获得证据数据" not in system for system in llm.systems_seen)


async def test_forced_web_search_also_activates_deep_analysis_instruction() -> None:
    from app.agents.llm import LLMResult

    class _SearchExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return {
                "source": "TAVILY_SEARCH",
                "query": args.get("query", ""),
                "answer": "黄金创新高，比特币回调。",
                "results": [
                    {
                        "title": "黄金再创历史新高",
                        "url": "https://example.com/gold",
                        "content": "现货黄金上涨 2.1%，突破 4,300 美元。",
                    }
                ],
            }

    class _CapturingLLM:
        def __init__(self) -> None:
            self.systems_seen: list[str] = []

        async def complete(self, system, messages, tools):
            self.systems_seen.append(system)
            return LLMResult(text="基于联网结果的分析")

    audit = InMemoryAuditStore()
    llm = _CapturingLLM()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("web_search",))
    runner = AgentRunner(profile, _SearchExecutor(), llm, audit)

    await runner.run_turn(
        _session(("web_search",)),
        "黄金和比特币为什么大涨大跌",
        "trace_t6b",
        _now(),
        web_search=True,
    )

    assert llm.systems_seen
    assert all("本轮已获得证据数据" in system for system in llm.systems_seen)


async def test_plain_balance_query_does_not_activate_deep_analysis() -> None:
    from app.agents.llm import LLMResult, LLMToolCall

    class _BalanceExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return {"balances": [{"asset": "USDT", "amount": "100.00"}]}

    class _CapturingLLM:
        def __init__(self) -> None:
            self.systems_seen: list[str] = []

        async def complete(self, system, messages, tools):
            self.systems_seen.append(system)
            if any(m.role == "tool" for m in messages):
                return LLMResult(text="你的余额是 100 USDT")
            return LLMResult(text="", tool_calls=(LLMToolCall("c1", "get_positions", {}),))

    audit = InMemoryAuditStore()
    llm = _CapturingLLM()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("get_positions",))
    runner = AgentRunner(profile, _BalanceExecutor(), llm, audit)

    await runner.run_turn(_session(("get_positions",)), "我的持仓", "trace_t6c", _now())

    assert llm.systems_seen
    assert all("本轮已获得证据数据" not in system for system in llm.systems_seen)


async def test_deep_analysis_instruction_is_appended_only_once() -> None:
    from app.agents.llm import LLMResult, LLMToolCall

    class _EchoExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return {"source": "MARKET_EVIDENCE", "evidence_text": "证据"}

    class _TwoRoundLLM:
        def __init__(self) -> None:
            self.systems_seen: list[str] = []
            self._step = 0

        async def complete(self, system, messages, tools):
            self.systems_seen.append(system)
            self._step += 1
            if self._step >= 3:
                return LLMResult(text="最终总结")
            return LLMResult(
                text="", tool_calls=(LLMToolCall(f"c{self._step}", "ask_investment_expert", {}),)
            )

    audit = InMemoryAuditStore()
    llm = _TwoRoundLLM()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("ask_investment_expert",))
    runner = AgentRunner(profile, _EchoExecutor(), llm, audit, max_steps=5)

    await runner.run_turn(
        _session(("ask_investment_expert",)), "多资产深度分析", "trace_t6d", _now()
    )

    assert llm.systems_seen[-1].count("本轮已获得证据数据") == 1


# ---- T7: 空证据防幻觉。证据工具「调用成功但什么都没查到」时不带 error 字段
# （Tavily 空结果是 {"results": []}，证据包是 coverage 全 False），旧逻辑只看
# out["error"] 会把它当成有效证据，照样激活「必须写 500~800 字、每条归因挂
# 具体数字」的四步法——没有数据却要求带数字归因，等于逼模型编造。 ----


def _capturing_llm_factory():
    """两轮 LLM：第一轮请求证据工具，拿到工具结果后出最终文本。"""
    from app.agents.llm import LLMResult, LLMToolCall

    class _CapturingLLM:
        def __init__(self, tool_name: str = "ask_investment_expert") -> None:
            self.systems_seen: list[str] = []
            self._tool_name = tool_name

        async def complete(self, system, messages, tools):
            self.systems_seen.append(system)
            if any(m.role == "tool" for m in messages):
                return LLMResult(text="这次没有查到相关信息。")
            return LLMResult(text="", tool_calls=(LLMToolCall("c1", self._tool_name, {}),))

    return _CapturingLLM


async def test_empty_search_results_do_not_activate_deep_analysis() -> None:
    """web_search 查不到东西时返回 {"answer": "", "results": []}，没有 error 字段。"""

    class _EmptySearchExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return {
                "source": "TAVILY_SEARCH",
                "query": "冷门山寨币 XYZ",
                "answer": "",
                "results": [],
            }

    llm = _capturing_llm_factory()("web_search")
    profile = AgentProfile("avatar", "fake", "你是数字人", ("web_search",))
    runner = AgentRunner(profile, _EmptySearchExecutor(), llm, InMemoryAuditStore())

    await runner.run_turn(_session(("web_search",)), "XYZ 币最近有什么消息", "trace_t7a", _now())

    assert llm.systems_seen
    assert all("本轮已获得证据数据" not in system for system in llm.systems_seen)
    assert "本轮未获得有效证据" in llm.systems_seen[-1]


async def test_empty_evidence_pack_does_not_activate_deep_analysis() -> None:
    """ask_investment_expert 行情和新闻都没抓到时 coverage 全 False，且不带 error。"""

    class _EmptyEvidenceExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return {
                "source": "MARKET_EVIDENCE",
                "market": {"data": [], "unavailable_symbols": ["XYZ"], "error": None},
                "news": {"source": "LOCAL_NEWS_STORE", "items": [], "error": None},
                "coverage": {"has_market": False, "has_news": False},
                "evidence_text": "未检索到任何行情或新闻证据，只能明确告知用户当前无法获取实时数据。",
            }

    llm = _capturing_llm_factory()()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("ask_investment_expert",))
    runner = AgentRunner(profile, _EmptyEvidenceExecutor(), llm, InMemoryAuditStore())

    await runner.run_turn(
        _session(("ask_investment_expert",)), "XYZ 为什么跌", "trace_t7b", _now()
    )

    assert llm.systems_seen
    assert all("本轮已获得证据数据" not in system for system in llm.systems_seen)
    assert "本轮未获得有效证据" in llm.systems_seen[-1]


async def test_partial_evidence_pack_still_activates_deep_analysis() -> None:
    """只抓到新闻、行情不可用时仍是有效证据——不能因为半边缺失就退化成不分析。"""

    class _NewsOnlyExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return {
                "source": "MARKET_EVIDENCE",
                "market": {"data": [], "unavailable_symbols": ["BTC"], "error": "行情抓取超时"},
                "news": {
                    "source": "LOCAL_NEWS_STORE",
                    "items": [{"title": "美联储降息 25bp", "content": "..."}],
                    "error": None,
                },
                "coverage": {"has_market": False, "has_news": True},
                "evidence_text": "新闻要点：美联储降息 25bp",
            }

    llm = _capturing_llm_factory()()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("ask_investment_expert",))
    runner = AgentRunner(profile, _NewsOnlyExecutor(), llm, InMemoryAuditStore())

    await runner.run_turn(
        _session(("ask_investment_expert",)), "比特币为什么跌", "trace_t7c", _now()
    )

    assert "本轮已获得证据数据" in llm.systems_seen[-1]
    assert "本轮未获得有效证据" not in llm.systems_seen[-1]


async def test_search_answer_without_results_counts_as_evidence() -> None:
    """Tavily 命中直答但没有条目列表时，answer 本身就是可引用内容。"""

    class _AnswerOnlyExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return {
                "source": "TAVILY_SEARCH",
                "query": "以太坊坎昆升级时间",
                "answer": "坎昆升级已于 2024 年 3 月 13 日在主网激活。",
                "results": [],
            }

    llm = _capturing_llm_factory()("web_search")
    profile = AgentProfile("avatar", "fake", "你是数字人", ("web_search",))
    runner = AgentRunner(profile, _AnswerOnlyExecutor(), llm, InMemoryAuditStore())

    await runner.run_turn(_session(("web_search",)), "坎昆升级什么时候", "trace_t7d", _now())

    assert "本轮已获得证据数据" in llm.systems_seen[-1]


async def test_forced_web_search_with_empty_results_does_not_activate_deep_analysis() -> None:
    """强制联网路径同样要判空——它绕过 LLM 直接执行 web_search 并回灌结果。"""
    from app.agents.llm import LLMResult

    class _EmptySearchExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return {
                "source": "TAVILY_SEARCH",
                "query": args.get("query", ""),
                "answer": "",
                "results": [],
            }

    class _CapturingLLM:
        def __init__(self) -> None:
            self.systems_seen: list[str] = []

        async def complete(self, system, messages, tools):
            self.systems_seen.append(system)
            return LLMResult(text="这次没有查到相关消息。")

    llm = _CapturingLLM()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("web_search",))
    runner = AgentRunner(profile, _EmptySearchExecutor(), llm, InMemoryAuditStore())

    await runner.run_turn(
        _session(("web_search",)),
        "XYZ 币为什么突然暴涨",
        "trace_t7e",
        _now(),
        web_search=True,
    )

    assert llm.systems_seen
    assert all("本轮已获得证据数据" not in system for system in llm.systems_seen)
    assert "本轮未获得有效证据" in llm.systems_seen[-1]


async def test_no_evidence_notice_is_withdrawn_once_evidence_arrives() -> None:
    """先空后有：第二个证据工具补上数据后，必须撤掉"没有证据"的提示，
    否则 system prompt 里会同时挂着"没查到"和"必须写四步法"两句矛盾指令。"""
    from app.agents.llm import LLMResult, LLMToolCall

    class _EmptyThenFullExecutor:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {"source": "TAVILY_SEARCH", "query": "q", "answer": "", "results": []}
            return {
                "source": "MARKET_EVIDENCE",
                "coverage": {"has_market": True, "has_news": False},
                "evidence_text": "BTC 现价 92,400，24h -3.2%",
            }

    class _TwoToolLLM:
        def __init__(self) -> None:
            self.systems_seen: list[str] = []
            self._step = 0

        async def complete(self, system, messages, tools):
            self.systems_seen.append(system)
            self._step += 1
            if self._step == 1:
                return LLMResult(text="", tool_calls=(LLMToolCall("c1", "web_search", {}),))
            if self._step == 2:
                return LLMResult(
                    text="", tool_calls=(LLMToolCall("c2", "ask_investment_expert", {}),)
                )
            return LLMResult(text="基于行情的分析")

    llm = _TwoToolLLM()
    profile = AgentProfile(
        "avatar", "fake", "你是数字人", ("web_search", "ask_investment_expert")
    )
    runner = AgentRunner(
        profile, _EmptyThenFullExecutor(), llm, InMemoryAuditStore(), max_steps=5
    )

    await runner.run_turn(
        _session(("web_search", "ask_investment_expert")),
        "比特币为什么跌",
        "trace_t7f",
        _now(),
    )

    final_system = llm.systems_seen[-1]
    assert "本轮已获得证据数据" in final_system
    assert "本轮未获得有效证据" not in final_system


# ---- T8: 工具失败三级分类。「工具调用成功」以外的出口不再一律终止整轮：
# 只读工具失败降级成 no_data 回灌给模型（让它如实说没查到并继续回答），
# 只有写工具失败和额度/杠杆超限才阻塞对话。产品要求见 Dante 批复第 3 点：
# 「不要给用户很多报错，除非真的是阻塞性错误」。 ----


class _ScriptedToolLLM:
    """第一轮请求指定工具，拿到工具结果后出最终文本。"""

    def __init__(self, tool_name: str, final_text: str = "已根据现有信息回答") -> None:
        from app.agents.llm import LLMResult, LLMToolCall

        self._result = LLMResult
        self._call = LLMToolCall
        self._tool_name = tool_name
        self._final_text = final_text
        self.messages_seen: list[tuple] = []
        self.call_count = 0

    async def complete(self, system, messages, tools):
        self.call_count += 1
        self.messages_seen.append(messages)
        if any(m.role == "tool" for m in messages):
            return self._result(text=self._final_text)
        return self._result(text="", tool_calls=(self._call("c1", self._tool_name, {}),))


class _RaisingExecutor:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
        raise self._exc


def _tool_messages(messages) -> list[str]:
    return [m.content for m in messages if m.role == "tool"]


async def test_readonly_tool_timeout_degrades_instead_of_blocking() -> None:
    """只读工具超时不再终止整轮——模型拿到"没查到"继续作答。"""
    llm = _ScriptedToolLLM("get_market_price", "行情暂时查不到，我先讲讲基本面。")
    profile = AgentProfile("avatar", "fake", "你是数字人", ("get_market_price",))
    runner = AgentRunner(
        profile, _RaisingExecutor(httpx.TimeoutException("timeout")), llm, InMemoryAuditStore()
    )

    result = await runner.run_turn(
        _session(("get_market_price",)), "BTC 现在多少钱", "trace_t8a", _now()
    )

    assert result.text == "行情暂时查不到，我先讲讲基本面。"
    assert llm.call_count == 2, "模型必须被再次调用，而不是整轮直接终止"


async def test_readonly_tool_timeout_does_not_use_trade_safety_copy() -> None:
    """回归：只读工具超时曾经套用"未确认的草稿不会自动成交"的交易语境文案，
    用户搜个新闻会收到交易安全提示，明显文不对题。"""
    llm = _ScriptedToolLLM("web_search", "这次没有查到相关信息。")
    profile = AgentProfile("avatar", "fake", "你是数字人", ("web_search",))
    runner = AgentRunner(
        profile, _RaisingExecutor(httpx.TimeoutException("timeout")), llm, InMemoryAuditStore()
    )

    result = await runner.run_turn(_session(("web_search",)), "XYZ 有什么新闻", "trace_t8b", _now())

    assert "草稿" not in result.text
    assert "成交" not in result.text


async def test_mutating_tool_timeout_still_blocks_with_trade_safety_copy() -> None:
    """写工具超时必须保持阻塞：草案可能已经在 Core 侧创建，只能让用户去界面确认。"""
    llm = _ScriptedToolLLM("create_trade_draft", "不应该走到这里")
    profile = AgentProfile("avatar", "fake", "你是数字人", ("create_trade_draft",))
    runner = AgentRunner(
        profile, _RaisingExecutor(httpx.TimeoutException("timeout")), llm, InMemoryAuditStore()
    )

    result = await runner.run_turn(
        _session(("create_trade_draft",)), "永续做多 0.1 BTC，限价 90000", "trace_t8c", _now()
    )

    assert "未确认的草稿不会自动成交" in result.text
    assert llm.call_count == 1, "写工具失败必须早退，不能让模型继续编"


async def test_readonly_tool_unexpected_error_degrades_and_feeds_model() -> None:
    """只读工具的未预期异常降级成 no_data，并把中性说明回灌给模型。"""
    llm = _ScriptedToolLLM("get_balances", "暂时取不到余额，你可以稍后再问我。")
    profile = AgentProfile("avatar", "fake", "你是数字人", ("get_balances",))
    runner = AgentRunner(
        profile, _RaisingExecutor(RuntimeError("boom")), llm, InMemoryAuditStore()
    )

    result = await runner.run_turn(_session(("get_balances",)), "我的余额", "trace_t8d", _now())

    assert result.text == "暂时取不到余额，你可以稍后再问我。"
    tool_messages = _tool_messages(llm.messages_seen[-1])
    assert tool_messages, "降级后必须给模型一条工具消息，否则它不知道查过且失败了"


async def test_mutating_tool_unexpected_error_still_blocks() -> None:
    llm = _ScriptedToolLLM("create_trade_draft", "不应该走到这里")
    profile = AgentProfile("avatar", "fake", "你是数字人", ("create_trade_draft",))
    runner = AgentRunner(
        profile, _RaisingExecutor(RuntimeError("boom")), llm, InMemoryAuditStore()
    )

    result = await runner.run_turn(
        _session(("create_trade_draft",)), "永续做多 0.1 BTC，限价 90000", "trace_t8e", _now()
    )

    assert result.text == "相关数据服务暂不可用，请稍后重试。"
    assert llm.call_count == 1


async def test_mandate_limit_still_blocks_with_dedicated_copy() -> None:
    """额度超限是真正的阻塞性错误，必须原样告诉用户。"""
    from app.policies.engine import PolicyViolationError

    llm = _ScriptedToolLLM("create_trade_draft", "不应该走到这里")
    profile = AgentProfile("avatar", "fake", "你是数字人", ("create_trade_draft",))
    runner = AgentRunner(
        profile,
        _RaisingExecutor(PolicyViolationError("超出单笔额度上限")),
        llm,
        InMemoryAuditStore(),
    )

    result = await runner.run_turn(
        _session(("create_trade_draft",)), "永续做多 10 BTC，限价 90000", "trace_t8f", _now()
    )

    assert "超过 Core 当前允许的单笔交易额度" in result.text
    assert llm.call_count == 1


async def test_readonly_tool_value_error_never_reaches_user_text() -> None:
    """回归：只读工具的 ValueError 文案曾经原样透出成"请求被拒绝：{英文技术串}"。
    降级后技术串只回灌给模型（它需要据此改正调用），绝不进用户可见文本。"""
    technical = "market_tickers requires exactly one array field: symbols"

    llm = _ScriptedToolLLM("market_tickers", "我换个方式查一下。")
    profile = AgentProfile("avatar", "fake", "你是数字人", ("market_tickers",))
    runner = AgentRunner(
        profile, _RaisingExecutor(ValueError(technical)), llm, InMemoryAuditStore()
    )

    result = await runner.run_turn(_session(("market_tickers",)), "查行情", "trace_t8g", _now())

    assert technical not in result.text
    assert "请求被拒绝" not in result.text
    assert result.text == "我换个方式查一下。"


async def test_degraded_failure_is_recorded_in_blockers_for_observability() -> None:
    """降级不等于静默：用户看不到报错，但 blockers 仍要留痕供排查。"""
    llm = _ScriptedToolLLM("get_balances", "暂时取不到余额。")
    profile = AgentProfile("avatar", "fake", "你是数字人", ("get_balances",))
    runner = AgentRunner(
        profile, _RaisingExecutor(RuntimeError("boom")), llm, InMemoryAuditStore()
    )

    result = await runner.run_turn(_session(("get_balances",)), "我的余额", "trace_t8h", _now())

    assert "TOOL_EXECUTION_ERROR" in result.blockers


async def test_internal_error_code_never_becomes_user_facing_text() -> None:
    """回归：步数耗尽时的兜底文案曾经取 blockers[-1]，会把 TOOL_EXECUTION_ERROR
    这类内部错误码直接当成回复发给用户。"""
    from app.agents.llm import LLMResult, LLMToolCall

    class _LoopingLLM:
        def __init__(self) -> None:
            self._step = 0

        async def complete(self, system, messages, tools):
            self._step += 1
            return LLMResult(
                text="", tool_calls=(LLMToolCall(f"c{self._step}", "get_balances", {}),)
            )

    profile = AgentProfile("avatar", "fake", "你是数字人", ("get_balances",))
    runner = AgentRunner(
        profile, _RaisingExecutor(RuntimeError("boom")), _LoopingLLM(), InMemoryAuditStore()
    )

    result = await runner.run_turn(_session(("get_balances",)), "我的余额", "trace_t8i", _now())

    assert "TOOL_EXECUTION_ERROR" not in result.text
    assert result.text


async def test_forced_web_search_timeout_degrades_instead_of_blocking() -> None:
    """强制联网路径的搜索超时同样降级——让模型如实说没搜到，而不是报错终止。"""
    from app.agents.llm import LLMResult

    class _CapturingLLM:
        def __init__(self) -> None:
            self.systems_seen: list[str] = []

        async def complete(self, system, messages, tools):
            self.systems_seen.append(system)
            return LLMResult(text="这次没有搜到相关消息。")

    llm = _CapturingLLM()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("web_search",))
    runner = AgentRunner(
        profile, _RaisingExecutor(httpx.TimeoutException("timeout")), llm, InMemoryAuditStore()
    )

    result = await runner.run_turn(
        _session(("web_search",)),
        "XYZ 为什么突然暴涨",
        "trace_t8j",
        _now(),
        web_search=True,
    )

    assert result.text == "这次没有搜到相关消息。"
    assert llm.systems_seen, "必须仍然调用模型，而不是直接返回搜索超时报错"
    assert "本轮未获得有效证据" in llm.systems_seen[-1]


# ---- T9: code review 补漏。写工具的判定范围、并发批次的结果保全、
# blockers 累加。 ----


async def test_preference_write_failure_blocks_and_says_not_saved() -> None:
    """update_user_preference 是持久化写入，不是查询。失败时按只读工具降级会
    回灌"没有查到"，用户可能以为偏好已经记住了——必须明确说没保存成功。"""
    llm = _ScriptedToolLLM("update_user_preference", "不应该走到这里")
    profile = AgentProfile("avatar", "fake", "你是数字人", ("update_user_preference",))
    runner = AgentRunner(
        profile, _RaisingExecutor(RuntimeError("db down")), llm, InMemoryAuditStore()
    )

    result = await runner.run_turn(
        _session(("update_user_preference",)), "记住我关注 BTC", "trace_t9a", _now()
    )

    assert "没有保存成功" in result.text
    assert "查" not in result.text, "写失败不能说成查询失败"
    assert llm.call_count == 1


async def test_trade_draft_failure_keeps_its_own_copy() -> None:
    """交易草案失败的后果与偏好写入不同——状态不明，要引导用户去界面确认。"""
    llm = _ScriptedToolLLM("create_trade_draft", "不应该走到这里")
    profile = AgentProfile("avatar", "fake", "你是数字人", ("create_trade_draft",))
    runner = AgentRunner(
        profile, _RaisingExecutor(httpx.TimeoutException("t")), llm, InMemoryAuditStore()
    )

    result = await runner.run_turn(
        _session(("create_trade_draft",)), "永续做多 0.1 BTC，限价 90000", "trace_t9b", _now()
    )

    assert "未确认的草稿不会自动成交" in result.text


async def test_concurrent_batch_mixes_ok_and_no_data() -> None:
    """并发只读批次：一个成功一个失败，成功的结果必须照常进入对话。"""
    from app.agents.llm import LLMResult, LLMToolCall

    class _MixedExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            if tool_name == "get_positions":
                raise RuntimeError("boom")
            return {"balances": [{"asset": "USDT", "amount": "100.00"}]}

    class _TwoToolLLM:
        def __init__(self) -> None:
            self.messages_seen: list[tuple] = []

        async def complete(self, system, messages, tools):
            self.messages_seen.append(messages)
            if any(m.role == "tool" for m in messages):
                return LLMResult(text="余额 100 USDT，持仓这次没取到。")
            return LLMResult(
                text="",
                tool_calls=(
                    LLMToolCall("c1", "get_positions", {}),
                    LLMToolCall("c2", "get_balances", {}),
                ),
            )

    llm = _TwoToolLLM()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("get_positions", "get_balances"))
    runner = AgentRunner(profile, _MixedExecutor(), llm, InMemoryAuditStore())

    result = await runner.run_turn(
        _session(("get_positions", "get_balances")), "看下我的账户", "trace_t9c", _now()
    )

    assert result.text == "余额 100 USDT，持仓这次没取到。"
    assert [t.name for t in result.executed_tools] == ["get_balances"]
    assert "TOOL_EXECUTION_ERROR" in result.blockers
    tool_messages = _tool_messages(llm.messages_seen[-1])
    assert len(tool_messages) == 2, "成功和降级的结果都要回灌，不能只留一条"


async def test_blocked_outcome_keeps_earlier_blockers() -> None:
    """同一轮里先有只读工具降级、后有写工具阻塞时，先前的 reason 不能被覆盖掉。"""
    from app.agents.llm import LLMResult, LLMToolCall

    class _MixedExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            raise RuntimeError("boom")

    class _SequencedLLM:
        def __init__(self) -> None:
            self._step = 0

        async def complete(self, system, messages, tools):
            self._step += 1
            if self._step == 1:
                return LLMResult(text="", tool_calls=(LLMToolCall("c1", "get_balances", {}),))
            return LLMResult(
                text="", tool_calls=(LLMToolCall("c2", "create_trade_draft", {}),)
            )

    profile = AgentProfile(
        "avatar", "fake", "你是数字人", ("get_balances", "create_trade_draft")
    )
    runner = AgentRunner(profile, _MixedExecutor(), _SequencedLLM(), InMemoryAuditStore())

    result = await runner.run_turn(
        _session(("get_balances", "create_trade_draft")),
        "永续做多 0.1 BTC，限价 90000",
        "trace_t9d",
        _now(),
    )

    assert result.text == "相关数据服务暂不可用，请稍后重试。"
    assert len(result.blockers) == 2, "只读工具降级的留痕不能被写工具的阻塞覆盖掉"


# ---- T10: 工具生命周期钩子。前端要在工具开始时显示「正在查询…」、结束时把它
# 收成终态，所以每一个发过 on_tool_start 的调用都必须有配对的 on_tool_end——
# 漏一个，App 上那一行就永远转圈。 ----


class _LifecycleRecorder:
    def __init__(self) -> None:
        self.started: list[tuple[str, str, dict]] = []
        self.ended: list = []

    async def on_start(self, call_id: str, name: str, arguments) -> None:
        self.started.append((call_id, name, dict(arguments)))

    async def on_end(self, outcome) -> None:
        self.ended.append(outcome)


async def test_tool_start_carries_arguments_for_display() -> None:
    """展示层要渲染「正在搜索：BTC 为什么跌」，光有工具名不够。"""
    from app.agents.llm import LLMResult, LLMToolCall

    class _Executor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return {"source": "TAVILY_SEARCH", "query": "x", "answer": "a", "results": [{}]}

    class _LLM:
        async def complete(self, system, messages, tools):
            if any(m.role == "tool" for m in messages):
                return LLMResult(text="好的")
            return LLMResult(
                text="", tool_calls=(LLMToolCall("c1", "web_search", {"query": "BTC 为什么跌"}),)
            )

    hooks = _LifecycleRecorder()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("web_search",))
    runner = AgentRunner(profile, _Executor(), _LLM(), InMemoryAuditStore())

    await runner.run_turn(
        _session(("web_search",)),
        "BTC 为什么跌",
        "trace_t10a",
        _now(),
        on_tool_start=hooks.on_start,
        on_tool_end=hooks.on_end,
    )

    assert hooks.started == [("c1", "web_search", {"query": "BTC 为什么跌"})]


@pytest.mark.parametrize(
    ("exc", "expected_status"),
    [
        (None, "ok"),
        (RuntimeError("boom"), "no_data"),
    ],
)
async def test_every_started_tool_gets_a_matching_end(exc, expected_status) -> None:
    from app.agents.llm import LLMResult, LLMToolCall

    class _Executor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            if exc is not None:
                raise exc
            return {"balances": []}

    class _LLM:
        async def complete(self, system, messages, tools):
            if any(m.role == "tool" for m in messages):
                return LLMResult(text="好的")
            return LLMResult(text="", tool_calls=(LLMToolCall("c1", "get_balances", {}),))

    hooks = _LifecycleRecorder()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("get_balances",))
    runner = AgentRunner(profile, _Executor(), _LLM(), InMemoryAuditStore())

    await runner.run_turn(
        _session(("get_balances",)),
        "我的余额",
        "trace_t10b",
        _now(),
        on_tool_start=hooks.on_start,
        on_tool_end=hooks.on_end,
    )

    assert len(hooks.started) == len(hooks.ended) == 1
    assert hooks.ended[0].status == expected_status
    assert hooks.ended[0].call_id == "c1", "终态必须能对上是哪一次调用"


async def test_blocked_tool_also_reports_an_end() -> None:
    """写工具失败会终止整轮，但终态照样要发——否则前端那一行卡在转圈上。"""
    from app.agents.llm import LLMResult, LLMToolCall

    class _Executor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            raise RuntimeError("boom")

    class _LLM:
        async def complete(self, system, messages, tools):
            return LLMResult(
                text="", tool_calls=(LLMToolCall("c1", "create_trade_draft", {}),)
            )

    hooks = _LifecycleRecorder()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("create_trade_draft",))
    runner = AgentRunner(profile, _Executor(), _LLM(), InMemoryAuditStore())

    await runner.run_turn(
        _session(("create_trade_draft",)),
        "永续做多 0.1 BTC，限价 90000",
        "trace_t10c",
        _now(),
        on_tool_start=hooks.on_start,
        on_tool_end=hooks.on_end,
    )

    assert len(hooks.ended) == 1
    assert hooks.ended[0].status == "blocked"


async def test_duplicate_call_reports_neither_start_nor_end() -> None:
    """重复调用在 on_tool_start 之前就被短路了，前端从没见过这次调用，
    补一条终态反而会让它凭空多出一行。"""
    from app.agents.llm import LLMResult, LLMToolCall

    class _Executor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return {"balances": []}

    call = LLMToolCall("c1", "get_balances", {})

    class _LLM:
        async def complete(self, system, messages, tools):
            return LLMResult(text="", tool_calls=(call, call))

    hooks = _LifecycleRecorder()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("get_balances",))
    runner = AgentRunner(profile, _Executor(), _LLM(), InMemoryAuditStore())

    await runner.run_turn(
        _session(("get_balances",)),
        "我的余额",
        "trace_t10d",
        _now(),
        on_tool_start=hooks.on_start,
        on_tool_end=hooks.on_end,
    )

    assert len(hooks.started) == 1
    assert len(hooks.ended) == 1, "只有真正执行的那次有生命周期，重复的那次没有"


async def test_forced_web_search_also_reports_lifecycle() -> None:
    """强制联网路径绕过 LLM 直接执行工具，同样要走钩子。"""
    from app.agents.llm import LLMResult

    class _Executor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return {"source": "TAVILY_SEARCH", "query": "x", "answer": "", "results": [{}]}

    class _LLM:
        async def complete(self, system, messages, tools):
            return LLMResult(text="基于搜索结果的回答")

    hooks = _LifecycleRecorder()
    profile = AgentProfile("avatar", "fake", "你是数字人", ("web_search",))
    runner = AgentRunner(profile, _Executor(), _LLM(), InMemoryAuditStore())

    await runner.run_turn(
        _session(("web_search",)),
        "XYZ 为什么暴涨",
        "trace_t10e",
        _now(),
        web_search=True,
        on_tool_start=hooks.on_start,
        on_tool_end=hooks.on_end,
    )

    assert len(hooks.started) == len(hooks.ended) == 1
    assert hooks.ended[0].status == "ok"


async def test_blocked_outcome_carries_the_tool_name() -> None:
    """回归：三个 blocked 分支都漏了 tool_name，导致写工具失败时 WS 下发的
    tool.result.name 是空串——埋点和日志会拿到空值。"""
    llm = _ScriptedToolLLM("create_trade_draft", "不应该走到这里")
    profile = AgentProfile("avatar", "fake", "你是数字人", ("create_trade_draft",))
    hooks = _LifecycleRecorder()

    for exc in (httpx.TimeoutException("t"), ValueError("bad args"), RuntimeError("boom")):
        hooks.ended.clear()
        runner = AgentRunner(profile, _RaisingExecutor(exc), llm, InMemoryAuditStore())
        await runner.run_turn(
            _session(("create_trade_draft",)),
            "永续做多 0.1 BTC，限价 90000",
            "trace_t10f",
            _now(),
            on_tool_start=hooks.on_start,
            on_tool_end=hooks.on_end,
        )
        assert hooks.ended[0].status == "blocked"
        assert hooks.ended[0].tool_name == "create_trade_draft", f"{exc!r} 分支漏了 tool_name"


async def test_a_broken_display_hook_never_breaks_the_turn() -> None:
    """展示层的钩子是锦上添花，绝不能因为它抛异常就影响工具执行或整轮对话。

    并发批次尤其危险：gather 不会取消兄弟协程，一个钩子抛异常会让 run_turn
    立刻异常退出，而其余工具仍在后台跑完并往一条已经收尾的连接上发消息。
    """
    from app.agents.llm import LLMResult, LLMToolCall

    executed: list[str] = []

    class _Executor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            executed.append(tool_name)
            return {"balances": []}

    class _LLM:
        async def complete(self, system, messages, tools):
            if any(m.role == "tool" for m in messages):
                return LLMResult(text="你的余额是 0")
            return LLMResult(
                text="",
                tool_calls=(
                    LLMToolCall("c1", "get_balances", {}),
                    LLMToolCall("c2", "get_positions", {}),
                ),
            )

    async def _exploding_start(call_id, name, arguments):
        raise RuntimeError("渲染展示文案时炸了")

    async def _exploding_end(outcome):
        raise RuntimeError("推送终态时炸了")

    profile = AgentProfile("avatar", "fake", "你是数字人", ("get_balances", "get_positions"))
    runner = AgentRunner(profile, _Executor(), _LLM(), InMemoryAuditStore())

    result = await runner.run_turn(
        _session(("get_balances", "get_positions")),
        "看下我的账户",
        "trace_t10g",
        _now(),
        on_tool_start=_exploding_start,
        on_tool_end=_exploding_end,
    )

    assert result.text == "你的余额是 0"
    assert sorted(executed) == ["get_balances", "get_positions"]


async def test_pure_price_query_prunes_ask_investment_expert() -> None:
    """纯价格查询（如「BTC 永续合约当前价格是多少」）必须动态排除 ask_investment_expert，
    防止大模型误选投研证据工具并被强行套用「深度分析四步法」研报。"""
    from app.agents.llm import LLMResult, LLMToolCall

    seen_tools: list[tuple[str, ...]] = []

    class _LLM:
        async def complete(self, system, messages, tools):
            seen_tools.append(tools)
            if any(m.role == "tool" for m in messages):
                return LLMResult(text="BTC 现价为 65000 USDT")
            return LLMResult(
                text="",
                tool_calls=(LLMToolCall("c1", "get_market_price", {"symbol": "BTC"}),),
            )

    class _Executor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return {"symbol": "BTC", "last_price": "65000"}

    tools = ("get_market_price", "ask_investment_expert")
    profile = AgentProfile("avatar", "fake", "你是助手", tools)
    runner = AgentRunner(profile, _Executor(), _LLM(), InMemoryAuditStore())

    # 1. 纯价格查询：排除 ask_investment_expert，只留 get_market_price
    result = await runner.run_turn(
        _session(tools),
        "BTC 永续合约当前价格是多少",
        "trc_price_1",
        _now(),
    )
    assert result.text == "BTC 现价为 65000 USDT"
    assert "ask_investment_expert" not in seen_tools[0]
    assert "get_market_price" in seen_tools[0]

    # 2. 深度分析/归因查询：必须完整保留 ask_investment_expert
    seen_tools.clear()
    await runner.run_turn(
        _session(tools),
        "BTC 为什么跌了，分析一下原因",
        "trc_price_2",
        _now(),
    )
    assert "ask_investment_expert" in seen_tools[0]
    assert "get_market_price" in seen_tools[0]


async def test_pure_price_query_never_triggers_deep_analysis_format() -> None:
    """纯查价即使因某种原因拿到证据，也绝对不激活深度分析四步法研报指令。"""
    from app.agents.llm import LLMResult, LLMToolCall
    from app.agents.runner import _DEEP_ANALYSIS_TRIGGER

    captured_system: list[str] = []

    class _LLM:
        async def complete(self, system, messages, tools):
            captured_system.append(system)
            if any(m.role == "tool" for m in messages):
                return LLMResult(text="BTC 现价 65000")
            return LLMResult(
                text="",
                tool_calls=(LLMToolCall("c1", "web_search", {"query": "BTC price"}),),
            )

    class _Executor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            return {"results": [{"title": "BTC News", "content": "BTC ETF outflows"}]}

    profile = AgentProfile("avatar", "fake", "你是助手", ("web_search", "get_market_price"))
    runner = AgentRunner(profile, _Executor(), _LLM(), InMemoryAuditStore())

    await runner.run_turn(
        _session(("web_search", "get_market_price")),
        "BTC 现在多少钱",
        "trc_price_3",
        _now(),
        web_search=True,
    )

    # 哪怕 web_search 查到了有效网页，系统提示词也绝对不能注入四步法激活指令
    assert all(_DEEP_ANALYSIS_TRIGGER not in s for s in captured_system)

