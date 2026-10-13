# evaluations

Agent 质量评测集。分两层，因为「有没有变聪明」和「结构有没有坏」是两个难度完全不同的问题。

## 结构层 —— 确定性，进 CI

`tests/test_evaluations.py`，用 `FunctionModel` 离线跑，不需要 key。

检查发给模型的消息结构是否合法。这些不变量与用哪个模型、什么提示词都无关，坏掉就一定是 bug：

| 不变量 | 坏掉的后果 |
|---|---|
| ModelRequest / ModelResponse **交替** | 落到 OpenAI wire 上是连续两条 `role:"user"`，部分兼容网关直接 400 |
| 每个 `ToolCallPart` 有配对的 `ToolReturnPart` | 孤儿任一侧都会让 provider 拒掉整个请求 |
| 工具结果**不出现在** `UserPromptPart` 里 | 模型看不到「是我自己调的工具」，多轮工具时无法归因 |

判定逻辑在 `prompt_shape.py`，可以单独复用。

```bash
uv run pytest tests/test_evaluations.py -q
```

先做结构层是刻意的：换模型、改提示词之后「有没有变聪明」很难客观衡量，但「消息结构有没有坏掉」是能钉死的，而后者恰恰是回答质量问题的一大来源 —— 改造前工具结果被伪装成用户消息、assistant 回合整个丢失，模型因此不知道自己调过工具。

## 质量层 —— 需要真实 key，手动跑

`run_baseline.py`。同一批测题在改动前后各跑一次，把工具调用序列和最终回复落成 markdown 供人工对比。**会产生费用，不进 CI。**

```bash
# 基线分支上跑一次
AGENT_PROVIDER_MODE=real uv run python -m evaluations.run_baseline --label before

# 改动后再跑
AGENT_PROVIDER_MODE=real uv run python -m evaluations.run_baseline --label after

diff evaluations/reports/before.md evaluations/reports/after.md
```

只跑其中几道：`--only market_price,small_talk`

模型与 key 走既有配置（`AGENT_LLM_*`），不另开一套。报告默认写到 `evaluations/reports/`（已在 gitignore 里）。

### 本地跑需要绕开语音必填项

`provider_mode=real` 会校验一整套语音配置，而文本评测根本不碰语音。本地没有那些 secret 时，把语音渠道切到只需要一个 key 的 `gemini_live`：

```bash
AGENT_REALTIME_VOICE_PROVIDER=gemini_live \
AGENT_RT_WS_BASE=wss://agent-staging.agentos-app.app \
uv run python -m evaluations.run_baseline --label after
```

（`AGENT_RT_WS_BASE` 只是过 real 模式的格式校验，评测本身不连它。）

在 staging 服务器上跑则不需要这两行——那边配置是齐的。

## 测题集

`cases.py`，七道题覆盖 runner 里几条差异很大的路径：

| 测题 | 覆盖 |
|---|---|
| `market_price` / `portfolio` | 单工具查询，最短路径 |
| `web_search_news` | 强制联网路径（运行时替模型执行搜索再回灌） |
| `multi_step_analysis` | 多步工具，验证多轮交替结构不塌 |
| `parallel_account_query` | 并发只读工具，同轮多个调用应合成一个 assistant 回合 |
| `small_talk` / `concept_question` | 工具全开也不该调用任何一个 |

加测题就往 `CASES` 里加一条 —— 结构层测试是参数化的，会自动覆盖到。

`expect_tool_calls` 是「期望至少用到」，不要求顺序也不要求穷尽：评测要发现的是「该调的没调」「不该调的乱调」，不是把模型钉死在某条路径上。
