"""系统提示词里的工具调用指引，必须与各链路真正拿得到的工具对得上。

一份 ``_DEFAULT_PROMPT`` 同时喂四条链路，但每条链路的工具集不一样：

* 文本网关 / OpenAI Realtime —— 全量注册表（18 个）
* Gemini Live —— ``GEMINI_LIVE_ALLOWED_TOOLS``（受控工具集）
* 火山 RTC —— 只有两个行情工具

所以提示词不能无条件点名某个工具，只能给「按本次会话实际提供的工具选择」的
映射。这里把提示词与 ``GEMINI_LIVE_ALLOWED_TOOLS`` 交叉断言：以后改白名单却
忘了改提示词，会在这里红掉，而不是等到线上模型去调一个不存在的工具。
"""

from __future__ import annotations

import re

from app.container import _DEFAULT_PROMPT, _TEXT_PROMPT, PERSONA_LOCK
from app.tools.builtin import build_default_registry
from app.voice.gemini_live import GEMINI_LIVE_ALLOWED_TOOLS


def test_prompt_names_every_tool_the_voice_channel_actually_gets() -> None:
    """语音直连只有这 6 个工具，提示词必须逐个点名，模型才会主动调。"""

    for tool in GEMINI_LIVE_ALLOWED_TOOLS:
        assert tool in _DEFAULT_PROMPT, tool


def test_prompt_routes_each_product_intent_to_one_draft_tool() -> None:
    """模型只理解产品入口，不应被兼容工具和 Catalog 实现细节干扰。"""

    assert "现货、兑换或 Swap 只能调用 create_spot_swap_draft" in _DEFAULT_PROMPT
    assert "永续、合约、做多、做空或杠杆交易只能调用 create_trade_draft" in _DEFAULT_PROMPT
    assert "create_perp_trade_draft" not in _DEFAULT_PROMPT
    assert "search_market_catalog" not in _DEFAULT_PROMPT


def test_prompt_only_names_tools_that_exist_in_the_registry() -> None:
    """提示词点名的工具必须真在注册表里，否则就是引导模型调不存在的工具。

    中文标点会和标识符粘在一起，按空白切词切不干净，所以直接正则捞
    snake_case 标识符。
    """

    registered = set(build_default_registry().names())
    named = set(re.findall(r"(?:get|create|search|market|update)_[a-z_]+", _DEFAULT_PROMPT))
    assert named, "提示词里一个工具名都没有，指引等于没写"
    for candidate in named:
        assert candidate in registered, candidate


def test_prompt_binds_tool_choice_to_the_session_toolset() -> None:
    """没提供的工具绝不许调 —— 这是一份提示词喂四条链路的前提。"""

    assert "只调用本次会话实际提供的工具" in _DEFAULT_PROMPT


def test_drafting_a_trade_does_not_require_a_catalog_lookup_first() -> None:
    """语音链路没有 search_market_catalog；起草交易必须能直接走 create_trade_draft。"""

    guide = _DEFAULT_PROMPT[_DEFAULT_PROMPT.index("create_trade_draft") :]
    assert "symbol" in guide
    # 旧提示词里那句无条件的「规划交易前必须先调用 search_market_catalog」是坑。
    assert "规划交易前必须先调用 search_market_catalog" not in _DEFAULT_PROMPT


def test_prompt_tells_the_model_to_call_tools_instead_of_guessing() -> None:
    """积极主动取真实数据：问到仓位/余额/资产/交易就调工具，不许凭记忆答。"""

    assert "主动调用" in _DEFAULT_PROMPT
    for topic in ("仓位", "余额", "资产"):
        assert topic in _DEFAULT_PROMPT, topic


def test_prompt_routes_balances_without_legacy_readiness_preflight() -> None:
    """稳定币/资产余额照常查询，钱包直签意图不得被旧 readiness 阻塞。"""

    assert "冻结余额时调用 get_balances" in _DEFAULT_PROMPT
    assert "创建意图前不得调用旧 Trading Readiness" in _DEFAULT_PROMPT
    assert "时调用 get_trading_readiness" not in _DEFAULT_PROMPT


def test_prompt_distinguishes_spot_and_perp_drafts() -> None:
    """语音既能起草 SPOT 也能起草 PERP，但不能把两条路径混用。"""

    assert "现货" in _DEFAULT_PROMPT
    assert "PERP" in _DEFAULT_PROMPT


def test_persona_lock_still_outranks_the_tool_guidance() -> None:
    assert _DEFAULT_PROMPT.startswith(PERSONA_LOCK)
    assert _DEFAULT_PROMPT.index(PERSONA_LOCK) < _DEFAULT_PROMPT.index("create_trade_draft")


def test_trading_red_lines_survive_the_rewrite() -> None:
    """改工具指引不许把风控铁律一起改没了。"""

    for red_line in (
        "绝不直接执行",
        "PENDING_USER",
        "Mobile 钱包直签",
        "as_of",
        "stale",
        "Hyperliquid Testnet",
        "不得编造价格",
    ):
        assert red_line in _DEFAULT_PROMPT, red_line


def test_prompt_instructs_draft_price_guard() -> None:
    """提示词必须指导模型在口语下单未指定价格时补全最新市价。"""

    assert "get_market_price" in _DEFAULT_PROMPT
    assert "建议限价" in _DEFAULT_PROMPT


def test_prompt_never_treats_market_data_as_draft_creation_evidence() -> None:
    assert "行情、余额和持仓工具的成功绝不代表草案已创建" in _DEFAULT_PROMPT
    assert "并在同一轮继续调用 create_trade_draft" in _DEFAULT_PROMPT
    assert "非空 trade_intent_id" in _DEFAULT_PROMPT
    assert "status=PENDING_USER" in _DEFAULT_PROMPT
    assert "非空 trade_draft_id" in _DEFAULT_PROMPT
    assert "尚未创建" in _DEFAULT_PROMPT


def test_prompt_only_drafts_after_explicit_trade_intent() -> None:
    assert "讨论行情、询问建议或查看余额时不得自动创建草案" in _DEFAULT_PROMPT


def test_prompt_requires_mobile_confirmation_after_draft_creation() -> None:
    assert "请用户在确认卡核对" in _DEFAULT_PROMPT
    assert "不得声称订单已提交或成交" in _DEFAULT_PROMPT


def test_prompt_restricts_account_readonly_tools_to_explicit_account_questions() -> None:
    """账户只读工具（余额/持仓/账户摘要）容易被诱导在分析大盘、宏观或资金流向时
    顺带调用，泄露用户个人账户信息。必须明确限定只在用户主动问自己的账户时才调用。"""

    for tool in ("get_balances", "get_positions", "get_account_summary"):
        assert tool in _DEFAULT_PROMPT, tool
    assert "我的账户" in _DEFAULT_PROMPT
    assert "我的余额" in _DEFAULT_PROMPT
    assert "我的持仓" in _DEFAULT_PROMPT
    assert "严禁调用" in _DEFAULT_PROMPT
    for topic in ("宏观", "大盘", "市场资金流向"):
        assert topic in _DEFAULT_PROMPT, topic


# ---- T5: 深度分析四步法 + 分场景篇幅（投研证据化改造）----
# 见 docs/plans/2026-09-02-investment-expert-evidence/TECH_DESIGN.md §4.7。


def test_prompt_splits_output_length_by_scenario() -> None:
    assert "200~300" in _DEFAULT_PROMPT
    assert "500~800" in _DEFAULT_PROMPT
    # 旧的无条件字数硬约束不该再存在——它是套话的根因之一。
    assert "篇幅严格控制在 200~300 字以内" not in _DEFAULT_PROMPT


def test_prompt_defines_the_four_step_deep_analysis_framework() -> None:
    for heading in ("盘面定性", "核心导火索", "资金与链上证据", "后市关键观察"):
        assert heading in _DEFAULT_PROMPT, heading


def test_prompt_bans_generic_filler_phrases() -> None:
    for phrase in (
        "受宏观情绪影响",
        "多空博弈加剧",
        "地缘局势升温",
        "市场流动性趋紧",
        "短期波动加大",
        "建议关注关键阻力位",
    ):
        assert phrase in _DEFAULT_PROMPT, phrase


def test_prompt_forbids_fabricating_data_for_unavailable_symbols() -> None:
    assert "unavailable_symbols" in _DEFAULT_PROMPT
    assert "严禁凭记忆编造" in _DEFAULT_PROMPT


def test_prompt_describes_investment_expert_as_evidence_only() -> None:
    assert "只给数据、不给结论" in _DEFAULT_PROMPT


def test_default_prompt_stays_within_length_budget() -> None:
    # _DEFAULT_PROMPT 同时喂三条语音链路的 systemInstruction，越长越容易稀释
    # 排在最前面的 PERSONA_LOCK（见 container.py 里那段注释）。
    # 实测基线：改造前（PERSONA_LOCK + _TOOL_GUIDE + _TRADING_RULES 等）已经
    # 约 3100 字符；本次新增 _DEEP_ANALYSIS_GUIDE 后约 3735。4200 留出未来
    # 小幅调整的余量，同时仍能拦住"提示词无限膨胀"这种真实回归。
    assert len(_DEFAULT_PROMPT) < 4200


def test_summary_is_three_bullets_not_one_paragraph() -> None:
    """重点摘要改成 3 条 bullet（对标币安的「⚡重点摘要」），不再是一段散文。

    一段 100 字的话在手机上还是要读完才知道结论；3 条 bullet 才是能扫的。
    """
    assert "重点摘要" in _DEFAULT_PROMPT
    assert "3 条" in _DEFAULT_PROMPT
    assert "不超过 100 字" in _DEFAULT_PROMPT, "总长上限是旧规则里唯一该留的部分"
    # 摘要必须排在四步法展开之前，模型才会先给结论再展开论据。
    summary_pos = _DEFAULT_PROMPT.index("重点摘要")
    for heading in ("盘面定性", "核心导火索", "资金与链上证据", "后市关键观察"):
        assert summary_pos < _DEFAULT_PROMPT.index(heading), heading


def test_prompt_asks_for_followup_questions_with_the_exact_parser_marker() -> None:
    """提示词里的标记必须和解析器用的是同一个常量。

    这两处只要写歪一个字，追问就永远解析不出来，而且**不会报错**——功能静默
    失效，只有人工去看输出才发现。所以拿常量本身来断言，不抄字面量。
    """
    from app.runtime.suggestion_split import MARKER

    assert MARKER in _TEXT_PROMPT
    assert "你可能还想问" in _TEXT_PROMPT


def test_followup_rule_is_not_cancelled_by_the_anti_menu_rule() -> None:
    """原来那句「严禁在结尾机械附加长串菜单选项」会把追问一起封死。

    它防的是套话式的功能菜单（「还可以帮你查行情/下单/看持仓…」），不是防
    结构化追问。改造后两条规则必须能共存：免责声明和无关下单提示仍然禁止。
    """
    assert "严禁在结尾机械附加长串菜单选项" not in _TEXT_PROMPT
    assert "重复免责声明" in _TEXT_PROMPT, "免责声明刷屏还是要禁"


def test_followup_questions_must_be_context_specific() -> None:
    """追问要跟着本轮上下文走，不能是三条放之四海而皆准的废话。"""
    assert "3 条" in _TEXT_PROMPT
    for phrase in ("本轮", "严禁"):
        assert phrase in _TEXT_PROMPT, phrase


def test_followup_questions_must_be_self_contained() -> None:
    """追问必须自带主语——这是真机串人事故的直接成因。

    文本回合是无状态的（app/agents/runner.py 每轮只把当前一句喂给模型），所以
    追问 chip 被点开时，"他/该基金/这只" 到了后端就是一个没有指代对象的代词。
    真机实测：问完 Leopold Aschenbrenner 的基金后，chip 是「他一季度具体做空了
    哪些股票？」，点开后模型把"他"补成了做空界词频最高的 Michael Burry，整段
    答案讲的是另一个人，而且理直气壮——搜索证据全是伯里的 13F。

    所以约束必须写在提示词里：每条追问都要能独立成句地被重新提问。
    """
    assert "代词" in _TEXT_PROMPT, "必须明说禁止代词，光说「紧扣本轮」模型会用「他」"
    for pronoun in ("他", "该基金", "这只"):
        assert pronoun in _TEXT_PROMPT, f"负面清单要点名 {pronoun}，泛泛而谈模型照犯"
    assert "完整主语" in _TEXT_PROMPT or "自带主语" in _TEXT_PROMPT


def test_voice_prompt_does_not_ask_for_followup_chips() -> None:
    """追问区只能出现在文本链路的提示词里。

    剥离追问的 SuggestionSplitter 只挂在文本 WS 上（app/api/ws.py）。语音三条
    链路拿的是同一份 instructions，却没有任何人剥标记——模型照着提示词输出
    「【你可能还想问】1. …」，TTS 就把这行控制符连着三条问题一起念出来。

    追问本身是文字 UI 的交互（渲染成可点的 chip），语音里没有对应载体，所以
    正确的做法是这段指令根本不进语音提示词，而不是在语音侧再补一个剥离器。
    """
    from app.runtime.suggestion_split import MARKER

    assert MARKER not in _DEFAULT_PROMPT
    assert "推荐追问" not in _DEFAULT_PROMPT
    # 语音提示词是文本提示词的前缀：人设锁、语言锁、工具指引全都共享，
    # 只有追问段是文本独有的。
    assert _TEXT_PROMPT.startswith(_DEFAULT_PROMPT)


def test_text_prompt_stays_within_its_own_length_budget() -> None:
    """文本链路多带追问与历史两段指令，预算单独算。

    语音那份（_DEFAULT_PROMPT）反而因为拆掉追问段变短了——它才是稀释
    PERSONA_LOCK 的高风险场景（systemInstruction 一次性注入，之后每轮都靠它）。

    实测基线：追问段 4210，加上 _HISTORY_GUIDE（173 字）后 4383。4600 留出的
    余量与上一版（4210/4400）相当，同时仍能拦住"提示词无限膨胀"这种真实回归。
    """
    assert len(_TEXT_PROMPT) < 4600


def test_text_prompt_forbids_treating_history_as_current_data() -> None:
    """历史只用于理解指代，不是事实来源。

    不写这条，模型会拿三轮前的旧价格当现价报出来——比串人更难发现，数字看着
    合理，只是过期了。
    """
    for phrase in ("指代", "重新调"):
        assert phrase in _TEXT_PROMPT, phrase
    assert "历史" in _TEXT_PROMPT


def test_voice_prompt_has_no_history_guide() -> None:
    """语音链路不走 Session.turns（它有自己的 recent_context 注入），
    这段约束进去只是白占 systemInstruction 长度、稀释人设锁。
    """
    assert "严禁把历史里的价格" not in _DEFAULT_PROMPT
