# yesup-agent-runtime

YesUP 数字人 / 语音智能体后端（Repository 3）。负责会话编排、意图路由、受控工具调用；
**不持数据库写权限、不直连交易所**，所有真实执行必过 `yesup-core` 二次确认。

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/api/agent-mobile/`](docs/api/agent-mobile/) | 智能体 ↔ 移动端接口契约（README / OpenAPI / WS 事件 / 语音时序 / 展示页） |
| [`docs/research/tech-stack-assessment.md`](docs/research/tech-stack-assessment.md) | 技术栈选型调研与冻结决策 |
| [`docs/superpowers/specs/`](docs/superpowers/specs/) | 接口设计 spec 与权衡 |
| [`docs/deploy/staging.md`](docs/deploy/staging.md) | Staging real 模式变量、部署与验证边界 |
| [`docs/deploy/followin-news.md`](docs/deploy/followin-news.md) | 新闻聚合部署说明：环境变量、`pg_trgm` 前置、已知限制与上线校验 |
| [`docs/strategy-core-binding.md`](docs/strategy-core-binding.md) | Runtime StrategyContainer 与 Core StrategyBinding 的权威边界及 Draft 流程 |

## 目录结构

```text
app/
├── api/            REST 端点（health + agent 控制面）
├── runtime/        session / turn / context / response
├── orchestration/  intent_router / planner（自研 async 状态机）
├── agents/         runner.py（通用 AgentRunner）+ profiles/（身份配置）
├── tools/          registry.py（受控工具 + 白名单二次校验）
├── voice/          Realtime GA provider（语音单独处理）
├── audit/          hashing.py（args_hash 冻结规范）
├── policies/ memory/ telemetry/
└── main.py         FastAPI app
```

## 快速开始

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"

# 跑测试
.venv/bin/python -m pytest -q

# 起服务
.venv/bin/uvicorn app.main:app --reload --port 8100
curl http://localhost:8100/healthz
```

配置从环境变量读取（前缀 `AGENT_`），见 [`.env.example`](.env.example)。

## 认证与真实语音

- 控制面接收 Core USER Bearer Token，并原样转发到 Core
  `GET /api/v1/me/actor-context`；Runtime 不解析 Core Token，也不持有 Core HMAC Secret。
- Session 只保存 Core 返回的规范化 `user_id`。不存在或跨用户访问统一返回
  `404 SESSION_NOT_FOUND`。
- `provider_mode=real` 默认由 Runtime 生成短期火山 RTC Token；App 进房后通过
  `/voice/start` 启动 AI，关闭 Session 时先调用 `StopVoiceChat`。AppKey、AK、SK 永不下发。
- OpenAI Realtime 仅在 `AGENT_REALTIME_VOICE_PROVIDER=openai` 时启用，不再是启动依赖；
  任一所选 Provider 的关键配置缺失都会 Fail Fast，不会回退 Fake。
- `/healthz` 仅是 liveness；`/readyz` 也不代表 Flutter 真机语音闭环已经验证。

## 现状

控制面 Session、RTC Token、Start/StopVoiceChat、文本消息、历史和工具旁挂已有实现；
`args_hash`、工具注册表、Session 所有权及白名单二次校验有离线测试覆盖。只有配置最小权限
火山子用户 AK/SK，并完成 Staging + Flutter 真机验证后，才能称为真实语音闭环。

## 策略交易边界

Runtime 的 `StrategyContainer` 只负责策略模板、运行状态、展示和本地预检，不是资金与
授权事实来源。创建策略 Trade Draft 前，Runtime 必须使用自身的最小权限 AGENT Token
读取 Core `GET /internal/v1/strategy-bindings/{strategy_id}`，再按返回的
`agent_trading_account_id` 精确匹配账户，并将同一绑定中的 `strategy_id`、`mandate_id`、
`unified_account_id`、`venue_account_id` 发送给 Core。Runtime 不选择“第一个 ACTIVE 账户”，
不接受客户端自报账户关系；Core Mandate、AuthorizationGrant 与 Risk Engine 保留最终否决权。

## 新闻聚合

数据来自 Followin MCP（`app/news/followin_client.py`），后台采集循环
（`app/news/collector.py`）按固定间隔拉取、清洗、打标签后写入本地库；对外只暴露一个
只读查询接口 `GET /v1/agent/news`（标签过滤 / 关键词搜索 / 游标分页），不透传上游、不留
Grok 时代的适配层。部署所需的环境变量、`pg_trgm` 前置条件、已知限制（首日纵深有限、
`twitter`/`telegram` 源不入库、中文短关键词退化为顺序扫描）与上线校验记录见
[`docs/deploy/followin-news.md`](docs/deploy/followin-news.md)。
