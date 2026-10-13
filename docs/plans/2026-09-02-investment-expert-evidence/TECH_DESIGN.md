# 技术设计：投研深度分析链路重构

关联文件：[PRD.md](./PRD.md) · [TASK_LIST.md](./TASK_LIST.md)

## 4.1 目标调用链

```
现状（3 跳 LLM）：
  用户 → 主脑LLM#1(选工具) → ask_investment_expert{内部LLM#2:无数据→150字套话}
                            → 主脑LLM#2(读套话→压成300字) → 用户

改后（2 跳 LLM）：
  用户 → 主脑LLM#1(选工具) → ask_investment_expert{纯并发抓取:Core行情+本地新闻,零LLM}
                            → 主脑LLM#2(读证据包→四步法→500~800字) → 用户
```

`ask_investment_expert` 从"迷你分析师"降级为"跑腿取证员"，分析工作全部搬到主脑本来就要跑的那一轮里。

## 4.2 新增模块 `app/services/market_evidence.py`

职责单一的证据组装，无状态、无 LLM、永不抛异常（遵循 `tests/test_investment_expert_tools.py` 文件头写死的约定：被 LLM 调用的路径上抛异常会崩掉整个 turn / websocket）。

```python
_DEFAULT_MAX_SYMBOLS = 8
_DEFAULT_MAX_NEWS_ITEMS = 5
_DEFAULT_MAX_EXCERPT_CHARS = 300
_MARKET_SOFT_TIMEOUT_SECONDS = 6.0   # 低于 core_timeout_seconds=10（遗留问题 ③）

async def build_market_evidence(
    *,
    question: str,
    symbols: Sequence[str] = (),
    core: CoreClient | None = None,
    news_store: NewsStore | None = None,
    news_lookback_hours: int = 24,
    max_news_items: int = _DEFAULT_MAX_NEWS_ITEMS,
    max_excerpt_chars: int = _DEFAULT_MAX_EXCERPT_CHARS,
    now: datetime | None = None,
) -> dict[str, Any]: ...
```

内部结构：
- `asyncio.gather(_fetch_market(...), _fetch_news(...), return_exceptions=True)` —— 两路并行，任一路失败只污染自己那一支。
- `_fetch_market`：逐个 symbol 经 `catalog_asset_query()` 归一化，**每个符号独立 try/except**（`catalog_asset_query("黄金")` 会剥光非 `A-Z0-9` 后抛 `MarketInstrumentResolutionError`，`app/tools/agentos/trade_flow.py:73-77`），失败的进 `unavailable_symbols`；再对存活的符号做一次批量 `core.get_market_tickers(...)`；整个抓取包在 `asyncio.wait_for(..., 6.0)` 里。
- `_fetch_news`：直接复用 PR #60 的 `search_local_news(store, question, lookback_hours=...)`，不另起检索逻辑。

返回形状（键名稳定，供提示词直接引用）：

```python
{
  "source": "MARKET_EVIDENCE",
  "question": "...",
  "as_of": "2026-09-02T...Z",
  "market": {
      "data": [{"symbol": "BTC", "last_price": "...", "change_percent_24h": "...",
                "high_24h": "...", "low_24h": "...", "funding_rate": "...",
                "open_interest": "...", "quote_volume_24h": "...",
                "as_of": "...", "stale": False}, ...],
      "unavailable_symbols": ["黄金"],
      "error": None,
  },
  "news": {"source": "LOCAL_NEWS_STORE", "items": [{"title","published_at","source_name","url","excerpt"}],
           "error": None},
  "coverage": {"has_market": True, "has_news": True},
  "evidence_policy": "只能基于以上字段作答；unavailable_symbols 中的标的必须明说暂无实时行情、"
                     "只能依据新闻定性，严禁编造点位、涨跌幅或资金费率。",
  "evidence_text": "……（下述预渲染纯文本）……",
}
```

`evidence_text` 是把结构体渲染成的紧凑 Markdown（行情一行一个标的、新闻一行一条），模型读渲染好的文本远好于读嵌套 JSON 转义串，由 `_summarize` 优先取用（§4.6）。结构化字段保留，供日志、指标和未来程序化消费。

尺寸预算：8 标的 × ~12 字段 ≈ 900 字符；5 条新闻 × (标题+300字摘要) ≈ 1800 字符；合计约 2700~3200 字符 ≈ 1500~2200 token，相对 4096 的输出上限完全可忽略。

## 4.3 `InvestmentExpertClient` 新接口

```python
class InvestmentExpertClient:
    """投研证据抓取客户端。不做分析、不调 LLM，只并发组装行情与新闻证据。"""

    def __init__(
        self, *, core: CoreClient | None = None, news_store: NewsStore | None = None,
        news_lookback_hours: int = 24,
    ) -> None: ...

    async def collect_evidence(
        self, question: str, *, symbols: Sequence[str] = (), chat_id: str | None = None,
    ) -> dict[str, Any]: ...
```

删除项：`_EXPERT_SYSTEM_PROMPT`、`_FAST_SYSTEM_PROMPT`、`chat()`、`_compute_cache_key()`、`llm` 构造参数、Redis 缓存分支。
保留项：`InvestmentExpertUpstreamError`（证据抓取仍需区分超时/状态码）、全部向后兼容别名。

**Redis 缓存下线（遗留问题 ②）**：建议彻底删除。理由：(1) 原本保护的是一次付费 LLM 调用，已不存在；(2) 剩下的两个操作是内网 ticker 读 + 本地 PG 查询，缓存收益接近零；(3) 会引入正确性 bug——把 10 分钟前的价格当"实时行情"喂给模型做点位归因，比套话更糟。`Settings.investment_expert_cache_ttl_seconds` 字段保留但标注 deprecated，避免动 staging 的 env 注入路径。

## 4.4 `ask_investment_expert` 工具 schema

```python
Tool(
    name="ask_investment_expert",
    external=True,
    description=(
        "抓取行情与新闻证据包，供你自己做深度归因分析。本工具不返回任何结论，"
        "只返回：① 指定加密标的的 Core 实时行情快照（最新价、24h 涨跌幅、24h 高低点、"
        "资金费率、持仓量、成交额）；② 近期相关新闻要点。"
        "当用户问「为什么涨/跌」「宏观复盘」「多标的对比」等需要具体数据支撑的问题时，"
        "一次性调用本工具并把全部标的一起传入，然后基于返回的数据自己写出分析结论。"
        "不得用于查询个人交易账户、余额或订单信息。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "用户的完整问题原文，保留全部查询意图，用于检索相关新闻。"},
            "symbols": {"type": "array", "items": {"type": "string"}, "maxItems": 8,
                        "description": "需要行情快照的加密标的基础符号（如 BTC、ETH、SOL）。"
                                       "黄金、原油、美股、美债等非加密标的不要填入，"
                                       "这些标的的信息只会出现在新闻部分。"},
        },
        "required": ["message"],
        "additionalProperties": False,
    },
)
```

`mode` 参数移除。**executor 对残留的 `mode` 必须静默忽略而不是报错**——现状 `app/tools/executor.py:142-143` 遇到非法 mode 直接返回 error，如果模型出于历史习惯还带了 `mode`，就会白烧一整轮。

```python
class _EvidenceClient(Protocol):
    async def collect_evidence(
        self, question: str, *, symbols: Sequence[str] = ..., chat_id: str | None = ...
    ) -> dict[str, Any]: ...

async def ask_investment_expert(
    message: str, *, client: _EvidenceClient,
    symbols: Sequence[str] = (), chat_id: str | None = None,
) -> dict[str, Any]:
    """抓取证据包，失败一律降级为 error dict，绝不抛异常。"""
```

## 4.5 `ToolExecutor` 分派（`app/tools/executor.py:137-144` 替换）

```python
if tool_name == "ask_investment_expert":
    if self._investment_expert_client is None:
        return {"error": "投研证据抓取未配置"}
    message = str(args.get("message") or "")
    raw_symbols = args.get("symbols") or []
    if not isinstance(raw_symbols, list) or len(raw_symbols) > 8 \
       or any(not isinstance(s, str) for s in raw_symbols):
        return {"error": "symbols 必须是最多 8 个字符串"}
    # 历史 mode 参数已下线，收到也静默忽略——为它返回 error 会白烧一整轮。
    return await ask_investment_expert(
        message, client=self._investment_expert_client, symbols=tuple(raw_symbols)
    )
```

`__init__` 里的 `minara_client=` 关键字参数与 `self._minara_client` 内部别名（`executor.py:81-82, 107-108`）原样保留，不动构造签名。

## 4.6 `_summarize` 修复（`app/agents/runner.py:57-59`）

```python
# 工具结果回灌预算。原来统一硬切 500 字符：PR #60 把 Tavily 结果做到 5 条 ×
# 500 字，但模型只看得到前 500 个字符，而且是从 JSON 中间切断的破损转义串，
# 模型连"这里被截断了"都不知道 —— 联网搜到了却答得很水，一半原因在这里。
_TOOL_RESULT_DEFAULT_LIMIT = 2000
_TOOL_RESULT_LIMITS = {
    "ask_investment_expert": 6000,   # 证据包是本轮分析的全部原料，不能截
    "web_search": 4000,              # 5 条 × 500 字摘要 + 结构开销
    "get_balances": 1500, "get_positions": 1500, "get_account_summary": 1500,
}

def _summarize(out: dict, tool_name: str = "") -> str:
    limit = _TOOL_RESULT_LIMITS.get(tool_name, _TOOL_RESULT_DEFAULT_LIMIT)
    text = out.get("evidence_text")
    if not isinstance(text, str) or not text:
        text = json.dumps(out, ensure_ascii=False)
    if len(text) <= limit:
        return text
    return text[:limit] + "…（结果已截断）"
```

两处调用点（377 行、428 行）改为传入 `tool_name`。

## 4.7 提示词改写（`app/container.py`）

### `_OUTPUT_STYLE_GUIDE` 重写（105-114 行）

```python
_OUTPUT_STYLE_GUIDE = (
    "【输出篇幅·按场景分层·极高优先级】面向手机 App 用户，回答必须信息密度高、无套话。"
    "① 简单查询场景（余额、持仓、单一标的价格、交易草案确认等，只调用了只读账户或行情工具）："
    "篇幅 200~300 字，结构化分点，直击核心数据与结论，杜绝铺垫与过度分析。"
    "② 深度分析场景（本轮收到了 ask_investment_expert 返回的证据包，或 web_search 返回的搜索结果）："
    "篇幅放宽到 500~800 字，必须把拿到的证据用满，严禁只写三五句概括就收尾。"
    "两种场景都严禁在结尾机械附加长串菜单选项、重复免责声明或无关的下单提示，避免信息过载；"
    "仅在用户明确表达下单买卖意图时才提示确认卡。"
    "【工具调用合并与单轮终结原则】"       # ← 以下原文一字不动
    "当用户询问跨市场（如A股/港股/美股）、多资产（如黄金/原油/美债/股指）或宏观复盘等综合问题时，"
    "必须在 1 次 ask_investment_expert 或 web_search 工具调用中一次性传入全部对比与分析需求，"
    "严禁将一个综合问题拆分成多个碎片工具分批调用；获得工具结果后必须立即输出最终总结，严禁连续多轮重复调用分析工具。"
)
```

### 新增 `_DEEP_ANALYSIS_GUIDE`（约 350 字，控制总长度见 §5.4）

```python
_DEEP_ANALYSIS_GUIDE = (
    "【深度分析四步法·仅在本轮收到证据包或搜索结果时适用】"
    "先给一段不超过 100 字的【核心结论速览】：一句话说清涨跌方向与最主要原因，方便用户扫读；"
    "紧接着必须严格按以下四段展开，每段都要挂具体数字或具体事件（速览段与四段合计仍是 500~800 字）："
    "1)【盘面定性】写出具体点位：当前价、24 小时涨跌幅、24 小时高低点、是否破位或创新高；多标的时逐个列出并横向对比。"
    "2)【核心导火索】写出具体事件、政策、数据或时间点（如某项经济数据的公布值、某机构的公告、某次大额清算）；"
    "证据包与搜索结果里没有的事件，绝对不允许自己编。"
    "3)【资金与链上证据】引用证据包里的资金费率、持仓量、成交额，以及新闻中的 ETF 流入流出、爆仓金额等，"
    "并说明这些数字指向多头还是空头占优。"
    "4)【后市关键观察】给出下一个具体的关键价位或事件节点（如某价位的多空防守位、某日的数据公布）。"
    "【套话负面清单】严禁输出没有数字或事实支撑的空泛归因，包括但不限于"
    "「受宏观情绪影响」「多空博弈加剧」「地缘局势升温」「市场流动性趋紧」「短期波动加大」「建议关注关键阻力位」；"
    "每一条归因后面都必须紧跟证据包或搜索结果里的具体数据、事件或时间。"
    "【数据缺失处理】证据包中列入 unavailable_symbols 或带 error 的标的，必须明说该标的暂无实时行情数据、"
    "只能依据新闻定性，严禁凭记忆编造点位、涨跌幅或资金费率。"
)

_DEFAULT_PROMPT = (
    PERSONA_LOCK + _LANGUAGE_LOCK + _OUTPUT_STYLE_GUIDE
    + _DEEP_ANALYSIS_GUIDE + _TOOL_GUIDE + _TRADING_RULES
)
```

### `_TOOL_GUIDE` 里唯一的改动（132-133 行两句）

```python
# 原文：
#   "【投研深度分析】当用户需要宏观经济、行业板块、多市场复盘、财报解读等深度研判时，"
#   "调用 ask_investment_expert 进行一次性分析。"
# 改为：
    "【投研证据抓取】当用户需要宏观经济、行业板块、多市场复盘、涨跌归因等深度研判时，"
    "先调用一次 ask_investment_expert 取回行情与新闻证据包（它只给数据、不给结论），"
    "拿到证据后由你自己按「深度分析四步法」写出结论；证据里没有的数据绝不允许自己补。"
```

其余各段（【总原则】【真实数据】【账户工具防滥用】【联网搜索】【交易类型分流】【PERP草案】【SPOT草案】【成功证据】）与 `_TRADING_RULES` 全段**一字不动**。

## 4.8 场景判断怎么做到可靠

**不要让模型做话题分类，要让它读客观事实。** 三层防线：

**第一层｜物理隔离（最可靠，零成本）。** `ask_investment_expert` 和 `web_search` 都不在 `GEMINI_LIVE_ALLOWED_TOOLS`（`app/voice/gemini_live.py:35-44` 只有 6 个行情/持仓/草案工具），火山 RTC 只有两个行情工具。**语音链路在物理上永远不可能收到证据包，也就永远不会进入 500~800 字档**——不需要为语音单独维护一份 prompt。

**第二层｜提示词里写成模型可自查的客观事实。** 条件写成「本轮收到了证据包或搜索结果」而不是「当用户问宏观类问题时」——前者模型扫一眼消息列表里有没有 `[工具结果]` 段就能判定（`PydanticAILLM.complete` 在 `app/agents/llm.py:132` 把 tool 消息渲染成 `[工具结果] ...`），后者要求模型先做主观分类，误判率高得多。

**第三层｜运行时无条件补刀（决定性的一票）。** 本轮一旦真的执行成功了证据类工具，就往 system prompt 尾部追加一句无条件指令：

```python
_EVIDENCE_TOOLS = frozenset({"ask_investment_expert", "web_search"})
_DEEP_ANALYSIS_TRIGGER = (
    "【本轮已获得证据数据】你已经拿到 ask_investment_expert 证据包或 web_search 搜索结果，"
    "本轮必须按系统提示词中的「深度分析四步法」四段式输出，篇幅 500~800 字，"
    "每一条归因都必须挂上证据里的具体数字或事件，禁用套话负面清单里的句子。"
)
```

不是新机制——`runner.py:198-207` 已经在用相同手法追加「本轮联网模式」两段。实现要点：用局部 `deep_analysis_activated: bool` 标志只追加一次；两处触发点都要覆盖（强制 web_search 分支成功后 375 行附近、常规工具循环里 `status=="ok"` 且 `tool_name in _EVIDENCE_TOOLS` 时 424-428 行）；追加动作必须发生在下一次 `self._llm.complete(system_prompt, ...)`（380行）之前。

## 4.9 是否需要一步摘要压缩：不需要，明确反对

理由四条：
1. **没有东西可压。** 证据包上限约 3200 字符 ≈ 1500~2200 token，相对 `llm_max_output_tokens=4096` 的输出预算可忽略。
2. **压缩步就是我们正在删掉的那个东西。** 今天的 `_EXPERT_SYSTEM_PROMPT`「150~200字」本质就是一个 LLM 压缩步，正是把"资金费率转负"压成"短期波动加大"的元凶。再加一层有损压缩等于把 bug 修复的同时重新装回去。
3. **会把这次改动的延迟收益吐回去。** 收益全部来自"砍掉一整轮串行LLM"，加回摘要调用会重新引入串行等待和新的超时/失败面。
4. **真要控量，用确定性手段。** `max_news_items`/`max_excerpt_chars`/`maxItems:8` 做成参数，源头限流，无损、零延迟、可测试。

## 4.10 容器接线（`app/container.py:490-514`）

```python
# 删除整个 investment_expert_llm 分支（490-509 行），包括独立的 PydanticAILLM 实例
# —— "砍掉一整轮 LLM 调用"在代码上就是这几十行的消失。

# 注意执行顺序：news_store 目前在 518 行构造，晚于 510 行的 client，
# 必须把 news_store 的构造上移到 client 之前（engine 在 477 行前已确定，安全）。
news_store: NewsStore = PostgresNewsStore(engine) if engine is not None else InMemoryNewsStore()

investment_expert_client = InvestmentExpertClient(
    core=core, news_store=news_store,
    news_lookback_hours=settings.news_local_search_lookback_hours,
)
```

Fake 模式无需特殊处理：`FakeCoreClient.get_market_tickers` 直接 `raise RuntimeError`，`build_market_evidence` 捕获后写进 `market.error`——这正是要的优雅降级，且必须有测试覆盖。

配置侧：`investment_expert_llm_model`/`minara_llm_model` 字段保留（`test_perf_and_interrupt.py:98-112` 断言它们），仅注释标注 deprecated。CI workflow（`deploy-staging.yml`）本次不动——那条 `set_env_optional` 现在只是注入没人读的变量，无害；按仓库既有教训（preflight 顺序/set_env漏项会让整个服务起不来），非必要不碰部署路径。

---

## 5. 风险与兼容性

### 5.1 不回归 PR #61 的「投研分析 vs 联网搜索」路由边界

**承诺：本次 diff 在 `app/agents/runner.py` 76-111 行（`_TIME_SENSITIVE_PATTERN`/`_looks_time_sensitive`/`_LIVE_DATA_REQUIRED_PATTERN`/`_requires_live_data`）与 316-331 行（排除块）必须是 0 改动。** 这两处进 PR checklist，评审时先看这一条。

守护测试（全部保持原样、必须全绿）：`test_requires_live_data_is_narrower_than_looks_time_sensitive`、`test_market_review_query_keeps_investment_expert_even_with_recency_words`、`test_time_sensitive_query_excludes_investment_expert_when_web_search_off`、`test_live_data_query_keeps_investment_expert_when_web_search_not_in_scope`、`test_non_time_sensitive_query_keeps_investment_expert_available`、`test_time_sensitive_query_does_not_exclude_investment_expert_when_web_search_on`。这些测试用 stub executor 只断言 `llm.tools_seen`，不真跑工具，本次改动天然不触及它们。

**语义漂移需主动向评审交代**：PR #61 排除 `ask_investment_expert` 的原始理由（"只靠训练记忆或旧缓存回答，本质是冒充最新分析"）在改造后部分不成立了——它现在真的会去拿实时行情和本地新闻。本次仍保留排除逻辑不动（本地新闻库召回面 ≠ 联网搜索广度；路由边界是独立议题），是否放宽见 PRD 遗留问题 ⑤。

### 5.2 不回归 PR #60 的联网搜索链路

`local_news_tool_output`/`normalize_search_query`/`extract_search_keywords`/`TavilySearchClient`/`executor.execute("web_search")` 分支——原样复用，一行不改。`tests/test_web_search.py` 全部保持不变且全绿。

**例外，2026-09-02 补充**：`search_local_news` 本身在验收阶段被改动了（不在原计划内）。原设计是"一行不改"，但本地烟测拿用户投诉的原句"黄金和比特币近期行情大变动的原因"实跑一遍后发现：`extract_search_keywords` 拆出 `['黄金', '比特币...']` 两个关键词，而 `search_local_news` 是"命中第一个有结果的关键词就返回，不做跨关键词合并"——只要"黄金"先命中，比特币那部分新闻证据就完全拿不到。这直接命中本次改造要解决的核心问题（证据不完整→分析空洞），所以改成并发查询全部候选关键词并按 `news_id` 去重合并，"查得到就秒回"的延迟特性通过并发（而非串行）保留。影响面：`web_search` 工具路径与证据包路径共用这一个函数，两边一起受益；`executor.py:165-176` 的"本地有结果就跳过 Tavily"判断条件不变，只是本地结果现在更完整。见 `app/news/local_search.py` 与 `tests/news/test_local_search.py` 新增的合并/去重/limit 测试。

原交集（`_summarize` 上限放宽）是**修复 PR #60 的隐形瓶颈**：PR #60 把 Tavily 结果做到 5 条×500字，但 runner 只把前 500 字符喂给模型，收益大部分被吃掉。PR 描述里必须点明这两处因果，避免被误读成"又在动刚优化过的搜索链路"。

**已知限制，本轮明确不修（2026-09-02 产品方确认）**：`extract_search_keywords`（`app/tools/query_normalization.py`）是正则子串切分，不是真正的中文分词，`_TOPIC_NOISE_PATTERN` 噪声词表覆盖面有限。用户原句"黄金和比特币**近期**行情大变动的原因"里，"近期"不在噪声词表（表里只有"最近"），导致切出 `['黄金', '比特币近期行情大变动的原因']`，第二个词几乎不可能匹配到任何真实新闻标题——多关键词合并逻辑对这句具体措辞救不回来，因为问题在分词质量本身。这是 PR #60 就有的既有设计（规则清洗，未接分词库），不是本次改动引入的，影响面也有限：`web_search` 走 `normalize_search_query`（整句不切词，直接交给 Tavily），完全不受影响；只有本地新闻库子串匹配这一路会漏，且提示词已允许模型同轮合并调用 `ask_investment_expert` + `web_search` 互补。真要修需要接分词库（如 jieba）并配套过滤逻辑，属于独立规模的工作（新依赖、部署镜像验证、真实语料测试、单独走 planner→TDD→review），本轮不做，留作后续独立议题。

### 5.3 向后兼容别名

全仓 grep 确认：`MinaraClient`/`MinaraUpstreamError`/`FakeMinaraClient`/`MINARA_TOOLS` 引用点只有兼容 shim 自身和 config env alias，无任何生产调用点。

- 类名/异常/工具元组别名全部保留（一行成本），shim 模块文件保留不动。
- **`chat()` 方法直接删除，不留 shim。** 留假 `chat()` 只会让"证据包"和"分析结论"两个概念继续混在一起。方法级不兼容，风险为零（无外部调用者）。
- `InvestmentExpertUpstreamError`/`MinaraUpstreamError` 保留：证据抓取仍会遇到 Core 超时、非 2xx 等需要区分错误类型的场景。
- `Settings.minara_llm_model` property 与全部 env alias 保留。

### 5.4 语音链路（`_DEFAULT_PROMPT` 一份喂四条链路）

`_DEFAULT_PROMPT` 经 `container.py:388` 进 `VoiceProviderContext.instructions`，落到 Gemini Live 的 `systemInstruction`、OpenAI Realtime 的 `instructions`、火山的 `system_messages`。

- 功能面安全：`_DEEP_ANALYSIS_GUIDE` 触发条件是"收到证据工具结果"，语音链路物理上拿不到这两个工具（§4.8 第一层），永不进入 500~800 字档。
- 真实风险是提示词变长稀释 `PERSONA_LOCK`。缓解：`_DEEP_ANALYSIS_GUIDE` 控制在约 550 字（含四步法+负面清单+数据缺失处理，比最初设想的 350 字略长，换来负面清单可以覆盖足够多的套话）；位置排在 `PERSONA_LOCK` 之后（`test_persona_lock_still_outranks_the_tool_guidance` 守着顺序）；新增 `_DEFAULT_PROMPT` 总长上限断言。**实测基线**：改造前 `PERSONA_LOCK+_TOOL_GUIDE+_TRADING_RULES` 等已约 3100 字符（3000 的预估本身定低了，不是本次改动才撑大的），加上 `_DEEP_ANALYSIS_GUIDE` 后约 3735，测试阈值相应定为 4200（留合理余量，仍能拦住无限膨胀）。
- 必须继续通过：`test_prompt_names_every_tool_the_voice_channel_actually_gets`、`test_prompt_only_names_tools_that_exist_in_the_registry`（新写段落里不能出现像工具名但没注册的词，如 `market_snapshot`、`get_market_evidence`）。

### 5.5 `_summarize` 放宽的副作用

放宽后所有工具结果变长，包括含账户数据的 `get_balances`/`get_positions`/`get_account_summary`。核查：这些结果本来就要给模型看；不影响审计（`result_ref` 只存 draft_id 或 session_id，与 `_summarize` 无关）；上一个 commit `9d739bf` 刚修的"工具执行异常对外脱敏"在 `_summarize` 之前的分支，不受影响；仍给账户类工具单独设 1500 更小上限，控 token 也控个人数据入上下文的量。

### 5.6 Fake 模式

- `FakeInvestmentExpertClient.chat()` → `collect_evidence()`，返回形状真实但内容明显是假的证据包。
- **关键约束：fake 数据里绝不能出现"分析结论"文本。** 现在的 `_FAKE_CHAT_RESPONSE`（"短期波动加大，建议关注关键阻力位"）如果照搬会掩盖"主脑没在做分析"这个 bug，而且本身就命中新写的套话负面清单。fake 里只放数据。
- `FakeLLM` 不需要改：`_TOOL_KEYWORDS`（`llm.py:61-72`）不含投研相关词，fake 链路永远不会触发证据工具，不要为了演示往里加关键词。
- `FakeCoreClient.get_market_tickers` 抛 `RuntimeError` 的路径必须有专门测试。

---

## 7. 延迟预算核算（详细版，摘要见 PRD §4）

| 阶段 | 现状 | 改后 |
|---|---|---|
| 主脑第 1 轮（选工具） | 3~8s | 3~8s |
| 中间跳 | expert LLM 2~15s | 证据抓取（`asyncio.gather` 并行，软超时6s）1~2s |
| 主脑第 2 轮（生成回复） | 200~300字 ≈ 5~10s | 500~800字，多~400-700 output token @~50tok/s → 13~24s |
| **合计** | **10~33s** | **17~34s** |

净效果：砍掉一整轮 LLM（−2~15s）换来更长输出（+8~14s），中位数基本持平，落在 20~35s 目标内。最坏情况仍由既有 `llm_timeout_seconds=120` + `llm_max_output_tokens=4096`（最坏80~100s）兜住，无需新增熔断。

唯一新增的串行等待是 Core ticker。`core_timeout_seconds=10` 对交互式对话偏长，所以证据抓取内部再加一层 6s 软超时，超时即降级为 `market.error`，不让一个抖动的行情接口给整轮凭空加10秒——这是本次唯一新增的超时数值（遗留问题③）。

## 9. 相关文件清单（全部绝对路径）

**要改的实现文件**
- `app/agents/runner.py`（`_summarize` 57-59；两处调用点377/428；新增 `_EVIDENCE_TOOLS`/`_DEEP_ANALYSIS_TRIGGER` 与激活逻辑。**76-111 与 316-331 必须 0 改动**）
- `app/container.py`（`_OUTPUT_STYLE_GUIDE` 105-114；新增 `_DEEP_ANALYSIS_GUIDE`；`_TOOL_GUIDE` 132-133 两句；`_DEFAULT_PROMPT` 169；容器接线 490-520。**PERSONA_LOCK 75-89 与 `_TRADING_RULES` 156-167 不动**）
- `app/services/investment_expert_client.py`（换血）
- `app/tools/investment_expert_tools.py`（schema + 函数签名）
- `app/tools/executor.py`（137-144 分派）
- `app/config.py`（197-220 加 deprecated 注释，字段保留）
- `.env.example`（109-112 注释同步）

**新增文件**
- `app/services/market_evidence.py`
- `tests/services/test_market_evidence.py`
- `tests/test_investment_expert_wiring.py`

**要改的测试文件**
- `tests/test_agent_runner.py`（新增 T0 与 T6 用例；**5条 PR #61 边界回归测试原样保留**）
- `tests/services/test_investment_expert_client.py`（大改）
- `tests/test_investment_expert_tools.py`（中改）
- `tests/test_perf_and_interrupt.py`（删两条缓存测试）
- `tests/test_prompt_tool_guidance.py`（新增6条断言，既有16条不动）

**只读复用、一行不改（守 PR #60）**
- `app/news/local_search.py`、`app/tools/query_normalization.py`、`app/tools/tavily_client.py`、`app/tools/search_tools.py`、`tests/test_web_search.py`

**兼容 shim，保留不动**
- `app/services/minara_client.py`、`app/tools/minara_tools.py`

## 与 brief 转述不符之处（以实际代码为准）

1. **`_summarize` 的 500 字符硬截断**（`runner.py:57-59`）brief 未提及，但它是"联网了还是很水"的一级成因，也会让任何证据包设计失效。已列为 T0 优先修复。
2. **"快速小模型"在实际部署里不便宜也不一定快**：`deploy-staging.yml:74` 默认 `deepseek-v4-flash-max`（`.env.example:107` 自注"比opus-low贵"），`.env.example:112` 示例值直接是 `gpt-5.6-terra`（与主脑同一模型）。现状很可能已经是"两轮顶级模型串联"，砍掉这一跳的延迟收益比预期更大。
3. **`_OUTPUT_STYLE_GUIDE` 里已有第二段**（110-113行【工具调用合并与单轮终结原则】），改写时必须原样保留。
4. **`_DEFAULT_PROMPT` 同时喂语音三条链路**（`container.py:388`），决定了场景判断必须用"是否收到证据工具结果"而不是"话题类型"，否则语音会被误伤。
5. **`get_account_summary` 已在 `_TOOL_GUIDE` 账户防滥用段里被点名**（128-131行，对应上一个 commit `9d739bf`），改写 `_TOOL_GUIDE` 时不能碰这段。
