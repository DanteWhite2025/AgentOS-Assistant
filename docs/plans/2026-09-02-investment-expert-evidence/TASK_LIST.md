# 任务清单：投研深度分析链路重构（TDD）

关联文件：[PRD.md](./PRD.md) · [TECH_DESIGN.md](./TECH_DESIGN.md)

## T0｜修复工具结果 500 字符截断【可独立合并、可独立见效、建议第一个上线】

- **RED** — `tests/test_agent_runner.py` 新增：
  - `test_tool_result_is_not_truncated_to_500_chars`：stub executor 返回 5 条×500字的 Tavily 形状结果，用 `_CapturingLLM` 抓 `messages`，断言 `tool` 消息长度 > 2000 且包含最后一条 result 的标题。
  - `test_oversized_tool_result_is_truncated_with_explicit_marker`：超限时结尾为 `…（结果已截断）`。
  - `test_summarize_prefers_evidence_text_when_present`：dict 里有 `evidence_text` 时直接用它，不做 json.dumps。
  - `test_account_tool_result_uses_tighter_limit`：`get_balances` 走 1500 上限。
- **GREEN** — 改 `app/agents/runner.py:57-59` + 377行 + 428行两处调用点。
- **同步检查**：`tests/test_web_search.py`（不受影响）、`tests/test_perf_and_interrupt.py`（不受影响）、`tests/test_text_message_recovery.py`（走 build_container，需跑一遍确认）。
- **建议单独合并并在 staging 观察一天**——这一条本身就可能显著改善"联网了还是很水"，且能把它的效果与后续改动的效果分开归因。

## T1｜证据组装模块（纯新增，先不接线）

- **RED** — 新建 `tests/services/test_market_evidence.py`：
  - `test_fetches_market_and_news_concurrently`：两个 stub 各 sleep 50ms，断言总耗时 < 80ms。
  - `test_market_failure_does_not_poison_news_branch` / `test_news_failure_does_not_poison_market_branch`。
  - `test_non_crypto_symbol_goes_to_unavailable_without_breaking_batch`：`symbols=["黄金", "BTC"]` → `unavailable_symbols == ["黄金"]` 且 BTC 数据正常返回。**这条直接对应用户投诉的原始问题。**
  - `test_fake_core_client_runtime_error_degrades_to_market_error`：`FakeCoreClient` 抛 RuntimeError → `market.error` 有值、整体不抛。
  - `test_market_soft_timeout_degrades_instead_of_blocking`：core 挂起 → 6s 软超时 → `market.error`。
  - `test_empty_symbols_returns_news_only`。
  - `test_news_items_and_excerpts_respect_budget`：`max_news_items`/`max_excerpt_chars` 生效，`evidence_text` 总长在预算内。
  - `test_never_raises_even_when_all_dependencies_are_broken`：core/news 全坏 → 返回带双 error 的合法结构。
  - `test_evidence_text_contains_key_metrics`：渲染文本里出现 `资金费率`、`24h`、`持仓量` 等字样。
- **GREEN** — 实现 `app/services/market_evidence.py`。

## T2｜`InvestmentExpertClient` 换血

- **RED/改** — `tests/services/test_investment_expert_client.py`：
  - 删除 6 条 `chat` 用例（`test_chat_returns_content_from_internal_llm`、`test_chat_feeds_user_message_to_internal_llm`、`test_chat_uses_different_system_prompt_by_mode`、`test_chat_preserves_provided_chat_id`、`test_chat_includes_usage_from_llm_result`、`test_chat_defaults_usage_to_zero_when_llm_result_has_none`、`test_chat_wraps_internal_llm_failure_as_upstream_error`）。
  - 新增：`test_collect_evidence_returns_structured_bundle`、`test_collect_evidence_passes_symbols_through`、`test_collect_evidence_degrades_when_core_missing`、`test_collect_evidence_degrades_when_news_store_missing`、`test_client_does_not_take_an_llm_anymore`（断言构造签名里没有 `llm`，防止有人悄悄加回来）。
  - `test_fake_client_returns_canned_response_without_calling_llm` → 改为 `test_fake_client_returns_evidence_shaped_bundle`，并断言 fake 内容里不含结论性措辞。
- **改** — `tests/test_perf_and_interrupt.py`：删除 `test_investment_expert_redis_caching_and_hit`、`test_investment_expert_redis_fallback_on_error`（缓存随 LLM 调用一起下线）；保留 `test_investment_expert_llm_model_configuration`。**这两条删除必须在 PR 描述里单列说明，别让评审以为是漏改。**
- **GREEN** — 改 `app/services/investment_expert_client.py`。

## T3｜工具 schema + executor 分派

- **RED/改** — `tests/test_investment_expert_tools.py`：
  - `test_investment_expert_tools_defines_ask_investment_expert` 增加断言：`"mode" not in tool.parameters["properties"]`、`"symbols" in tool.parameters["properties"]`、`maxItems == 8`。
  - `test_executor_ask_investment_expert_rejects_invalid_mode` → 改名并反转语义为 `test_executor_ask_investment_expert_ignores_legacy_mode_argument`（传 `mode="超级深度"` 应正常返回证据包，不返回 error）。
  - 新增 `test_executor_rejects_oversized_symbols_list`（>8）、`test_executor_rejects_non_string_symbols`。
  - `test_ask_investment_expert_rejects_empty_message`、`test_executor_ask_investment_expert_reports_when_unconfigured`、`test_executor_ask_investment_expert_works_without_agentos_client`（守 `executor.py:134` 那条 external 豁免）——保留，断言从 `"content" in result` 改为 `result["source"] == "MARKET_EVIDENCE"`。
- **GREEN** — 改 `app/tools/investment_expert_tools.py` + `app/tools/executor.py:137-144`。

## T4｜容器接线 + 删掉独立 LLM 实例

- **RED** — 新建 `tests/test_investment_expert_wiring.py`：
  - `test_container_wires_evidence_client_without_a_second_llm`：fake 与 real 两种 Settings 下，`container.executor._investment_expert_client` 非 None 且不持有任何 LLM（断言 `not hasattr(client, "_llm")`）。
  - `test_evidence_client_shares_the_same_news_store_as_web_search`：证据包和 web_search 用同一个 news_store 实例。
- **GREEN** — 改 `app/container.py`（上移 news_store、删 490-509、改 client 构造）、`app/config.py` 加 deprecated 注释、`.env.example` 同步注释。
- **回归跑**：`tests/test_llm_gateway.py`、`tests/test_health.py`、`tests/test_text_message_recovery.py`。

## T5｜提示词四步法 + 分场景篇幅

- **RED/改** — `tests/test_prompt_tool_guidance.py` 新增：
  - `test_prompt_splits_output_length_by_scenario`：`"200~300"` 与 `"500~800"` 都在，且旧的无条件约束"篇幅严格控制在 200~300 字以内"不再存在。
  - `test_prompt_defines_the_four_step_deep_analysis_framework`：`盘面定性`/`核心导火索`/`资金与链上证据`/`后市关键观察` 四个小标题都在。
  - `test_prompt_requires_a_leading_100_char_summary`：`核心结论速览` 与 `不超过 100 字` 都在，且排在四步法四个小标题之前（用 `_DEFAULT_PROMPT.index(...)` 比较位置）。
  - `test_prompt_bans_generic_filler_phrases`：六条负面清单措辞都在。
  - `test_prompt_forbids_fabricating_data_for_unavailable_symbols`：`unavailable_symbols` 与"严禁凭记忆编造"都在。
  - `test_prompt_describes_investment_expert_as_evidence_only`：出现"只给数据、不给结论"。
  - `test_default_prompt_stays_within_length_budget`：`len(_DEFAULT_PROMPT) < 3000`。
  - **既有16条断言全部保持不变且全绿**（PERSONA_LOCK顺序、工具名存在性、六条风控红线、账户防滥用五个关键词等）。
- **GREEN** — 改 `app/container.py` 的 `_OUTPUT_STYLE_GUIDE`、新增 `_DEEP_ANALYSIS_GUIDE`、改 `_TOOL_GUIDE` 的【投研深度分析】两句、拼进 `_DEFAULT_PROMPT`。
- **回归跑**：`tests/test_persona_lock.py`。

## T6｜runner 侧深度分析激活句

- **RED** — `tests/test_agent_runner.py` 新增：
  - `test_evidence_tool_result_activates_deep_analysis_instruction`：`_CapturingLLM` 记录每次 `system`，断言第1次不含激活句、第2次含。
  - `test_forced_web_search_also_activates_deep_analysis_instruction`：`web_search=True` 强制路径同样激活。
  - `test_plain_balance_query_does_not_activate_deep_analysis`：只调 `get_positions` 时 system 不含激活句（守住200~300字档不被污染）。
  - `test_deep_analysis_instruction_is_appended_only_once`：多轮循环里只拼一次。
- **GREEN** — 改 `app/agents/runner.py`：加 `_EVIDENCE_TOOLS`/`_DEEP_ANALYSIS_TRIGGER` 常量 + `deep_analysis_activated` 标志 + 两处触发点。
- **硬约束**：本任务的 diff 中，76-111行与316-331行必须为0改动。

## T7｜观测与人工验收

- 在证据抓取处加一条 `logger.info("market_evidence symbols=%s market_ok=%s news_count=%d elapsed_ms=%d", ...)`，用于上线后核对延迟预算，以及回答"黄金这类标的到底有没有召回到新闻"。
- 人工验收清单（跑在 staging）：
  1. 原始投诉问题「黄金和比特币近期行情大变动的原因」——回答中具体数字 ≥ 3个？四段结构齐全？黄金部分有没有明说"暂无实时行情、依据新闻"而不是编点位？
  2. 「我的余额是多少」——仍然200~300字，没有被四步法污染。
  3. 语音链路问同一个宏观问题——回答长度正常，没有出现四段式八百字。
  4. 端到端耗时中位数落在20~35s。
  5. 负面清单六个词在回答里出现0次。

## 任务依赖与建议提交顺序

```
T0（独立，先上，观察一天）
  └─ T1 → T2 → T3 → T4     （证据链路，串行依赖）
  └─ T5 → T6               （提示词与激活，可与 T1~T4 并行开发，但必须在 T4 之后合并）
                              └─ T7（收尾）
```

T5/T6 若先于 T4 合并，会出现"提示词要求四步法、但工具还在返回150字套话"的错配窗口，比现状更糟。**合并顺序必须是 T0 → T1-T4 → T5-T6 → T7。**
