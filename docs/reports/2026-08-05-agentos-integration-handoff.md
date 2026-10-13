# AgentOS 联调 —— 给 Core 侧的配合清单（2026-08-05）

## 现状：真链路已端到端跑通 ✅
Agent runtime → AgentOS Core，真实 **Hyperliquid TESTNET**：
- 只读：`balances` / `positions` / `orders` / `agent-trading-accounts` /
  `strategy-bindings` ✅
- 建草稿（AGENT token）→ `PENDING_CONFIRMATION` + `risk_decision_id` ✅
- 用户确认（USER token）→ 生成回链订单（`ord_...`，PENDING 挂单）✅

测试账户：unified `uac_ff5a5ff0f4fb674d079f8e5f1e2fb5d7` / venue `vact_0094395c183fae6ba7b2512bb4ef2afd` / agent-trading-account `atac_e0dba1c561968a94df98304cc49ef3a6`（agent-test-001，user `usr_testnet_e2e_20260803`）。

---

## 一、需要你现在配合的（清理，阻塞账户健康）
1. **撤掉测试网挂单 `ord_cb7ecd9857fb7398f8f84338bde61329`**
   - BTC BUY 0.0002 @ 50000（远低市价、只挂不成交），是 confirm E2E 生成的。
   - 我方 API **没有订单级撤单端点**（草稿级 cancel 对已确认草稿返回 409），撤不了。
   - 之前账户被 `UNWIND_REQUIRED` 锁定就是遗留挂单/拒单导致，麻烦撤掉防再锁。

## 二、下个里程碑「自动化执行 request-execution」需要你提供
2. 为 user `usr_testnet_e2e_20260803` / agent `agent-test-001` **创建 `AuthorizationGrant` + `ExecutionMandate`**，给我 **`mandate_id`**。
3. 确认 **`POST /internal/v1/trade-drafts/{id}/request-execution`** 的调用契约：请求体字段、鉴权（AGENT 还是 USER token）、mandate 命中/超限的返回。

## 三、请你确认的契约问题
4. **订单级撤单**：runtime 侧到底有没有撤单/撤挂单端点？（权威 OpenAPI 里没有）测试后我方该如何清理挂单？
5. **权限校验缺口（安全）**：这次 confirm 用的 USER token **没有 `trade:confirm` scope**（只有 `trade:draft` 等），但 confirm **照样成功**了。服务端是否应该对 confirm 强校验 `trade:confirm`？是有意还是漏了？
6. **token 续期**：现在的 AGENT / USER token 有效期到 **~2026-09-03**；过期后请按**同配方**续发：
   - AGENT：`actor_type=AGENT`、`iss=yesup-staging-identity` 或 `identity-staging.agentos-app.app`、`scopes` 含 `trade:draft`、**用 staging Core 的 HS256 签名密钥**（关键：claim 对但密钥错=401）。
   - USER：`actor_type=USER`、同 `user_id`、`scopes` 含 `trade:confirm`（本次那张缺这个 scope，建议补）。

---

## 四、不需要你配合、我这边自助补的（仅告知）
用现有 token 即可，无外部依赖：
- 撤草稿测试（**confirm 之前**撤，无订单副作用）
- `account-summary` / `agent-trading-accounts` 只读测试 + client 方法
- 策略 Draft 不再从账户列表选择第一个 ACTIVE 账户；先读取 Core
  `StrategyBinding`，再按 `agent_trading_account_id` 精确匹配，并透传
  `strategy_id + mandate_id`。Core 仍执行最终归属、授权和风险检查。

---

一句话：**目前只卡「① 撤掉那张挂单」**（清理）；要往「自动化执行」推进则需 **② mandate_id + request-execution 契约**。其余是确认项与我方自助项。
