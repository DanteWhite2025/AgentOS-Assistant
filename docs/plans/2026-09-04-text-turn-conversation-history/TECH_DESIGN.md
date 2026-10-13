# 技术设计：文本回合多轮上下文

## 1 背景：一次真机串人事故

同事真机反馈（2026-09-04）：

1. 问「leopold的基金现在怎么样了」→ 正确答了 Leopold Aschenbrenner 的 Situational Awareness 基金
2. 点服务端下发的追问 chip「他一季度具体做空了哪些股票？」
3. 答案变成 **Michael Burry** 的 2021Q1 特斯拉空头 / ARK 成长 ETF / 英伟达

根因不是模型抽风，是**文本回合彻底无状态**：

```python
# app/agents/runner.py:549
messages: list[LLMMessage] = [LLMMessage("user", user_text)]
```

每轮只有当前一句。后面的 `messages.append` 全是本轮内的工具循环（`runner.py:875-993`）。

四条可能的历史来源逐一核对，没有一条能把上一轮带进来：

| 来源 | 实际情况 |
|---|---|
| `POST /messages` 入参 | `SingleTurnRequest` 只有 `session_id` / `text` / `web_search` |
| 文本 WS（主路径） | `app/api/ws.py:324` 同样只传 `text` |
| `Session` 对象 | 只有 `text_turn_results`（幂等重放缓存），不是对话历史 |
| 长期记忆 | `recall_episodes` 按 query 检索，而 episode **只在语音挂断时写**（`app/api/voice.py:132`），文字聊天一条都不写 |
| `GET/POST /sessions/{id}/history` | GET 回显审计的工具调用记录；POST 归档语音字幕时间戳。都不进 LLM |

事故有两半，都得修：

- **上半**：模型看不到上文，"他"没有指代对象，补成了做空领域词频最高的伯里。
- **下半**：`web_search=true` 的强制调用直接把 `user_text` 当检索词（`runner.py:838`），Tavily 拿到的是「他一季度具体做空了哪些股票？」，返回的证据包全是伯里的 13F。而提示词要求"必须基于本轮提供的搜索结果回答"——**只修上半，模型有了上下文却被错误证据带跑，照样答错人**。

PR #81 已先止血：把服务端下发的 chip 改成自包含（禁代词、写全名）。本设计做正解。

## 2 已定决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 历史粒度 | 只带纯文本问答 | 够解指代与"刚才说的那个观点"；工具原始数据入历史会让过期行情留在上下文里误导模型，且推高首字延迟 |
| 窗口 | 最近 3 轮 + 字符预算封顶 | 两道限制都要：光卡轮数拦不住 3 轮深度分析（每轮 800 字）；光卡字数会把一整轮切成半句 |
| 写入位置 | runner 内部一处 | 见 §4 |
| 检索词消解 | 确定性拼前缀，不调 LLM | 首字延迟实测 7~9s 已是主要瓶颈，再串一次 LLM 改写更糟 |
| 语音衔接 | 本 PR 一起做 | 用户感知的是同一件事（"刚说过的你怎么不记得"） |

## 3 数据模型

`app/runtime/session.py`：

```python
_MAX_HISTORY_TURNS = 3
_MAX_HISTORY_CHARS = 6000


@dataclass(frozen=True)
class ConversationTurn:
    user_text: str
    assistant_text: str
    created_at: datetime
```

`Session` 增加字段与不可变更新方法：

```python
turns: tuple[ConversationTurn, ...] = ()

def with_conversation_turn(self, now: datetime, turn: ConversationTurn) -> Session: ...
```

裁剪在这个方法里完成，顺序是**先按轮数、再按字符**：

1. `(*self.turns, turn)[-_MAX_HISTORY_TURNS:]`
2. 从最老的一端起丢，直到 `sum(len(user_text) + len(assistant_text))` ≤ `_MAX_HISTORY_CHARS`
3. 永远保留刚追加的那一轮——哪怕它自己就超预算。这时把它的 `assistant_text` 从尾部截到 `_MAX_HISTORY_CHARS - len(user_text)`（`user_text` 不截：它是这一轮到底问了什么，截了就没意义了），其余历史全丢。丢掉最新一轮等于这次写入静默失效

`with_conversation_turn` 同时刷新 `last_activity_at=now` 并置 `state="text_active"`，与既有的 `with_text_turn_result` 保持一致——两者都表示"这个会话刚发生了一轮文字对话"。

字段跟着 `Session` 走 Redis 序列化（`app/runtime/redis_session_store.py` 的 `_dump` / `_load`），随 `_SESSION_TTL_SECONDS = 3600` 过期。会话上下文本来就不需要活过一小时，不引入新的持久化层。

### 3.1 `assistant_text` 存什么

存 `RunResult.text`，但**入库前必须过 `SuggestionSplitter` 剥掉追问区**。

`RunResult.text` 是模型的完整输出，含 `【你可能还想问】` 与三条追问（剥离只发生在 WS 的下发路径上，`app/api/ws.py:230`）。历史里留着它，下一轮模型会把追问当正文的一部分读，甚至照着模仿——上一轮的三条追问会污染这一轮的三条追问。

## 4 写入路径：runner 内部一处

`AgentRunner.__init__` 增加 `session_store: SessionStore | None = None`（缺省 `None` 保持既有行为，仓库里大量测试直接构造 runner）。回合收尾时做 read-modify-write：

```python
latest = await self._session_store.get(session.session_id)
if latest is not None:
    await self._session_store.update(
        latest.with_conversation_turn(now, ConversationTurn(...))
    )
```

**为什么不放在两条 API 入口各写一次**：那样 REST 和 WS 都得记得写，漏一条就是静默失效——正是本次事故的失效模式。写在 runner 里，将来加第三条入口也不会漏。

runner 已经在读 `session.scope` 和长期记忆，拿会话状态不算破例；但它确实从"纯计算"变成了"会写状态"，所以 `run_turn` / `run_turn_stream` 的签名一个字都不动（保持 PR #78~#80 定下的约定）。

覆盖时机上的两个既有事实，正好让这个方案成立：

- REST 幂等重放命中缓存时**根本不进** `run_turn`（`app/api/agent.py:2185` 直接 return），天然不会重复写
- REST 写 `with_text_turn_result` 之前会 re-fetch（`agent.py:2252`），不会拿旧快照冲掉 runner 刚写的 turns

不写入的情况：

- 空文本回合（超时且一个字都没吐出来）——历史里放一条空 assistant 消息会让模型以为自己上一轮沉默了
- `session_store` 未注入（测试构造的 runner）

**超时但吐了半句要写**：用户看得到那半句，下一轮如果问"刚才那句没说完的"，历史里必须有它。

## 5 组装与防污染

`runner.py:549` 改为：

```python
messages: list[LLMMessage] = [
    *_history_messages(session.turns),
    LLMMessage("user", user_text),
]
```

`_history_messages` 是 `runner.py` 里的模块级纯函数（`tuple[ConversationTurn, ...] -> list[LLMMessage]`），把每轮摊成 `user` / `assistant` 两条 `LLMMessage`（不带 `tool_calls`，见 §2 的粒度决策）。放在 runner 里而不是 `session.py`：`LLMMessage` 属于 LLM 层，领域模型不该知道它的存在。

system prompt 追加一段约束（拼在 `_TEXT_PROMPT` 之后，只进文本链路）：

> 历史仅用于理解本轮的指代与省略（"他""该基金""这只"指的是谁）。严禁把历史里的价格、涨跌幅、持仓、资金费率当作当前数据回答——那些数字属于当时那一轮，本轮必须重新调工具取。

不加这句，模型会拿三轮前的旧价格当现价报出来。这类错误比串人更难被发现：数字看着合理，只是过期了。

## 6 检索词锚定（事故的下半）

`runner.py:838` 的强制 `web_search`：

```python
search_args = {"query": _anchor_query(user_text, session.turns), ...}
```

`_anchor_query` 的规则：本轮文本命中指代词、且有历史时，把上一轮的 `user_text` 拼在前面作主题锚：

```
"leopold的基金现在怎么样了 他一季度具体做空了哪些股票"
```

Tavily 是语义检索，前缀足以把主题钉住。没命中指代词就原样返回——换话题时拼上一轮反而是污染。

指代词规则从 `evaluations/answer_shape.py`（PR #81 加的）挪进 `app/runtime/coreference.py` 作为生产代码，评测侧改成 import 它。**一份规则两处用**：各写一份的话，改歪一个字，两边判定就不一致，而且不会报错。

这一节是启发式，不追求完美消解——它要挡住的是"检索词里一个实体名都没有"这种必然出错的情况。

## 7 WS stale session 修正（不做则 §4 失效）

`app/api/ws.py` 长连接期间用的是握手那一刻的 `session` 快照（`ws.py:103`），每轮结束 `await container.session_store.update(session.touch(now, state="text_active"))`（`ws.py:526`）。Redis 的 `update` 是整体覆盖、无 CAS。

所以第二轮结束时，这行会用握手时的旧快照把 runner 刚写的 turns 冲掉——**历史永远只剩最后一轮，而且不报错**。

改为 re-fetch 后再写。

只改这一处：`session` 是外层闭包变量（`ws.py:103`），`_handle_turn` 里没有 `nonlocal`，所以每轮拿到的都是握手快照；而 `now` 是 `_handle_turn` 的入参、每轮在 `ws.py:577` 重新取，本身是对的——不需要动。

这个隐患目前看不出来，只是因为 `Session` 上还没有需要跨轮累积的字段。

## 8 语音衔接

`ContextBuilder.build_voice_context` 增加可选参数：

```python
async def build_voice_context(
    self, user_id: str | None, limit: int = 3, turns: tuple[ConversationTurn, ...] = ()
) -> str:
```

有 `turns` 时多拼一块 `<recent_context>` 内的「本次会话刚聊过」。

三个调用点里只改两个：

- `agent.py:1131`、`agent.py:2053` —— 有既存 session，传 `session.turns`
- `agent.py:746` —— **不传**。那是新建会话领票，`Session` 对象还没构造，历史必然是空的

签名只是加一个带缺省值的关键字参数，既有调用不受影响。

## 9 测试策略

| 用例 | 卡住什么 |
|---|---|
| 第二轮 `messages` 里带着第一轮问答（fake LLM 断言） | 核心行为 |
| 追问区不进历史 | §3.1，否则上一轮的 chips 污染这一轮 |
| 裁剪：超轮数、超字符、两者同时触发 | §3 的裁剪顺序 |
| 单轮自身超预算时截断而非丢弃 | 丢掉最新一轮 = 静默失效 |
| **WS 长连接第 2、3 轮历史不被冲掉** | §7。本方案唯一的失效点，必须有回归测试 |
| 超时吐了半句 → 进历史；一个字没吐 → 不进 | §4 |
| REST 幂等重放不产生重复轮次 | §4 |
| 检索词：带指代词时含上一轮，不带时原样 | §6 |
| 语音接通带上文字历史；新建会话领票不带 | §8 |
| 未注入 `session_store` 的 runner 行为不变 | 向后兼容，仓库里大量测试这么构造 |

评测侧（`evaluations/`）不改判定逻辑，只让 `answer_shape` 复用挪过去的 `coreference` 模块。

## 10 不做什么

- **不带工具结果进历史**（§2）。想省一次工具往返的收益，抵不上过期行情留在上下文里的误导。
- **不做 LLM query 改写**（§2）。首字延迟已经是主要瓶颈。
- **不上 PG history store**。会话上下文只需活 1 小时，Session TTL 已经覆盖。
- **不动 `GET /history` 端点**。它现在回显审计记录，改成回显对话是另一件事（要考虑分页、语音字幕合流），本 PR 不碰。
- **不做换话题时清历史**。3 轮窗口 + §5 的防污染约束已经够；检测"换话题"本身要么靠 LLM（拖延迟）要么靠关键词（不准）。

## 11 风险

| 风险 | 处置 |
|---|---|
| 每轮 prompt 变长，首字延迟上升 | 3 轮 + 6000 字符封顶；纯文本不带工具结果。实测后如仍明显，先降 `_MAX_HISTORY_CHARS` |
| runner 多一次 Redis 读写 | 回合收尾时做，不在首字延迟路径上 |
| 历史被当成事实依据（旧价格当现价） | §5 的 prompt 约束 + 评测里加一条"隔轮问价格必须重新调工具"的测题 |
| §6 的启发式误伤 | 只在命中指代词时才拼；正则带负向断言（"其**他**""应**该**"不算），PR #81 已有正反例测试 |
| WS re-fetch 失败（会话已过期） | 拿不到就跳过写入，不阻断回合——用户宁可丢一轮历史，不要一个报错 |
