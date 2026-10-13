# 文本回合多轮上下文 · 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让文本回合看得到前几轮对话，从根上消掉「点追问 chip 答案讲了别人」这类指代失败。

**Architecture:** 历史挂在 `Session` 上（随 Redis TTL 过期，不引入新存储层），只存纯文本问答，最近 3 轮 + 6000 字符双封顶。写入收敛在 `AgentRunner` 内部一处（REST / WS 两条入口自动都有，加第三条也不会漏），组装时摊成 user/assistant 交替的 `LLMMessage`。附带修掉两处：`web_search` 强制调用的检索词带指代时锚定上一轮主题；WS 长连接用握手快照写回 Session 会把历史静默冲掉。

**Tech Stack:** Python 3.12 / FastAPI / pytest + pytest-asyncio / Redis（`redis.asyncio`）/ ruff / mypy

**Spec:** [TECH_DESIGN.md](./TECH_DESIGN.md)

## Global Constraints

- `run_turn` / `run_turn_stream` 的签名一个字不动（PR #78~#80 定下的约定：REST 的幂等与任务复用机制依赖"一次 turn 有且只有一个可缓存的完整值"）。
- `AgentRunner.__init__` 新增参数必须带缺省值：仓库里大量测试直接 `AgentRunner(profile, executor, FakeLLM(), audit)` 构造，缺省缺失会一次性红掉几十个用例。
- 所有 `async for` 消费生成器的地方必须 `aclosing` 包住：提前退出**不会**自动关闭内层生成器，本项目在 P1 阶段同一类问题犯过四次，全靠测试抓到。
- 被 LLM 调用路径上的代码永不抛异常（`tests/test_investment_expert_tools.py` 文件头写死的约定）：抛异常会崩掉整个 turn / websocket。历史读写失败一律降级为"这轮没历史"，绝不阻断回合。
- 常量精确值：`_MAX_HISTORY_TURNS = 3`、`_MAX_HISTORY_CHARS = 6000`。
- CI 的确切命令（本地必须按这三条验证，不要用自己顺手的变体）：
  ```
  python -m pytest -v --cov=app.tools.agentos --cov=app.strategy --cov=app.api.strategy --cov-branch --cov-fail-under=100
  ruff check .
  mypy app
  ```
- 提交信息格式 `<type>: <description>`，中文描述，不加 Co-authored-by。

---

### Task 1: `ConversationTurn` 与 `Session.turns` 裁剪

**Files:**
- Modify: `app/runtime/session.py`（在 `TextTurnResult` 之后加 `ConversationTurn`；`Session` 加字段与方法）
- Test: `tests/test_session_history.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces:
  - `ConversationTurn(user_text: str, assistant_text: str, created_at: datetime)` — frozen dataclass
  - `Session.turns: tuple[ConversationTurn, ...] = ()`
  - `Session.with_conversation_turn(now: datetime, turn: ConversationTurn) -> Session`
  - 模块常量 `_MAX_HISTORY_TURNS = 3`、`_MAX_HISTORY_CHARS = 6000`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_session_history.py`：

```python
"""Session 上的对话历史与裁剪规则。

历史是修「点追问 chip 答案讲了别人」的地基（见 TECH_DESIGN.md §1）。裁剪必须
两道限制都有：光卡轮数拦不住 3 轮深度分析（每轮 800 字）把首字延迟拉高，光卡
字数会把一整轮切成半句。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.runtime.session import ConversationTurn, Scope, Session


def _now(minute: int = 0) -> datetime:
    return datetime(2026, 9, 4, 12, minute, 0, tzinfo=UTC)


def _session() -> Session:
    return Session(
        session_id="sess_1",
        user_id="u1",
        modality="text",
        scope=Scope(agents=("avatar",), tools=()),
        state="created",
        created_at=_now(),
        last_activity_at=_now(),
    )


def _turn(index: int, assistant_chars: int = 10) -> ConversationTurn:
    return ConversationTurn(
        user_text=f"问题{index}",
        assistant_text="答" * assistant_chars,
        created_at=_now(index),
    )


def test_new_session_has_no_history() -> None:
    assert _session().turns == ()


def test_appending_a_turn_returns_a_new_session() -> None:
    """不可变：原对象不能被改。"""

    original = _session()
    updated = original.with_conversation_turn(_now(1), _turn(1))

    assert original.turns == ()
    assert len(updated.turns) == 1
    assert updated.turns[0].user_text == "问题1"


def test_appending_marks_the_session_text_active() -> None:
    """与既有的 with_text_turn_result 对齐：两者都表示刚发生了一轮文字对话。"""

    updated = _session().with_conversation_turn(_now(1), _turn(1))

    assert updated.state == "text_active"
    assert updated.last_activity_at == _now(1)


def test_history_keeps_only_the_most_recent_turns() -> None:
    session = _session()
    for i in range(1, 6):
        session = session.with_conversation_turn(_now(i), _turn(i))

    assert [t.user_text for t in session.turns] == ["问题3", "问题4", "问题5"]


def test_history_drops_oldest_turns_over_the_char_budget() -> None:
    """三轮没超轮数上限，但合计超了字符预算——从最老的一端丢。"""

    session = _session()
    for i in range(1, 4):
        session = session.with_conversation_turn(_now(i), _turn(i, assistant_chars=2500))

    # 每轮约 2503 字（含 user_text），3 轮 7509 > 6000，最老那轮被丢掉
    assert [t.user_text for t in session.turns] == ["问题2", "问题3"]


def test_the_newest_turn_survives_even_when_it_alone_exceeds_the_budget() -> None:
    """丢掉刚追加的那一轮等于这次写入静默失效——只能截断它的 assistant_text。

    user_text 不截：它是这一轮到底问了什么，截了就没意义了。
    """

    session = _session().with_conversation_turn(
        _now(1), ConversationTurn("问题1", "答" * 9000, _now(1))
    )

    assert len(session.turns) == 1
    kept = session.turns[0]
    assert kept.user_text == "问题1"
    assert len(kept.assistant_text) == 6000 - len("问题1")


def test_oversized_newest_turn_evicts_all_older_history() -> None:
    session = _session().with_conversation_turn(_now(1), _turn(1))
    session = session.with_conversation_turn(
        _now(2), ConversationTurn("问题2", "答" * 9000, _now(2))
    )

    assert [t.user_text for t in session.turns] == ["问题2"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_session_history.py -q`
Expected: FAIL — `ImportError: cannot import name 'ConversationTurn' from 'app.runtime.session'`

- [ ] **Step 3: 写实现**

在 `app/runtime/session.py` 的 `TextTurnResult` 之后插入：

```python
# 历史窗口。两道限制都要：光卡轮数拦不住 3 轮深度分析（每轮 500~800 字）把
# prompt 撑大、首字延迟拉高；光卡字数又会把一整轮切成半句，模型读到的是
# 一句没头没尾的话。
_MAX_HISTORY_TURNS = 3
_MAX_HISTORY_CHARS = 6000


@dataclass(frozen=True)
class ConversationTurn:
    """一轮已完成的文字问答。

    只存纯文本，不存工具调用与结果：过期的行情数字留在上下文里会被模型当成
    当前数据用（见 TECH_DESIGN.md §2），而那种错比串人更难发现——数字看着
    合理，只是过期了。
    """

    user_text: str
    assistant_text: str
    created_at: datetime
```

在 `Session` 的 `text_turn_results` 字段后加：

```python
    # 最近几轮的文字问答，供下一轮理解指代（"他""该基金""这只"指的是谁）。
    turns: tuple[ConversationTurn, ...] = ()
```

在 `with_text_turn_result` 之后加方法：

```python
    def with_conversation_turn(self, now: datetime, turn: ConversationTurn) -> Session:
        """追加一轮历史并按窗口裁剪。

        裁剪顺序是先轮数、再字符。刚追加的那一轮永远保留——丢掉它等于这次
        写入静默失效，而调用方拿不到任何信号。
        """

        recent = (*self.turns, turn)[-_MAX_HISTORY_TURNS:]
        newest = recent[-1]
        newest_chars = len(newest.user_text) + len(newest.assistant_text)
        if newest_chars > _MAX_HISTORY_CHARS:
            # 最新一轮自己就超预算：截它的答案（问题不截，那是"到底问了什么"），
            # 更老的历史全部让位。
            budget = max(_MAX_HISTORY_CHARS - len(newest.user_text), 0)
            recent = (replace(newest, assistant_text=newest.assistant_text[:budget]),)
        else:
            total = sum(len(t.user_text) + len(t.assistant_text) for t in recent)
            while len(recent) > 1 and total > _MAX_HISTORY_CHARS:
                dropped, recent = recent[0], recent[1:]
                total -= len(dropped.user_text) + len(dropped.assistant_text)
        return replace(
            self,
            state="text_active",
            last_activity_at=now,
            turns=recent,
        )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_session_history.py -q`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add app/runtime/session.py tests/test_session_history.py
git commit -m "feat(session): 对话历史值对象与窗口裁剪"
```

---

### Task 2: `ConversationTurn` 的 Redis 序列化

**Files:**
- Modify: `app/runtime/redis_session_store.py`（`_dump` 加一个键，`_load` 加一段还原）
- Test: `tests/test_session_history.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `ConversationTurn`、`Session.turns`
- Produces: JSON 键 `"turns"`，每项 `{"user_text", "assistant_text", "created_at"}`

Session 走 Redis 时是整体 JSON `set`（`redis_session_store.py:396`），新字段不加进 `_dump` / `_load` 就会在每次读写间静默丢掉——本地 `InMemorySessionStore` 测试全绿、staging 上历史永远是空的。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_session_history.py`：

```python
def test_history_survives_a_redis_dump_load_round_trip() -> None:
    """Session 走 Redis 是整体 JSON set/get。新字段漏进序列化的话，本地内存
    store 测试全绿，staging 上历史永远是空的——而且不报错。
    """

    from app.runtime.redis_session_store import RedisSessionStore

    session = _session().with_conversation_turn(_now(1), _turn(1))
    session = session.with_conversation_turn(_now(2), _turn(2))

    restored = RedisSessionStore._load(RedisSessionStore._dump(session))

    assert [t.user_text for t in restored.turns] == ["问题1", "问题2"]
    assert restored.turns[0].assistant_text == "答" * 10
    assert restored.turns[1].created_at == _now(2)


def test_loading_a_payload_without_turns_yields_empty_history() -> None:
    """已经在 Redis 里的旧会话没有这个键，不能因此崩掉。"""

    import json

    from app.runtime.redis_session_store import RedisSessionStore

    raw = json.loads(RedisSessionStore._dump(_session()))
    raw.pop("turns", None)

    assert RedisSessionStore._load(json.dumps(raw)).turns == ()


def test_loading_skips_malformed_history_entries() -> None:
    """照 text_turn_results 的既有防御模式：字段不全的条目跳过，不抛。"""

    import json

    from app.runtime.redis_session_store import RedisSessionStore

    raw = json.loads(RedisSessionStore._dump(_session().with_conversation_turn(_now(1), _turn(1))))
    raw["turns"].append({"user_text": "缺字段"})
    raw["turns"].append("不是对象")

    restored = RedisSessionStore._load(json.dumps(raw))

    assert [t.user_text for t in restored.turns] == ["问题1"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_session_history.py -q -k redis or malformed or without_turns`
Expected: FAIL — `restored.turns == ()`，序列化没带上

- [ ] **Step 3: 写实现**

`_dump` 的返回字典里，`"text_turn_results"` 之后加：

```python
                "turns": [
                    {
                        "user_text": item.user_text,
                        "assistant_text": item.assistant_text,
                        "created_at": item.created_at.isoformat(),
                    }
                    for item in session.turns
                ],
```

`_load` 的 `Session(...)` 里，`text_turn_results=...` 之后加（照该文件既有的防御模式，字段不全或不是 dict 的条目跳过）：

```python
            turns=tuple(
                ConversationTurn(
                    user_text=str(item["user_text"]),
                    assistant_text=str(item["assistant_text"]),
                    created_at=datetime.fromisoformat(str(item["created_at"])),
                )
                for item in data.get("turns", [])
                if isinstance(item, dict)
                and all(name in item for name in ("user_text", "assistant_text", "created_at"))
            ),
```

文件头的 import 改为：

```python
from app.runtime.session import ConversationTurn, Scope, Session, TextTurnResult
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_session_history.py -q`
Expected: 10 passed

- [ ] **Step 5: 提交**

```bash
git add app/runtime/redis_session_store.py tests/test_session_history.py
git commit -m "feat(session): 对话历史进 Redis 序列化"
```

---

### Task 3: 历史组装进 `messages`

**Files:**
- Modify: `app/agents/runner.py:549`（`messages` 初始化）；同文件加模块级函数 `_history_messages`
- Test: `tests/test_agent_runner_history.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `ConversationTurn`、`Session.turns`
- Produces: `_history_messages(turns: tuple[ConversationTurn, ...]) -> list[LLMMessage]`

放在 `runner.py` 而不是 `session.py`：`LLMMessage` 属于 LLM 层，领域模型不该知道它的存在。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_agent_runner_history.py`：

```python
"""回合把历史带给模型。

真机事故（TECH_DESIGN.md §1）：问完 Leopold Aschenbrenner 的基金，点追问 chip
「他一季度具体做空了哪些股票？」，答案讲的是 Michael Burry。根因就是这里——
每轮只把当前一句喂给模型，"他"没有指代对象。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.agents.llm import LLMMessage, LLMResult
from app.agents.runner import AgentProfile, AgentRunner, _history_messages
from app.audit.store import InMemoryAuditStore
from app.runtime.session import ConversationTurn, Scope, Session
from app.tools.builtin import build_default_registry
from app.tools.core_client import FakeCoreClient
from app.tools.executor import ToolExecutor


def _now() -> datetime:
    return datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


class _RecordingLLM:
    """记下每次收到的 messages，用来断言历史真的进了 prompt。"""

    def __init__(self) -> None:
        self.seen: list[tuple[LLMMessage, ...]] = []

    async def complete(
        self, system: str, messages: tuple[LLMMessage, ...], tools: tuple[str, ...]
    ) -> LLMResult:
        self.seen.append(messages)
        return LLMResult(text="好的")


def _session(turns: tuple[ConversationTurn, ...] = ()) -> Session:
    return Session(
        session_id="sess_1",
        user_id="u1",
        modality="text",
        scope=Scope(agents=("avatar",), tools=()),
        state="text_active",
        created_at=_now(),
        last_activity_at=_now(),
        turns=turns,
    )


def _runner(llm: _RecordingLLM) -> AgentRunner:
    return AgentRunner(
        AgentProfile("avatar", "fake", "你是数字人", ()),
        ToolExecutor(build_default_registry(), FakeCoreClient()),
        llm,
        InMemoryAuditStore(),
    )


def test_history_messages_alternates_user_and_assistant() -> None:
    turns = (
        ConversationTurn("leopold的基金现在怎么样了", "Leopold Aschenbrenner 的基金…", _now()),
    )

    assert [(m.role, m.content) for m in _history_messages(turns)] == [
        ("user", "leopold的基金现在怎么样了"),
        ("assistant", "Leopold Aschenbrenner 的基金…"),
    ]


def test_history_messages_carries_no_tool_calls() -> None:
    """只带纯文本（TECH_DESIGN.md §2）：过期行情留在上下文里会被当成当前数据。"""

    turns = (ConversationTurn("问", "答", _now()),)

    assert all(m.tool_calls == () and m.tool_call_id == "" for m in _history_messages(turns))


def test_empty_history_produces_no_messages() -> None:
    assert _history_messages(()) == []


async def test_turn_prepends_history_before_the_current_question() -> None:
    llm = _RecordingLLM()
    turns = (
        ConversationTurn("leopold的基金现在怎么样了", "Leopold Aschenbrenner 的基金…", _now()),
    )

    await _runner(llm).run_turn(
        _session(turns), "他一季度具体做空了哪些股票？", "trace1", _now()
    )

    roles_and_texts = [(m.role, m.content) for m in llm.seen[0]]
    assert roles_and_texts == [
        ("user", "leopold的基金现在怎么样了"),
        ("assistant", "Leopold Aschenbrenner 的基金…"),
        ("user", "他一季度具体做空了哪些股票？"),
    ]


async def test_turn_without_history_sends_only_the_current_question() -> None:
    """向后兼容：没有历史时的行为与改造前完全一致。"""

    llm = _RecordingLLM()

    await _runner(llm).run_turn(_session(), "BTC 现在多少", "trace1", _now())

    assert [(m.role, m.content) for m in llm.seen[0]] == [("user", "BTC 现在多少")]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_agent_runner_history.py -q`
Expected: FAIL — `ImportError: cannot import name '_history_messages' from 'app.agents.runner'`

- [ ] **Step 3: 写实现**

在 `app/agents/runner.py` 的 `_finish` 定义之后加：

```python
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
```

`runner.py:549` 改为：

```python
        messages: list[LLMMessage] = [
            *_history_messages(session.turns),
            LLMMessage("user", user_text),
        ]
```

文件头补 import：`from app.runtime.session import ConversationTurn, Session`（该文件已 import `Session`，加上 `ConversationTurn`）。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_agent_runner_history.py tests/test_agent_runner.py -q`
Expected: 全部 passed（既有 runner 测试不受影响——它们构造的 session 没有 turns）

- [ ] **Step 5: 提交**

```bash
git add app/agents/runner.py tests/test_agent_runner_history.py
git commit -m "feat(runner): 历史轮次组装进 LLM messages"
```

---

### Task 4: 防污染的 prompt 约束

**Files:**
- Modify: `app/container.py`（加 `_HISTORY_GUIDE`，拼进 `_TEXT_PROMPT`）
- Test: `tests/test_prompt_tool_guidance.py`（追加）

**Interfaces:**
- Consumes: PR #81 建立的 `_TEXT_PROMPT` / `_DEFAULT_PROMPT` 分工
- Produces: `_HISTORY_GUIDE` 常量；`_TEXT_PROMPT` 含它

模型会拿三轮前的旧价格当现价报出来。这类错误比串人更难发现：数字看着合理，只是过期了。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_prompt_tool_guidance.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_prompt_tool_guidance.py -q -k history`
Expected: FAIL — `assert '指代' in _TEXT_PROMPT`

- [ ] **Step 3: 写实现**

在 `app/container.py` 的 `_FOLLOWUP_GUIDE` 之后加：

```python
# 历史的定位。文本回合现在会带最近几轮问答（app/runtime/session.py 的
# Session.turns），但历史只是"上文"，不是"数据"——不写死这条，模型会拿三轮前
# 的旧价格当现价报出来。这类错误比串人更难发现：数字看着合理，只是过期了。
_HISTORY_GUIDE = (
    "【历史上下文的用法】本轮消息前面可能带有本次会话最近几轮的问答。"
    "它们仅用于理解本轮的指代与省略（判断「他」「她」「该基金」「这只」「上述」指的是谁、"
    "以及用户省略了什么主语）。"
    "严禁把历史里的价格、涨跌幅、持仓、资金费率、持仓量当作当前数据回答——"
    "那些数字属于当时那一轮，本轮要用就必须重新调工具取。"
    "历史与本轮工具结果冲突时，一律以本轮工具结果为准。"
)
```

`_TEXT_PROMPT` 改为：

```python
_TEXT_PROMPT = _DEFAULT_PROMPT + _FOLLOWUP_GUIDE + _HISTORY_GUIDE
```

同时把 `test_text_prompt_stays_within_its_own_length_budget` 的上限从 4400 提到 4800，并在注释里写明为什么涨了（多了 `_HISTORY_GUIDE` 约 170 字）。语音那份 `_DEFAULT_PROMPT` 不变。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_prompt_tool_guidance.py tests/test_persona_lock.py -q`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add app/container.py tests/test_prompt_tool_guidance.py
git commit -m "feat(prompt): 历史只用于理解指代，不得当作当前数据"
```

---

### Task 5: runner 内单点写入历史

**Files:**
- Modify: `app/agents/runner.py`（`__init__` 加 `session_store`；`run_turn_stream` 外再套一层持久化）
- Modify: `app/container.py`（装配时把 `session_store` 传给 `AgentRunner`）
- Test: `tests/test_agent_runner_history.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `with_conversation_turn`；Task 3 的组装
- Produces: `AgentRunner.__init__(..., session_store: SessionStore | None = None)`

写在 runner 里而不是两条 API 入口各写一次：那样 REST 和 WS 都得记得写，漏一条就是静默失效——正是本次事故的失效模式。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_agent_runner_history.py`：

```python
class _SlowLLM:
    """吐一句固定答案，用来验证写入的内容与时机。"""

    def __init__(self, text: str = "答案正文") -> None:
        self._text = text

    async def complete(
        self, system: str, messages: tuple[LLMMessage, ...], tools: tuple[str, ...]
    ) -> LLMResult:
        return LLMResult(text=self._text)


def _store_runner(llm: object, store: object) -> AgentRunner:
    return AgentRunner(
        AgentProfile("avatar", "fake", "你是数字人", ()),
        ToolExecutor(build_default_registry(), FakeCoreClient()),
        llm,
        InMemoryAuditStore(),
        session_store=store,
    )


async def test_turn_persists_itself_into_history() -> None:
    from app.runtime.session_store import InMemorySessionStore

    store = InMemorySessionStore()
    session = _session()
    await store.create(session)

    await _store_runner(_SlowLLM(), store).run_turn(session, "BTC 现在多少", "trace1", _now())

    saved = await store.get("sess_1")
    assert saved is not None
    assert [(t.user_text, t.assistant_text) for t in saved.turns] == [
        ("BTC 现在多少", "答案正文")
    ]


async def test_second_turn_sees_the_first_one() -> None:
    """端到端：这就是事故链路——第二轮必须看得到第一轮。"""

    from app.runtime.session_store import InMemorySessionStore

    store = InMemorySessionStore()
    session = _session()
    await store.create(session)
    llm = _RecordingLLM()
    runner = _store_runner(llm, store)

    await runner.run_turn(session, "leopold的基金现在怎么样了", "trace1", _now())
    reloaded = await store.get("sess_1")
    assert reloaded is not None
    await runner.run_turn(reloaded, "他一季度具体做空了哪些股票？", "trace2", _now())

    assert [(m.role, m.content) for m in llm.seen[1]] == [
        ("user", "leopold的基金现在怎么样了"),
        ("assistant", "好的"),
        ("user", "他一季度具体做空了哪些股票？"),
    ]


async def test_persisted_answer_has_the_followup_section_stripped() -> None:
    """追问区不能进历史（TECH_DESIGN.md §3.1）。

    留着的话下一轮模型会把上一轮的三条追问当正文读，甚至照着模仿。
    """
    from app.runtime.session_store import InMemorySessionStore
    from app.runtime.suggestion_split import MARKER

    store = InMemorySessionStore()
    session = _session()
    await store.create(session)
    answer = f"正文结论。\n\n{MARKER}\n1. 追问一\n2. 追问二\n3. 追问三"

    await _store_runner(_SlowLLM(answer), store).run_turn(session, "问一句", "trace1", _now())

    saved = await store.get("sess_1")
    assert saved is not None
    assert saved.turns[0].assistant_text == "正文结论。"
    assert MARKER not in saved.turns[0].assistant_text
    assert "追问一" not in saved.turns[0].assistant_text


async def test_empty_answer_is_not_persisted() -> None:
    """空回合进历史会让模型以为自己上一轮沉默了。"""

    from app.runtime.session_store import InMemorySessionStore

    store = InMemorySessionStore()
    session = _session()
    await store.create(session)

    await _store_runner(_SlowLLM(""), store).run_turn(session, "问一句", "trace1", _now())

    saved = await store.get("sess_1")
    assert saved is not None
    assert saved.turns == ()


async def test_runner_without_a_store_behaves_exactly_as_before() -> None:
    """向后兼容：仓库里大量测试直接构造 runner，不传 store。"""

    result = await _runner(_RecordingLLM()).run_turn(_session(), "问一句", "trace1", _now())

    assert result.text == "好的"


async def test_persist_failure_does_not_break_the_turn() -> None:
    """被 LLM 调用的路径上永不抛异常——历史写失败就丢这一轮历史，不能崩掉回合。"""

    class _BrokenStore:
        async def get(self, session_id: str):
            raise RuntimeError("redis down")

        async def update(self, session) -> None:  # pragma: no cover - 到不了
            raise AssertionError("get 就炸了，不该走到这里")

    result = await _store_runner(_SlowLLM(), _BrokenStore()).run_turn(
        _session(), "问一句", "trace1", _now()
    )

    assert result.text == "答案正文"


async def test_a_vanished_session_is_not_recreated() -> None:
    """会话已过期就跳过写入，不要凭手里的旧对象把它写回去。"""

    from app.runtime.session_store import InMemorySessionStore

    store = InMemorySessionStore()  # 故意不 create

    await _store_runner(_SlowLLM(), store).run_turn(_session(), "问一句", "trace1", _now())

    assert await store.get("sess_1") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_agent_runner_history.py -q`
Expected: FAIL — `TypeError: AgentRunner.__init__() got an unexpected keyword argument 'session_store'`

- [ ] **Step 3: 写实现**

`AgentRunner.__init__` 末尾加参数与赋值：

```python
        session_store: Any = None,
    ) -> None:
        ...
        # 回合结束后把这一轮写进会话历史。缺省 None：仓库里大量测试直接构造
        # runner，也让"不带历史"保持可用。
        self._session_store = session_store
```

在 `run_turn_stream` 里，把现有的 `with_turn_timeout(...)` 结果再包一层：

```python
        return self._persisting_history(
            with_turn_timeout(
                self._run_turn_events(...),
                timeout_seconds=self._turn_timeout_seconds,
                on_timeout=_on_timeout,
                trace_id=trace_id,
            ),
            session,
            user_text,
            now,
        )
```

新增私有方法（放在 `run_turn_stream` 之后）：

```python
    async def _persisting_history(
        self,
        events: AsyncGenerator[TurnEvent, None],
        session: Session,
        user_text: str,
        now: datetime,
    ) -> AsyncGenerator[TurnEvent, None]:
        """透传事件，并在终止事件之前把这一轮写进会话历史。

        写在**yield TurnDone 之前**：消费方拿到终止事件后随时可能停止迭代，
        而 async for 提前退出会直接关掉这个生成器——写在之后就永远不执行。
        一次 Redis 往返约 1ms，放在这里不影响首字延迟（那已经发生过了）。

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
        """read-modify-write 一轮历史。任何失败都只丢历史，绝不阻断回合。"""

        if self._session_store is None:
            return
        # 追问区是给客户端渲染 chips 的，不是答案正文。留在历史里下一轮模型会
        # 把它当正文读，甚至照着模仿上一轮的三条追问。
        splitter = SuggestionSplitter()
        body = splitter.feed(answer)
        tail, _ = splitter.finish()
        body = (body + tail).strip()
        if not body:
            # 空回合（超时且一个字都没吐）进历史，会让模型以为自己上一轮沉默了。
            return
        try:
            latest = await self._session_store.get(session.session_id)
            if latest is None:
                # 会话已过期。凭手里的旧对象写回去等于把它复活，TTL 语义就乱了。
                return
            await self._session_store.update(
                latest.with_conversation_turn(
                    now, ConversationTurn(user_text, body, now)
                )
            )
        except Exception:
            logger.warning("会话历史写入失败，本轮不进历史", exc_info=True)
```

文件头补 import：`from app.runtime.suggestion_split import SuggestionSplitter`（`aclosing` 与 `ConversationTurn` 前面的任务已引入）。

`app/container.py:623` 的 `AgentRunner(...)` 装配处加 `session_store=session_store`。顺序已经是对的：`session_store` 在 522/524/560 三个分支装配完毕，runner 在 623 才构造，直接传即可，不用挪动代码。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_agent_runner_history.py tests/test_agent_runner.py tests/test_turn_timeout.py -q`
Expected: 全部 passed

- [ ] **Step 5: 补一条超时也写入的测试**

```python
async def test_a_half_finished_answer_still_lands_in_history() -> None:
    """回合超时但吐了半句：用户看得到那半句，下一轮必须知道说过什么。"""

    import asyncio

    from app.runtime.session_store import InMemorySessionStore

    class _HangingLLM:
        async def complete(self, system, messages, tools):
            await asyncio.sleep(10)
            raise AssertionError("不该跑到这里")

        async def stream(self, system, messages, tools):
            from app.agents.llm import LLMTextDelta

            yield LLMTextDelta(delta="已经说了半句")
            await asyncio.sleep(10)

    store = InMemorySessionStore()
    session = _session()
    await store.create(session)
    runner = AgentRunner(
        AgentProfile("avatar", "fake", "你是数字人", ()),
        ToolExecutor(build_default_registry(), FakeCoreClient()),
        _HangingLLM(),
        InMemoryAuditStore(),
        turn_timeout_seconds=0.05,
        session_store=store,
    )

    result = await runner.run_turn(session, "问一句", "trace1", _now())

    assert "TURN_TIMEOUT" in result.blockers
    saved = await store.get("sess_1")
    assert saved is not None
    assert saved.turns[0].assistant_text == "已经说了半句"
```

Run: `uv run pytest tests/test_agent_runner_history.py -q`
Expected: 全部 passed

- [ ] **Step 6: 提交**

```bash
git add app/agents/runner.py app/container.py tests/test_agent_runner_history.py
git commit -m "feat(runner): 回合结束把这一轮写进会话历史"
```

---

### Task 6: WS 长连接不再用握手快照冲掉历史

**Files:**
- Modify: `app/api/ws.py:526`
- Test: `tests/test_ws_history.py`（新建）

**Interfaces:**
- Consumes: Task 5 的写入
- Produces: 无（行为修正）

**这是整个方案唯一的失效点。** `session` 是外层闭包变量（`ws.py:103`，握手时取一次），`_handle_turn` 里没有 `nonlocal`。Redis 的 `update` 是整体覆盖、无 CAS，所以第二轮结束时这行会用握手快照把 runner 刚写的 turns 冲掉——历史永远只剩最后一轮，而且不报错。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_ws_history.py`。构造方式照 `tests/test_ws.py` 的既有 fixture（用 `TestClient` + `websocket_connect`，参考该文件里已有的连接与鉴权样板，不要另造一套）：

```python
"""文本 WS 的多轮历史。

WS 是文本主路径，而它长连接期间用的是握手那一刻的 session 快照。这个测试卡住
的是：连着聊三轮，历史必须一轮不丢——用旧快照写回 Session 会静默冲掉 runner
刚写的 turns（TECH_DESIGN.md §7）。
"""
```

测试主体（在同一条 websocket 连接上连发三条 `response.create`，每条等到 `response.text.done`，最后断言 store 里有 3 轮）：

```python
async def test_three_turns_on_one_connection_all_land_in_history(...) -> None:
    ...
    for text in ("第一问", "第二问", "第三问"):
        ws.send_json({"type": "response.create", "data": {"text": text}})
        _drain_until(ws, "response.text.done")

    saved = await container.session_store.get(session_id)
    assert [t.user_text for t in saved.turns] == ["第一问", "第二问", "第三问"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_ws_history.py -q`
Expected: FAIL — 只剩最后一轮（`["第三问"]`），前两轮被握手快照冲掉

- [ ] **Step 3: 写实现**

`app/api/ws.py:526` 改为：

```python
        # 必须重新取：session 是握手时拿的快照（本函数外层的闭包变量），
        # 而 Redis 的 update 是整体覆盖、无 CAS。拿旧快照写回去会把 runner
        # 这一轮刚写进去的历史静默冲掉——第二轮之后就永远只剩最后一轮。
        latest = await container.session_store.get(session_id)
        if latest is not None:
            await container.session_store.update(latest.touch(now, state="text_active"))
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_ws_history.py tests/test_ws.py tests/test_ws_suggestions.py -q`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add app/api/ws.py tests/test_ws_history.py
git commit -m "fix(ws): 每轮重取会话，别用握手快照冲掉历史"
```

---

### Task 7: 检索词锚定 + 指代词规则挪进生产代码

**Files:**
- Create: `app/runtime/coreference.py`
- Modify: `evaluations/answer_shape.py`（删掉本地正则，改 import）
- Modify: `app/agents/runner.py:838`（强制 `web_search` 的 query）
- Test: `tests/test_coreference.py`（新建，把 `tests/test_answer_shape.py` 里的正反例搬过来）
- Test: `tests/test_agent_runner_history.py`（追加锚定用例）

**Interfaces:**
- Consumes: Task 1 的 `ConversationTurn`
- Produces:
  - `has_dangling_reference(text: str) -> bool`
  - `dangling_reference_questions(questions: Sequence[str]) -> tuple[str, ...]`（从 `evaluations/answer_shape.py` 原样搬过来，签名不变）
  - `anchor_search_query(text: str, turns: tuple[ConversationTurn, ...]) -> str`

一份规则两处用。各写一份的话，改歪一个字，两边判定就不一致，而且不会报错。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_coreference.py`：

```python
"""指代词识别与检索词锚定。

事故的下半（TECH_DESIGN.md §6）：web_search 强制调用直接把 user_text 当检索词，
Tavily 拿到「他一季度具体做空了哪些股票？」，返回的证据包全是 Michael Burry 的
13F。模型有了上下文也会被错误证据带跑。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.runtime.coreference import anchor_search_query, has_dangling_reference
from app.runtime.session import ConversationTurn


def _now() -> datetime:
    return datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "text",
    [
        "他一季度具体做空了哪些股票？",
        "她的持仓有多少",
        "它的资金费率是多少",
        "他们后续会加仓吗",
        "该基金现在规模多少",
        "该公司下季度指引如何",
        "这只股票的支撑位在哪",
        "那家机构还持有多少",
        "上述标的哪个更值得关注",
        "此人还管理其他基金吗",
    ],
)
def test_dangling_references_are_detected(text: str) -> None:
    assert has_dangling_reference(text)


@pytest.mark.parametrize(
    "text",
    [
        "Leopold Aschenbrenner 一季度做空了哪些股票",
        "BTC 仓位的开仓均价是多少",
        "英伟达空头仓位有多大",
        "ARK 成长 ETF 具体是哪几只",
        "我的可用余额是多少",
        "还有其他可以关注的板块吗",
        "美联储下次议息是什么时候",
        "这个月的 CPI 数据什么时候公布",
        "我应该现在减仓吗",
    ],
)
def test_self_contained_text_is_not_flagged(text: str) -> None:
    assert not has_dangling_reference(text)


def test_anchor_prepends_the_previous_question_when_the_query_is_dangling() -> None:
    turns = (ConversationTurn("leopold的基金现在怎么样了", "……", _now()),)

    assert (
        anchor_search_query("他一季度具体做空了哪些股票？", turns)
        == "leopold的基金现在怎么样了 他一季度具体做空了哪些股票？"
    )


def test_anchor_uses_only_the_most_recent_turn() -> None:
    turns = (
        ConversationTurn("很久以前问的事", "……", _now()),
        ConversationTurn("leopold的基金现在怎么样了", "……", _now()),
    )

    anchored = anchor_search_query("他做空了哪些股票", turns)

    assert anchored.startswith("leopold的基金现在怎么样了")
    assert "很久以前" not in anchored


def test_anchor_leaves_a_self_contained_query_untouched() -> None:
    """换话题时拼上一轮反而是污染。"""

    turns = (ConversationTurn("leopold的基金现在怎么样了", "……", _now()),)

    assert anchor_search_query("BTC 现在多少", turns) == "BTC 现在多少"


def test_anchor_without_history_returns_the_query_as_is() -> None:
    assert anchor_search_query("他做空了哪些股票", ()) == "他做空了哪些股票"


def test_evaluations_reuse_the_same_rules() -> None:
    """评测侧不能自己再写一份——改歪一个字两边判定就不一致，且不会报错。"""

    from evaluations import answer_shape

    assert answer_shape.dangling_reference_questions is not None
    assert "他一季度具体做空了哪些股票" in answer_shape.dangling_reference_questions(
        ("他一季度具体做空了哪些股票",)
    )
```

追加到 `tests/test_agent_runner_history.py`：

```python
async def test_forced_web_search_anchors_a_dangling_query() -> None:
    """带指代的检索词要拼上一轮主题，否则证据包是别人的事。"""

    captured: list[dict] = []

    class _SearchExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            captured.append(args)
            return {"source": "TAVILY", "results": [{"title": "t", "content": "c"}]}

    session = Session(
        session_id="sess_1",
        user_id="u1",
        modality="text",
        scope=Scope(agents=("avatar",), tools=("web_search",)),
        state="text_active",
        created_at=_now(),
        last_activity_at=_now(),
        turns=(ConversationTurn("leopold的基金现在怎么样了", "……", _now()),),
    )
    runner = AgentRunner(
        AgentProfile("avatar", "fake", "你是数字人", ("web_search",)),
        _SearchExecutor(),
        _RecordingLLM(),
        InMemoryAuditStore(),
    )

    await runner.run_turn(
        session, "他一季度具体做空了哪些股票？", "trace1", _now(), web_search=True
    )

    assert captured[0]["query"].startswith("leopold的基金现在怎么样了")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_coreference.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.runtime.coreference'`

- [ ] **Step 3: 写实现**

新建 `app/runtime/coreference.py`。把 `evaluations/answer_shape.py` 里的 `_DANGLING_PATTERNS` 与 `dangling_reference_questions` **原样搬过来**（含注释），再加两个函数：

```python
def has_dangling_reference(text: str) -> bool:
    """这段文字里有没有指代上文的说法。"""

    return any(p.search(text) for p in _DANGLING_PATTERNS)


def anchor_search_query(text: str, turns: tuple[ConversationTurn, ...]) -> str:
    """给带指代的检索词拼上一轮的问题作主题锚。

    Tavily 是语义检索，前缀足以把主题钉住：「他一季度具体做空了哪些股票」单独
    去搜命中的是 Michael Burry 的 13F，拼上「leopold的基金现在怎么样了」之后
    主题就锚住了。

    没命中指代词就原样返回——换话题时拼上一轮反而是污染。
    """

    if not turns or not has_dangling_reference(text):
        return text
    return f"{turns[-1].user_text} {text}"
```

`evaluations/answer_shape.py` 删掉本地的 `_DANGLING_PATTERNS` 与 `dangling_reference_questions`，改为：

```python
from app.runtime.coreference import dangling_reference_questions
```

（`AnswerShape.dangling_questions` 继续调它，签名不变，`evaluations/run_baseline.py` 不用改。）

`app/agents/runner.py:838` 改为：

```python
            search_args: dict[str, Any] = {
                "query": anchor_search_query(user_text, session.turns),
                "topic": "news" if recent_query else "general",
            }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_coreference.py tests/test_answer_shape.py tests/test_agent_runner_history.py tests/test_web_search.py -q`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add app/runtime/coreference.py app/agents/runner.py evaluations/answer_shape.py tests/test_coreference.py tests/test_agent_runner_history.py
git commit -m "fix(search): 带指代的检索词锚定上一轮主题"
```

---

### Task 8: 语音接通带上文字历史

**Files:**
- Modify: `app/runtime/context_builder.py`（`build_voice_context` 加 `turns` 参数）
- Modify: `app/api/agent.py:1131`、`app/api/agent.py:2053`（传 `session.turns`）
- Test: `tests/test_context_builder.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `ConversationTurn`
- Produces: `build_voice_context(user_id, limit=3, turns=()) -> str`

`agent.py:746` **不传**：那是新建会话领票，`Session` 对象还没构造，历史必然是空的。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_context_builder.py`：

```python
async def test_voice_context_includes_the_current_session_text_turns() -> None:
    """刚打字聊完就打电话，语音那边不能什么都不知道。

    build_voice_context 原来只读 long-term episode，而文字聊天从不写 episode
    （只有语音挂断才写，app/api/voice.py），所以文字→语音一直是断片的。
    """

    from app.runtime.session import ConversationTurn

    builder = ContextBuilder(None)
    turns = (
        ConversationTurn("leopold的基金现在怎么样了", "Leopold Aschenbrenner 的基金…", _now()),
    )

    rendered = await builder.build_voice_context("u1", turns=turns)

    assert "leopold的基金现在怎么样了" in rendered
    assert "Leopold Aschenbrenner 的基金" in rendered
    assert rendered.startswith("<recent_context>")


async def test_voice_context_without_turns_is_unchanged() -> None:
    """向后兼容：不传 turns 时行为与改造前完全一致（无记忆则空串）。"""

    assert await ContextBuilder(None).build_voice_context("u1") == ""
```

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_context_builder.py -q -k voice_context`
Expected: FAIL — `TypeError: build_voice_context() got an unexpected keyword argument 'turns'`

- [ ] **Step 3: 写实现**

`build_voice_context` 签名与主体改为：

```python
    async def build_voice_context(
        self,
        user_id: str | None,
        limit: int = 3,
        turns: tuple[ConversationTurn, ...] = (),
    ) -> str:
```

在现有 `recall_episodes` 那段之后、`body = "\n".join(...)` 之前插入：

```python
        # 本次会话的文字轮次。长期记忆里没有它们——文字聊天不写 episode
        # （只有语音挂断才写，见 app/api/voice.py），所以刚打字聊完就打电话，
        # 不注入这块语音那边就完全断片。
        if turns:
            lines.append(
                "# 本次会话刚聊过\n"
                + "\n".join(f"- 用户：{t.user_text}\n  你：{t.assistant_text}" for t in turns)
            )
```

`app/api/agent.py` 的两处调用改为 `build_voice_context(session.user_id, turns=session.turns)`（1131、2053），`746` 那处不动。

文件头补 `ConversationTurn` 的 import（`TYPE_CHECKING` 块里，与既有的 `EpisodeItem`/`MemoryItem` 并列）。

- [ ] **Step 4: 跑测试确认通过**

Run: `uv run pytest tests/test_context_builder.py tests/test_memory_integration.py tests/test_voice_lifecycle.py -q`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add app/runtime/context_builder.py app/api/agent.py tests/test_context_builder.py
git commit -m "feat(voice): 接通时注入本次会话的文字历史"
```

---

### Task 9: 全量验证与文档收尾

**Files:**
- Modify: `docs/api/agent-mobile/`（若其中有"每轮无状态"之类的措辞，改成与实现一致）
- Test: 无新增

- [ ] **Step 1: 跑 CI 的三条确切命令**

```bash
uv run python -m pytest -v --cov=app.tools.agentos --cov=app.strategy --cov=app.api.strategy --cov-branch --cov-fail-under=100
uv run ruff check .
uv run mypy app
```

Expected: 全绿、覆盖率 100%。任一条红了就回到对应 Task，不要在这里打补丁。

- [ ] **Step 2: 检查文档里关于会话与上下文的措辞**

```bash
grep -rn "无状态\|单轮\|不保留上下文" docs/api/ | head
```

有与新行为矛盾的描述就改；没有就跳过这步。

- [ ] **Step 3: 提交（若 Step 2 改了东西）**

```bash
git add docs/
git commit -m "docs: 会话上下文说明与实现对齐"
```

- [ ] **Step 4: 推分支开 PR**

```bash
git push -u origin feat/text-turn-conversation-history
gh pr create --base main --title "feat(runtime): 文本回合多轮上下文" --body-file <(...)
```

PR 描述要写清：事故复现路径、根因（`runner.py:549`）、两半修复（上下文 + 检索词锚定）、§7 那个静默失效点为什么必须一起改、以及"需真机验证"清单：

- [ ] 真机复跑 Leopold 链路，点追问 chip 后答案仍是 Leopold
- [ ] 隔轮问价格，确认模型重新调工具而不是引用历史里的旧数字
- [ ] 打字聊完立刻打电话，语音接得上刚才的话题
- [ ] 连聊 5 轮以上，观察首字延迟有没有明显变化（历史带长了会涨）

---

## Self-Review

**Spec 覆盖检查（逐节对照 TECH_DESIGN.md）：**

| Spec 节 | 对应 Task |
|---|---|
| §3 数据模型与裁剪 | Task 1 |
| §3 Redis 序列化 | Task 2 |
| §3.1 追问区不进历史 | Task 5 Step 1（`test_persisted_answer_has_the_followup_section_stripped`） |
| §4 runner 单点写入 | Task 5 |
| §5 组装 | Task 3 |
| §5 防污染 prompt | Task 4 |
| §6 检索词锚定 + 规则挪进 app | Task 7 |
| §7 WS stale session | Task 6 |
| §8 语音衔接 | Task 8 |
| §9 测试策略 | 分散在各 Task 的 Step 1，§9 表格 10 条全部有对应用例 |
| §10 不做什么 | 无 Task（YAGNI 边界） |
| §11 风险 | Task 9 Step 4 的真机清单覆盖前三条 |

无遗漏。

**Placeholder 扫描：** 无 TBD / TODO / "类似 Task N"。Task 6 Step 1 的测试主体给的是骨架而非全文——那里必须照 `tests/test_ws.py` 的既有 fixture 写，硬编一份连接样板反而会与仓库现状冲突；已在文中明确指向要参照的文件。

**类型一致性：** `ConversationTurn(user_text, assistant_text, created_at)` 在 Task 1/2/3/5/7/8 中字段名一致；`has_dangling_reference` / `dangling_reference_questions` / `anchor_search_query` 三个函数名在 Task 7 定义、Task 7 内使用，无别名漂移；`session_store` 参数名在 Task 5 定义、Task 5 装配处使用一致。
