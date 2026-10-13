"""评测测题集。

覆盖四类典型对话，对应 runner 里几条差异很大的代码路径：
单工具查询 / 联网搜索 / 多步工具 / 纯闲聊（不调工具）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 与 build_default_registry() 对齐的常用只读工具，测题里按需取子集。
READONLY_TOOLS = (
    "get_positions",
    "get_balances",
    "get_account_summary",
    "get_market_price",
    "market_tickers",
    "web_search",
    "ask_investment_expert",
)


@dataclass(frozen=True)
class EvalCase:
    """一道测题。

    `expect_tool_calls` 是**期望模型至少用到**的工具，不要求顺序，也不要求穷尽——
    评测的目的是发现「该调的没调」「不该调的乱调」，不是把模型钉死在某条路径上。
    """

    name: str
    question: str
    scope_tools: tuple[str, ...]
    web_search: bool = False
    expect_tool_calls: tuple[str, ...] = ()
    expect_no_tool_calls: bool = False
    # 期望模型一次性请求全部工具（并发批次），而不是一轮一个串行调用。
    # 两条路径在 runner 里差别很大：只有 len(tool_calls) > 1 才会走 asyncio.gather
    # 的合批分支，也才会产生「一个 assistant 回合带多个 ToolCallPart」的历史。
    parallel_calls: bool = False
    note: str = ""


CASES: tuple[EvalCase, ...] = (
    EvalCase(
        name="market_price",
        question="BTC 现在多少钱",
        scope_tools=("get_market_price", "market_tickers"),
        expect_tool_calls=("get_market_price",),
        note="单工具查询：最短路径，验证一轮工具调用后能正常收口",
    ),
    EvalCase(
        name="portfolio",
        question="帮我看下持仓",
        scope_tools=("get_positions", "get_balances"),
        expect_tool_calls=("get_positions",),
        note="账户类只读工具",
    ),
    EvalCase(
        name="web_search_news",
        question="XYZ 币最近有什么新闻",
        scope_tools=("web_search",),
        web_search=True,
        expect_tool_calls=("web_search",),
        note="强制联网路径：运行时替模型执行一次搜索再回灌，"
        "结构上必须让模型看起来像是自己调的",
    ),
    EvalCase(
        name="multi_step_analysis",
        question="比特币今天为什么跌了",
        scope_tools=("web_search", "ask_investment_expert", "get_market_price"),
        web_search=True,
        # 必须列两个工具：强制联网分支在进入模型循环之前就把 web_search 塞进历史了，
        # 只写 web_search 的话模型脚本一看「已经调过」就直接收尾，这道题会退化成
        # 和 web_search_news 完全一样的单轮，多轮交替根本没被测到。
        expect_tool_calls=("web_search", "get_market_price"),
        note="多步工具：强制搜索之后再查行情，逼出两轮 ToolCall/ToolReturn 交替",
    ),
    EvalCase(
        name="parallel_account_query",
        question="看下我的余额和持仓",
        scope_tools=("get_balances", "get_positions"),
        expect_tool_calls=("get_balances", "get_positions"),
        parallel_calls=True,
        note="并发只读工具：同一轮多个调用应合成一个 assistant 回合",
    ),
    EvalCase(
        name="small_talk",
        question="你好呀，今天心情怎么样",
        scope_tools=READONLY_TOOLS,
        expect_no_tool_calls=True,
        note="纯闲聊：工具全开也不该调用任何一个",
    ),
    EvalCase(
        name="cross_asset_compare",
        question="帮我对比一下 BTC 和 ETH 最近的走势，哪个更强",
        scope_tools=("market_tickers", "get_market_price", "web_search"),
        expect_tool_calls=("market_tickers",),
        note="跨标的对比：验证【重点摘要】能不能在多标的场景下收敛成 3 条，"
        "以及追问会不会退化成「大盘怎么样」这类泛问",
    ),
    EvalCase(
        name="macro_news",
        question="最近美联储的动向对加密市场有什么影响",
        scope_tools=("web_search", "ask_investment_expert"),
        web_search=True,
        expect_tool_calls=("web_search",),
        note="宏观资讯：篇幅最长的深度分析场景，最容易把摘要写成一整段、"
        "也最容易在结尾堆免责声明而漏掉追问",
    ),
    EvalCase(
        name="concept_question",
        question="解释一下什么是永续合约的资金费率",
        scope_tools=READONLY_TOOLS,
        expect_no_tool_calls=True,
        note="概念解释：靠模型自身知识回答，不需要实时数据",
    ),
)


@dataclass
class CaseOutcome:
    """一道测题跑完的结果，供报告渲染。"""

    case: EvalCase
    answer: str = ""
    tool_calls: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    message_shape: list[str] = field(default_factory=list)
    error: str = ""
