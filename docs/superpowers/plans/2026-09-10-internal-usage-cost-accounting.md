# 内部用量与成本核算 实施计划（一期）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让任意 `user_id` + 账期能查出「用了多少 token/时长/次数、折算多少 USD」，三类用量（LLM / 语音 / 外部 API）统一落 PG 流水，金额可用单价与汇率快照复算。

**Architecture:** 新增独立包 `app/usage/`，内部分层为「领域模型 → 计价 → 存储 → 记账门面（UsageRecorder）」。三条业务链路只调门面一个方法，门面内部包 try/except 保证记账失败绝不影响主链路。查询走新的 `/internal/usage` 路由。

**Tech Stack:** Python 3.12、SQLAlchemy 2.x async Core（直插，不用 ORM 关系）、FastAPI、Decimal、pytest + pytest-asyncio（`asyncio_mode = "auto"`）、sqlite+aiosqlite（测试）

**Spec:** `docs/superpowers/specs/2026-09-09-internal-usage-cost-accounting-design.md`

## Global Constraints

- **不做任何顺手优化或重构。** 只改本计划明确列出的行。看到的其它问题一律不碰，记下来事后再说。
- **不引入 bug 优先于进度。** 每个 Task 结束时全量测试必须绿：`python -m pytest -q`。
- CI 门槛（必须本地先过，命令逐字照抄）：`python -m pytest -v --cov=app.tools.agentos --cov=app.strategy --cov=app.api.strategy --cov-branch --cov-fail-under=100` → `ruff check .` → `mypy app`
- ruff：`line-length = 100`，`target-version = "py312"`。
- 新代码测试覆盖 ≥ 80%（`app/usage/` 不在 CI 的 `--cov` 白名单内，但仍须自测到位）。
- 金额与单价一律 `Decimal`，DB 列用 `sa.Numeric(scale=8)`，回读一律 `Decimal(str(row.x))`（照 `app/strategy/equity_pg_store.py:28,58` 的既有惯例）。**禁止 float 存金额。**
- 流水表**只追加**。更正靠写反向冲抵记录，不 UPDATE 历史行。
- 流水表**不存 `billed_units`**，保持原始量纲（tokens / seconds / count）——spec D10。现有 `compute_billed_units()` 归语音配额专用，本计划不碰、不复用。
- 基准币种 **USD**（spec D4）。非 USD 供应商在写入时折算，同时存 `amount_original` + `currency_original` + `fx_rate_snapshot`。
- 拿不到用量或匹配不到单价时，**照样落一条流水**，`status` 记 `usage_missing` / `unpriced`，金额留空。**绝不填 0 冒充免费**（spec §7.2，与 `app/agents/llm.py:70` 既有红线一致）。
- 文件 200-400 行为宜，800 行为上限。

## 与 spec 的两处有意偏离（执行前请确认可接受）

**① `price_book` 与 `fx_rate` 一期不建表。** spec §5.2 / §5.3 定义了这两张表，
本计划把它们实现为代码内声明（`app/usage/price_seed.py`）+ 配置可覆盖，只有
`usage_ledger` 落 PG。

理由：账单的可复算性（G3）依赖的是**流水行里的 `unit_price_snapshot` /
`price_version` / `fx_rate_snapshot`**，不依赖单价表还在不在、有没有被改过。
单价表落库的真正价值是「运营不发版就能调价」，那是二期的运营需求，一期没人提。

代价：调价要改代码并发版。若 Bruce 要求一期就能不发版调价，本条要改回落库，
新增一个 Task（约等于 Task 5 的一半工作量）。

**② Task 10 只交付纯函数与配置位，不接真实回调。** spec D1 决议「语音服务端兜底
要做」。本计划做了时长推导与交叉校验的全部逻辑，并留好配置开关，但**没有把它接到
火山回调入口**。

理由：火山回调的 `EventType` 取值在本仓库代码里没有枚举——`app/voice/volc_callbacks.py`
是 schema-agnostic 的宽容解析。进房/退房对应哪个字面量必须先在 staging 用真实回调
确认。在代码里写猜测的魔法值，比暂不接线更危险：它会静默算出错的时长，而错的账单
比没有账单更糟。

后续：拿到 staging 真实事件样本后，接线是一次小改动（填两个配置项 + 回调入口调一次
`derive_duration_seconds`）。**在此之前，语音时长仍只有客户端上报这一个来源，
单用户语音账单存在已知漏报**——这一点要在交付给 Bruce 时说明。

---

## 三个必须先知道的现状事实（已核对代码，与 spec 描述的细微差异）

1. **LLM 记账只有一个点，不是两个。** spec §6.2 写「runner 两处（461 非流式 / 1051 流式）」。实际 `runner.py:461` 的 `complete()` 位于 `_stream_llm` 内部的降级分支，它随后也 `yield LLMStreamDone(result=result)`；而 `_stream_llm` 全仓只被 `runner.py:1053` 一处调用，`run_turn`（`runner.py:470`）内部又是消费 `run_turn_stream` 收干净的。**两条路最终都汇聚到 `LLMStreamDone`**，所以只在消费 `LLMStreamDone` 处记账即可覆盖全部 LLM 调用。改动面比 spec 预估的更小。

2. **turn_id 不需要改任何函数签名。** spec D9 要求「对话入口生成 UUID 贯穿整轮」。实现上在 `_run_turn_events` 函数体开头生成一个**局部变量**即可：该函数体内既有 `max_steps` 的 LLM 循环，也有 `_run_tool` 闭包（4 个 audit 记账点全在其作用域内），局部变量对两者天然可见。**不动 `run_turn` / `run_turn_stream` / `LLMProvider` 任何签名**（与既有「不动 run_turn 签名」口径一致）。

3. **`LLMResult` 没有模型名字段。** 按模型定价必须知道实际响应模型，而 runner 拿不到，config 里的配置值也不可靠（网关多模型 + fallback）。Task 8 给 `LLMResult` 加一个**带默认值的可选字段** `response_model: str | None = None`，由 `_to_result` 用既有的 `getattr(response, "model_name", None)` 填充。带默认值 → 现有全部构造点零影响。这是本计划唯一改动 `llm.py` 数据结构的地方。

---

### Task 1: 用量流水领域模型与 dedup_key

**Files:**
- Create: `app/usage/__init__.py`
- Create: `app/usage/models.py`
- Test: `tests/usage/__init__.py`, `tests/usage/test_usage_models.py`

**Interfaces:**
- Consumes: 无（本包起点）
- Produces: `UsageQuantity`、`UsageRecord`、类型别名 `UsageKind` / `UsageStatus`、
  `llm_dedup_key(turn_id: str, step_index: int) -> str`、
  `voice_dedup_key(session_id: str) -> str`、
  `api_dedup_key(trace_id: str, tool_name: str, seq: int) -> str`

- [ ] **Step 1: 写失败测试**

```python
# tests/usage/test_usage_models.py
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.usage.models import (
    UsageQuantity,
    UsageRecord,
    api_dedup_key,
    llm_dedup_key,
    voice_dedup_key,
)


def test_dedup_keys_are_stable_and_namespaced() -> None:
    """三类 kind 的 dedup_key 口径一次定死，跨版本不得改（spec §5.1）。"""
    assert llm_dedup_key("trc_abc", 0) == "llm:trc_abc:0"
    assert llm_dedup_key("trc_abc", 2) == "llm:trc_abc:2"
    assert voice_dedup_key("sess_x") == "voice:sess_x"
    assert api_dedup_key("trc_abc", "get_market", 1) == "api:trc_abc:get_market:1"


def test_dedup_keys_do_not_collide_across_kinds() -> None:
    keys = {llm_dedup_key("x", 1), voice_dedup_key("x"), api_dedup_key("x", "t", 1)}
    assert len(keys) == 3


def test_usage_record_is_immutable() -> None:
    """流水只追加不更新，用 frozen dataclass 从类型层面挡住原地改。"""
    record = UsageRecord(
        dedup_key="voice:sess_x",
        occurred_at=datetime(2026, 9, 10, tzinfo=UTC),
        recorded_at=datetime(2026, 9, 10, tzinfo=UTC),
        user_id="u1",
        kind="voice",
        provider="volc",
        scene="voice",
        quantity=UsageQuantity(duration_seconds=Decimal("61.5")),
    )
    with pytest.raises(FrozenInstanceError):
        record.user_id = "u2"  # type: ignore[misc]


def test_usage_quantity_defaults_are_all_none() -> None:
    """量纲字段默认 None 而非 0——0 表示「真的是零」，None 表示「没这个量纲」。"""
    q = UsageQuantity()
    assert q.input_tokens is None
    assert q.output_tokens is None
    assert q.cache_read_tokens is None
    assert q.cache_write_tokens is None
    assert q.duration_seconds is None
    assert q.call_count is None


def test_usage_record_optional_fields_default_to_none_and_status_ok() -> None:
    record = UsageRecord(
        dedup_key="llm:trc:0",
        occurred_at=datetime(2026, 9, 10, tzinfo=UTC),
        recorded_at=datetime(2026, 9, 10, tzinfo=UTC),
        user_id="u1",
        kind="llm",
        provider="anthropic",
        scene="chat",
        quantity=UsageQuantity(input_tokens=100, output_tokens=20),
    )
    assert record.status == "ok"
    assert record.amount_usd is None
    assert record.turn_id is None
    assert record.unit_price_snapshot is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/usage/test_usage_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.usage'`

- [ ] **Step 3: 写最小实现**

```python
# app/usage/__init__.py
"""内部用量与成本核算。

见 docs/superpowers/specs/2026-09-09-internal-usage-cost-accounting-design.md
"""
```

```python
# app/usage/models.py
"""用量流水领域模型。

流水**只追加**：更正靠写一条反向冲抵记录，不 UPDATE 历史行（spec §5.1）。
量纲保持原始单位（tokens / seconds / count），不存 billed_units——把时长单元与
token 单元相加会丢掉定价所需的结构（spec D10）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

UsageKind = Literal["llm", "voice", "external_api"]
UsageStatus = Literal["ok", "usage_missing", "duplicate", "unpriced"]


def llm_dedup_key(turn_id: str, step_index: int) -> str:
    """一轮内每步 LLM 调用一条流水。口径定死，改动会造成存量重复计费。"""
    return f"llm:{turn_id}:{step_index}"


def voice_dedup_key(session_id: str) -> str:
    """沿用语音既有的「按 session 幂等」口径（app/voice/usage.py 模块注释）。"""
    return f"voice:{session_id}"


def api_dedup_key(trace_id: str, tool_name: str, seq: int) -> str:
    """同一 trace 内同名工具可能被调多次，靠 seq 区分。"""
    return f"api:{trace_id}:{tool_name}:{seq}"


@dataclass(frozen=True)
class UsageQuantity:
    """原始量纲。

    默认 None 而非 0：None 表示「这类用量不适用」，0 表示「真的是零」。
    两者在报表里意义不同，不能混。
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    duration_seconds: Decimal | None = None
    call_count: int | None = None


@dataclass(frozen=True)
class UsageRecord:
    """一条用量流水。"""

    dedup_key: str
    occurred_at: datetime
    recorded_at: datetime
    user_id: str
    kind: UsageKind
    provider: str
    scene: str
    quantity: UsageQuantity
    session_id: str | None = None
    turn_id: str | None = None
    agent_id: str | None = None
    model: str | None = None
    price_version: str | None = None
    unit_price_snapshot: dict[str, Any] | None = None
    amount_original: Decimal | None = None
    currency_original: str | None = None
    fx_rate_snapshot: Decimal | None = None
    amount_usd: Decimal | None = None
    status: UsageStatus = "ok"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/usage/test_usage_models.py -v && ruff check app/usage tests/usage && mypy app/usage`
Expected: 5 passed，ruff 与 mypy 无输出

- [ ] **Step 5: 提交**

```bash
git add app/usage/__init__.py app/usage/models.py tests/usage/__init__.py tests/usage/test_usage_models.py
git commit -m "feat(usage): 用量流水领域模型与 dedup_key 口径"
```

---

### Task 2: 单价表与计价

**Files:**
- Create: `app/usage/pricing.py`
- Test: `tests/usage/test_usage_pricing.py`

**Interfaces:**
- Consumes: Task 1 的 `UsageQuantity`
- Produces: `PriceEntry`、`PriceBook`、`PriceBook.lookup(...) -> PriceEntry | None`、
  `quantize_amount(value: Decimal) -> Decimal`、
  `price_quantity(quantity, book, *, provider, kind, model, at) -> PricedAmount`、
  `PricedAmount`（字段 `amount`、`currency`、`price_version`、`snapshot`、`priced`）

**计价单位口径（写死，测试逐条锁）**
| quantity 字段 | metric 名 | 单价的「1 单位」 |
|---|---|---|
| `input_tokens` | `input_tokens` | 1M tokens |
| `output_tokens` | `output_tokens` | 1M tokens |
| `cache_read_tokens` | `cache_read` | 1M tokens |
| `cache_write_tokens` | `cache_write` | 1M tokens |
| `duration_seconds` | `duration_minute` | 1 分钟 |
| `call_count` | `call` | 1 次 |

- [ ] **Step 1: 写失败测试**

```python
# tests/usage/test_usage_pricing.py
from datetime import UTC, datetime
from decimal import Decimal

from app.usage.models import UsageQuantity
from app.usage.pricing import PriceBook, PriceEntry, price_quantity, quantize_amount

AT = datetime(2026, 9, 10, tzinfo=UTC)


def _entry(metric: str, price: str, *, model: str | None = "claude-opus-5", **kw: object) -> PriceEntry:
    defaults: dict[str, object] = {
        "provider": "anthropic",
        "kind": "llm",
        "metric": metric,
        "unit_price": Decimal(price),
        "currency": "USD",
        "effective_from": datetime(2026, 1, 1, tzinfo=UTC),
        "price_version": "v1",
        "model": model,
    }
    defaults.update(kw)
    return PriceEntry(**defaults)  # type: ignore[arg-type]


def test_token_price_is_per_million() -> None:
    """$15/1M input + $75/1M output，1000 in / 200 out。"""
    book = PriceBook([_entry("input_tokens", "15"), _entry("output_tokens", "75")])
    priced = price_quantity(
        UsageQuantity(input_tokens=1000, output_tokens=200),
        book,
        provider="anthropic",
        kind="llm",
        model="claude-opus-5",
        at=AT,
    )
    assert priced.priced is True
    # 1000/1e6*15 = 0.015 ; 200/1e6*75 = 0.015
    assert priced.amount == Decimal("0.03000000")
    assert priced.currency == "USD"
    assert priced.price_version == "v1"
    assert priced.snapshot == {"input_tokens": "15", "output_tokens": "75"}


def test_duration_price_is_per_minute_and_not_rounded_up() -> None:
    """成本核算按真实秒数折算，不做「不足一分钟按一分钟」的向上取整——
    那是配额口径（compute_billed_units），不是成本口径。"""
    book = PriceBook(
        [_entry("duration_minute", "0.6", provider="volc", kind="voice", model=None, currency="CNY")]
    )
    priced = price_quantity(
        UsageQuantity(duration_seconds=Decimal("90")),
        book,
        provider="volc",
        kind="voice",
        model=None,
        at=AT,
    )
    assert priced.amount == Decimal("0.90000000")  # 1.5 分钟 × 0.6
    assert priced.currency == "CNY"


def test_call_price_is_per_call() -> None:
    book = PriceBook(
        [_entry("call", "0.008", provider="tavily", kind="external_api", model=None)]
    )
    priced = price_quantity(
        UsageQuantity(call_count=3),
        book,
        provider="tavily",
        kind="external_api",
        model=None,
        at=AT,
    )
    assert priced.amount == Decimal("0.02400000")


def test_zero_price_entry_is_priced_not_unpriced() -> None:
    """Core 是内部服务，单价 0（spec D2）。0 与「查不到价」必须区分开。"""
    book = PriceBook([_entry("call", "0", provider="core", kind="external_api", model=None)])
    priced = price_quantity(
        UsageQuantity(call_count=5), book, provider="core", kind="external_api", model=None, at=AT
    )
    assert priced.priced is True
    assert priced.amount == Decimal("0E-8")


def test_missing_price_yields_unpriced_with_no_amount() -> None:
    """匹配不到单价 → 金额留空，绝不填 0（spec §5.2）。"""
    priced = price_quantity(
        UsageQuantity(input_tokens=100),
        PriceBook([]),
        provider="anthropic",
        kind="llm",
        model="claude-opus-5",
        at=AT,
    )
    assert priced.priced is False
    assert priced.amount is None


def test_partial_price_coverage_is_unpriced() -> None:
    """有 input 价没 output 价时，整条算 unpriced——只算一半会得出偏低的假金额。"""
    book = PriceBook([_entry("input_tokens", "15")])
    priced = price_quantity(
        UsageQuantity(input_tokens=1000, output_tokens=200),
        book,
        provider="anthropic",
        kind="llm",
        model="claude-opus-5",
        at=AT,
    )
    assert priced.priced is False
    assert priced.amount is None


def test_none_quantity_fields_do_not_require_a_price() -> None:
    """cache 两项为 None 时不参与计价，不因缺 cache 价而 unpriced。"""
    book = PriceBook([_entry("input_tokens", "15"), _entry("output_tokens", "75")])
    priced = price_quantity(
        UsageQuantity(input_tokens=1000, output_tokens=200, cache_read_tokens=None),
        book,
        provider="anthropic",
        kind="llm",
        model="claude-opus-5",
        at=AT,
    )
    assert priced.priced is True


def test_zero_quantity_still_needs_no_price_and_costs_nothing() -> None:
    """量为 0 时该 metric 不计费，也不因缺价而 unpriced。"""
    book = PriceBook([_entry("input_tokens", "15")])
    priced = price_quantity(
        UsageQuantity(input_tokens=1000, cache_write_tokens=0),
        book,
        provider="anthropic",
        kind="llm",
        model="claude-opus-5",
        at=AT,
    )
    assert priced.priced is True
    assert priced.amount == Decimal("0.01500000")


def test_effective_window_is_half_open() -> None:
    """[effective_from, effective_to) —— 调价当刻不能两条价都命中。"""
    old = _entry(
        "call",
        "1",
        provider="tavily",
        kind="external_api",
        model=None,
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        effective_to=datetime(2026, 9, 1, tzinfo=UTC),
        price_version="v1",
    )
    new = _entry(
        "call",
        "2",
        provider="tavily",
        kind="external_api",
        model=None,
        effective_from=datetime(2026, 9, 1, tzinfo=UTC),
        price_version="v2",
    )
    book = PriceBook([old, new])
    before = book.lookup(
        provider="tavily", kind="external_api", metric="call", model=None,
        at=datetime(2026, 8, 31, tzinfo=UTC),
    )
    boundary = book.lookup(
        provider="tavily", kind="external_api", metric="call", model=None,
        at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert before is not None and before.price_version == "v1"
    assert boundary is not None and boundary.price_version == "v2"


def test_model_specific_price_beats_wildcard() -> None:
    """model=None 的条目是该 provider 的兜底价；精确匹配优先。"""
    book = PriceBook(
        [
            _entry("input_tokens", "3", model=None, price_version="wild"),
            _entry("input_tokens", "15", model="claude-opus-5", price_version="exact"),
        ]
    )
    hit = book.lookup(
        provider="anthropic", kind="llm", metric="input_tokens", model="claude-opus-5", at=AT
    )
    assert hit is not None and hit.price_version == "exact"
    fallback = book.lookup(
        provider="anthropic", kind="llm", metric="input_tokens", model="claude-haiku-4-5", at=AT
    )
    assert fallback is not None and fallback.price_version == "wild"


def test_quantize_amount_keeps_eight_decimals_half_up() -> None:
    """token 单价量级到 1e-8，精度必须显式定死；
    strategy 侧曾因 Decimal 默认精度静默截断踩过一次。"""
    assert quantize_amount(Decimal("0.000000005")) == Decimal("0.00000001")
    assert quantize_amount(Decimal("1")) == Decimal("1.00000000")


def test_empty_quantity_is_priced_zero() -> None:
    priced = price_quantity(
        UsageQuantity(), PriceBook([]), provider="x", kind="llm", model=None, at=AT
    )
    assert priced.priced is True
    assert priced.amount == Decimal("0E-8")
    assert priced.snapshot == {}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/usage/test_usage_pricing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.usage.pricing'`

- [ ] **Step 3: 写最小实现**

```python
# app/usage/pricing.py
"""单价表与计价。

匹配不到单价时返回 priced=False、amount=None，由调用方落 status=unpriced。
**绝不返回 0 冒充免费**——那会让成本被系统性低估（spec §5.2）。
单价 0（如 Core 内部服务，spec D2）是有效价格，与「查不到价」严格区分。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from app.usage.models import UsageQuantity

#: 金额与单价统一保留 8 位小数，与 DB 的 Numeric(scale=8) 对齐。
AMOUNT_EXPONENT = Decimal("0.00000001")

TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)
SECONDS_PER_MINUTE = Decimal(60)

#: quantity 字段 → (metric 名, 把原始量换算成「计价单位数」的除数)
_METRICS: tuple[tuple[str, str, Decimal], ...] = (
    ("input_tokens", "input_tokens", TOKENS_PER_PRICE_UNIT),
    ("output_tokens", "output_tokens", TOKENS_PER_PRICE_UNIT),
    ("cache_read_tokens", "cache_read", TOKENS_PER_PRICE_UNIT),
    ("cache_write_tokens", "cache_write", TOKENS_PER_PRICE_UNIT),
    ("duration_seconds", "duration_minute", SECONDS_PER_MINUTE),
    ("call_count", "call", Decimal(1)),
)


def quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(AMOUNT_EXPONENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class PriceEntry:
    provider: str
    kind: str
    metric: str
    unit_price: Decimal
    currency: str
    effective_from: datetime
    price_version: str
    #: None 表示该 provider+kind 的兜底价；精确 model 优先。
    model: str | None = None
    effective_to: datetime | None = None


@dataclass(frozen=True)
class PricedAmount:
    """计价结果。``priced=False`` 表示缺价，此时 amount 必为 None。"""

    priced: bool
    amount: Decimal | None
    currency: str | None
    price_version: str | None
    snapshot: dict[str, str]


class PriceBook:
    """内存态单价表。条目不可变，构造后不改。"""

    def __init__(self, entries: Iterable[PriceEntry]) -> None:
        self._entries: Sequence[PriceEntry] = tuple(entries)

    def lookup(
        self, *, provider: str, kind: str, metric: str, model: str | None, at: datetime
    ) -> PriceEntry | None:
        """取 at 时刻生效的单价。生效区间是左闭右开 [from, to)。"""
        candidates = [
            entry
            for entry in self._entries
            if entry.provider == provider
            and entry.kind == kind
            and entry.metric == metric
            and entry.effective_from <= at
            and (entry.effective_to is None or at < entry.effective_to)
            and (entry.model is None or entry.model == model)
        ]
        if not candidates:
            return None
        # 精确 model 优先于兜底价；同档取最近生效的一条。
        candidates.sort(key=lambda e: (e.model is not None, e.effective_from))
        return candidates[-1]


def price_quantity(
    quantity: UsageQuantity,
    book: PriceBook,
    *,
    provider: str,
    kind: str,
    model: str | None,
    at: datetime,
) -> PricedAmount:
    """按原始量纲逐 metric 计价并求和。

    只有「量非 None 且非 0」的 metric 才需要单价：cache 两项常为 None 或 0，
    不该因为没配 cache 价就把整条流水判成 unpriced。
    """
    total = Decimal(0)
    snapshot: dict[str, str] = {}
    currency: str | None = None
    price_version: str | None = None

    for field, metric, divisor in _METRICS:
        raw = getattr(quantity, field)
        if raw is None:
            continue
        amount_units = Decimal(str(raw)) / divisor
        if amount_units == 0:
            continue
        entry = book.lookup(provider=provider, kind=kind, metric=metric, model=model, at=at)
        if entry is None:
            return PricedAmount(
                priced=False, amount=None, currency=None, price_version=None, snapshot={}
            )
        total += amount_units * entry.unit_price
        snapshot[metric] = str(entry.unit_price)
        currency = entry.currency
        price_version = entry.price_version

    return PricedAmount(
        priced=True,
        amount=quantize_amount(total),
        currency=currency,
        price_version=price_version,
        snapshot=snapshot,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/usage/test_usage_pricing.py -v && ruff check app/usage tests/usage && mypy app/usage`
Expected: 12 passed，ruff 与 mypy 无输出

- [ ] **Step 5: 提交**

```bash
git add app/usage/pricing.py tests/usage/test_usage_pricing.py
git commit -m "feat(usage): 单价表与按原始量纲计价"
```

---

### Task 3: 汇率折算到 USD

**Files:**
- Create: `app/usage/fx.py`
- Test: `tests/usage/test_usage_fx.py`

**Interfaces:**
- Consumes: Task 2 的 `quantize_amount`
- Produces: `FxRate`、`FxTable`、`FxTable.rate_for(currency, on) -> FxRate | None`、
  `convert_to_usd(amount, currency, rate) -> Decimal | None`

- [ ] **Step 1: 写失败测试**

```python
# tests/usage/test_usage_fx.py
from datetime import UTC, date, datetime
from decimal import Decimal

from app.usage.fx import FxRate, FxTable, convert_to_usd


def test_usd_needs_no_rate() -> None:
    assert convert_to_usd(Decimal("1.5"), "USD", None) == Decimal("1.50000000")


def test_cny_converts_with_rate_snapshot() -> None:
    rate = FxRate(rate_date=date(2026, 9, 9), currency="CNY", rate_to_usd=Decimal("0.14"), source="ecb")
    assert convert_to_usd(Decimal("0.9"), "CNY", rate) == Decimal("0.12600000")


def test_missing_rate_yields_none_not_zero() -> None:
    """没汇率就留空，不能当成 0 元（spec §7.2 同一红线）。"""
    assert convert_to_usd(Decimal("0.9"), "CNY", None) is None


def test_rate_for_picks_latest_on_or_before_date() -> None:
    """周末与节假日没有当日汇率，取不晚于该日的最近一条。"""
    table = FxTable(
        [
            FxRate(rate_date=date(2026, 9, 4), currency="CNY", rate_to_usd=Decimal("0.13"), source="ecb"),
            FxRate(rate_date=date(2026, 9, 8), currency="CNY", rate_to_usd=Decimal("0.14"), source="ecb"),
        ]
    )
    hit = table.rate_for("CNY", datetime(2026, 9, 9, tzinfo=UTC))
    assert hit is not None and hit.rate_to_usd == Decimal("0.14")
    early = table.rate_for("CNY", datetime(2026, 9, 5, tzinfo=UTC))
    assert early is not None and early.rate_to_usd == Decimal("0.13")


def test_rate_for_returns_none_before_first_record() -> None:
    table = FxTable(
        [FxRate(rate_date=date(2026, 9, 8), currency="CNY", rate_to_usd=Decimal("0.14"), source="ecb")]
    )
    assert table.rate_for("CNY", datetime(2026, 9, 1, tzinfo=UTC)) is None


def test_rate_for_unknown_currency_is_none() -> None:
    assert FxTable([]).rate_for("JPY", datetime(2026, 9, 9, tzinfo=UTC)) is None


def test_fallback_rate_used_when_table_has_no_record() -> None:
    """配置的固定汇率作为兜底（spec §4.2）。"""
    table = FxTable([], fallback={"CNY": Decimal("0.1385")})
    hit = table.rate_for("CNY", datetime(2026, 9, 9, tzinfo=UTC))
    assert hit is not None
    assert hit.rate_to_usd == Decimal("0.1385")
    assert hit.source == "fallback"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/usage/test_usage_fx.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.usage.fx'`

- [ ] **Step 3: 写最小实现**

```python
# app/usage/fx.py
"""汇率折算。基准币种 USD（spec D4）。

折算在**流水写入时**做，汇率快照随流水存下来——供应商调价与汇率波动都不能
改写历史账单的金额（spec §4.2）。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from app.usage.pricing import quantize_amount


@dataclass(frozen=True)
class FxRate:
    rate_date: date
    currency: str
    rate_to_usd: Decimal
    source: str


class FxTable:
    """按币种存一串按日汇率，取「不晚于目标日的最近一条」。

    周末与节假日没有当日汇率，若要求严格等于当日会大面积取不到值。
    """

    def __init__(
        self, rates: Iterable[FxRate], *, fallback: Mapping[str, Decimal] | None = None
    ) -> None:
        self._rates = tuple(sorted(rates, key=lambda r: r.rate_date))
        self._fallback = dict(fallback or {})

    def rate_for(self, currency: str, on: datetime) -> FxRate | None:
        target = on.date()
        hits = [r for r in self._rates if r.currency == currency and r.rate_date <= target]
        if hits:
            return hits[-1]
        fallback = self._fallback.get(currency)
        if fallback is None:
            return None
        return FxRate(
            rate_date=target, currency=currency, rate_to_usd=fallback, source="fallback"
        )


def convert_to_usd(amount: Decimal, currency: str, rate: FxRate | None) -> Decimal | None:
    """折算为 USD。拿不到汇率返回 None（留空，不当成 0）。"""
    if currency == "USD":
        return quantize_amount(amount)
    if rate is None:
        return None
    return quantize_amount(amount * rate.rate_to_usd)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/usage/test_usage_fx.py -v && ruff check app/usage tests/usage && mypy app/usage`
Expected: 7 passed，ruff 与 mypy 无输出

- [ ] **Step 5: 提交**

```bash
git add app/usage/fx.py tests/usage/test_usage_fx.py
git commit -m "feat(usage): 汇率折算与 USD 基准币种"
```

---

### Task 4: UsageStore 协议与内存实现

**Files:**
- Create: `app/usage/store.py`
- Test: `tests/usage/test_usage_store.py`

**Interfaces:**
- Consumes: Task 1 的 `UsageRecord`
- Produces: `UsageStore`（Protocol）、`InMemoryUsageStore`、`UsageAggregate`、`UserBill`
  - `append(record: UsageRecord) -> bool`（False = dedup_key 已存在，未新增）
  - `bill_for_user(user_id, start, end) -> UserBill`
  - `list_for_user(user_id, start, end, *, kind=None, limit=100, offset=0) -> list[UsageRecord]`
  - `aggregate(start, end, *, group_by: Sequence[str]) -> list[UsageAggregate]`
  - `top_users(start, end, *, limit=10) -> list[tuple[str, Decimal]]`

- [ ] **Step 1: 写失败测试**

```python
# tests/usage/test_usage_store.py
from datetime import UTC, datetime
from decimal import Decimal

from app.usage.models import UsageQuantity, UsageRecord
from app.usage.store import InMemoryUsageStore

T0 = datetime(2026, 9, 5, tzinfo=UTC)
T1 = datetime(2026, 9, 10, tzinfo=UTC)
T2 = datetime(2026, 9, 20, tzinfo=UTC)


def _record(
    key: str,
    *,
    user_id: str = "u1",
    occurred_at: datetime = T1,
    amount_usd: Decimal | None = Decimal("0.01"),
    kind: str = "llm",
    scene: str = "chat",
    provider: str = "anthropic",
    status: str = "ok",
    quantity: UsageQuantity | None = None,
) -> UsageRecord:
    return UsageRecord(
        dedup_key=key,
        occurred_at=occurred_at,
        recorded_at=occurred_at,
        user_id=user_id,
        kind=kind,  # type: ignore[arg-type]
        provider=provider,
        scene=scene,
        quantity=quantity or UsageQuantity(input_tokens=100, output_tokens=10),
        amount_usd=amount_usd,
        status=status,  # type: ignore[arg-type]
    )


async def test_append_is_idempotent_on_dedup_key() -> None:
    """重复上报不产生新流水——语音挂断重试、前后台切换都会走到这里。"""
    store = InMemoryUsageStore()
    assert await store.append(_record("k1")) is True
    assert await store.append(_record("k1")) is False
    bill = await store.bill_for_user("u1", T0, T2)
    assert bill.total_amount_usd == Decimal("0.01")


async def test_bill_sums_only_records_inside_period() -> None:
    store = InMemoryUsageStore()
    await store.append(_record("in", occurred_at=T1))
    await store.append(_record("before", occurred_at=datetime(2026, 8, 1, tzinfo=UTC)))
    await store.append(_record("after", occurred_at=datetime(2026, 10, 1, tzinfo=UTC)))
    bill = await store.bill_for_user("u1", T0, T2)
    assert bill.total_amount_usd == Decimal("0.01")


async def test_bill_period_is_half_open() -> None:
    """[start, end) —— 月度账单相邻两月不能双算同一条。"""
    store = InMemoryUsageStore()
    await store.append(_record("edge", occurred_at=T2))
    assert (await store.bill_for_user("u1", T0, T2)).total_amount_usd == Decimal(0)
    assert (await store.bill_for_user("u1", T2, datetime(2026, 9, 21, tzinfo=UTC))).total_amount_usd == Decimal("0.01")


async def test_bill_isolates_users() -> None:
    store = InMemoryUsageStore()
    await store.append(_record("a", user_id="u1"))
    await store.append(_record("b", user_id="u2", amount_usd=Decimal("5")))
    assert (await store.bill_for_user("u1", T0, T2)).total_amount_usd == Decimal("0.01")


async def test_bill_counts_data_quality_flags() -> None:
    """usage_missing / unpriced 必须可见，否则拿到的是偏低的假数（spec §7.2）。"""
    store = InMemoryUsageStore()
    await store.append(_record("ok", status="ok"))
    await store.append(_record("miss", status="usage_missing", amount_usd=None))
    await store.append(_record("unpriced", status="unpriced", amount_usd=None))
    bill = await store.bill_for_user("u1", T0, T2)
    assert bill.usage_missing_count == 1
    assert bill.unpriced_count == 1
    assert bill.record_count == 3
    assert bill.total_amount_usd == Decimal("0.01")


async def test_bill_groups_by_kind_and_scene() -> None:
    store = InMemoryUsageStore()
    await store.append(_record("l", kind="llm", scene="chat", amount_usd=Decimal("1")))
    await store.append(
        _record("v", kind="voice", scene="voice", amount_usd=Decimal("2"),
                quantity=UsageQuantity(duration_seconds=Decimal("60"))))
    bill = await store.bill_for_user("u1", T0, T2)
    assert bill.by_kind == {"llm": Decimal("1"), "voice": Decimal("2")}
    assert bill.by_scene == {"chat": Decimal("1"), "voice": Decimal("2")}


async def test_bill_sums_quantities_per_kind() -> None:
    """账单要答「用了多少量」，不只是多少钱。"""
    store = InMemoryUsageStore()
    await store.append(_record("a", quantity=UsageQuantity(input_tokens=100, output_tokens=10)))
    await store.append(_record("b", quantity=UsageQuantity(input_tokens=50, output_tokens=5)))
    bill = await store.bill_for_user("u1", T0, T2)
    assert bill.quantity_totals["input_tokens"] == 150
    assert bill.quantity_totals["output_tokens"] == 15


async def test_list_for_user_filters_and_paginates() -> None:
    store = InMemoryUsageStore()
    for i in range(5):
        await store.append(_record(f"k{i}"))
    await store.append(_record("voice", kind="voice", scene="voice"))
    page = await store.list_for_user("u1", T0, T2, limit=2, offset=0)
    assert len(page) == 2
    only_voice = await store.list_for_user("u1", T0, T2, kind="voice")
    assert len(only_voice) == 1


async def test_aggregate_groups_by_requested_dimensions() -> None:
    store = InMemoryUsageStore()
    await store.append(_record("a", scene="chat", provider="anthropic", amount_usd=Decimal("1")))
    await store.append(_record("b", scene="chat", provider="anthropic", amount_usd=Decimal("2")))
    await store.append(_record("c", scene="voice", provider="volc", amount_usd=Decimal("4")))
    rows = await store.aggregate(T0, T2, group_by=("scene", "provider"))
    by_key = {(r.keys["scene"], r.keys["provider"]): r.amount_usd for r in rows}
    assert by_key[("chat", "anthropic")] == Decimal("3")
    assert by_key[("voice", "volc")] == Decimal("4")


async def test_top_users_orders_by_amount_desc() -> None:
    store = InMemoryUsageStore()
    await store.append(_record("a", user_id="u1", amount_usd=Decimal("1")))
    await store.append(_record("b", user_id="u2", amount_usd=Decimal("9")))
    await store.append(_record("c", user_id="u3", amount_usd=Decimal("5")))
    assert await store.top_users(T0, T2, limit=2) == [("u2", Decimal("9")), ("u3", Decimal("5"))]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/usage/test_usage_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.usage.store'`

- [ ] **Step 3: 写最小实现**

```python
# app/usage/store.py
"""用量流水存储接口与内存实现。

账期一律左闭右开 [start, end)：相邻两个月的账单不能双算同一条流水。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.usage.models import UsageRecord

#: 参与「用了多少量」汇总的量纲字段。
_QUANTITY_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "duration_seconds",
    "call_count",
)


@dataclass(frozen=True)
class UsageAggregate:
    keys: dict[str, str]
    amount_usd: Decimal
    record_count: int


@dataclass(frozen=True)
class UserBill:
    """单用户账期账单。"""

    user_id: str
    start: datetime
    end: datetime
    total_amount_usd: Decimal
    record_count: int
    usage_missing_count: int
    unpriced_count: int
    by_kind: dict[str, Decimal] = field(default_factory=dict)
    by_scene: dict[str, Decimal] = field(default_factory=dict)
    quantity_totals: dict[str, Decimal] = field(default_factory=dict)


class UsageStore(Protocol):
    async def append(self, record: UsageRecord) -> bool: ...

    async def bill_for_user(
        self, user_id: str, start: datetime, end: datetime
    ) -> UserBill: ...

    async def list_for_user(
        self,
        user_id: str,
        start: datetime,
        end: datetime,
        *,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[UsageRecord]: ...

    async def aggregate(
        self, start: datetime, end: datetime, *, group_by: Sequence[str]
    ) -> list[UsageAggregate]: ...

    async def top_users(
        self, start: datetime, end: datetime, *, limit: int = 10
    ) -> list[tuple[str, Decimal]]: ...


def build_bill(
    user_id: str, start: datetime, end: datetime, records: Sequence[UsageRecord]
) -> UserBill:
    """把一批流水聚成账单。PG 实现回读后复用同一段逻辑，保证两种存储口径一致。"""
    total = Decimal(0)
    by_kind: dict[str, Decimal] = {}
    by_scene: dict[str, Decimal] = {}
    quantity_totals: dict[str, Decimal] = {}
    usage_missing = 0
    unpriced = 0

    for record in records:
        if record.status == "usage_missing":
            usage_missing += 1
        elif record.status == "unpriced":
            unpriced += 1
        amount = record.amount_usd or Decimal(0)
        total += amount
        by_kind[record.kind] = by_kind.get(record.kind, Decimal(0)) + amount
        by_scene[record.scene] = by_scene.get(record.scene, Decimal(0)) + amount
        for name in _QUANTITY_FIELDS:
            value = getattr(record.quantity, name)
            if value is None:
                continue
            quantity_totals[name] = quantity_totals.get(name, Decimal(0)) + Decimal(str(value))

    return UserBill(
        user_id=user_id,
        start=start,
        end=end,
        total_amount_usd=total,
        record_count=len(records),
        usage_missing_count=usage_missing,
        unpriced_count=unpriced,
        by_kind=by_kind,
        by_scene=by_scene,
        quantity_totals=quantity_totals,
    )


class InMemoryUsageStore:
    """进程内流水账本（自测与 fake 模式用）。"""

    def __init__(self) -> None:
        self._records: list[UsageRecord] = []
        self._keys: set[str] = set()

    def _in_period(
        self, record: UsageRecord, start: datetime, end: datetime
    ) -> bool:
        return start <= record.occurred_at < end

    async def append(self, record: UsageRecord) -> bool:
        if record.dedup_key in self._keys:
            return False
        self._keys.add(record.dedup_key)
        self._records.append(record)
        return True

    async def bill_for_user(self, user_id: str, start: datetime, end: datetime) -> UserBill:
        rows = [
            r
            for r in self._records
            if r.user_id == user_id and self._in_period(r, start, end)
        ]
        return build_bill(user_id, start, end, rows)

    async def list_for_user(
        self,
        user_id: str,
        start: datetime,
        end: datetime,
        *,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[UsageRecord]:
        rows = [
            r
            for r in self._records
            if r.user_id == user_id
            and self._in_period(r, start, end)
            and (kind is None or r.kind == kind)
        ]
        rows.sort(key=lambda r: r.occurred_at, reverse=True)
        return rows[offset : offset + limit]

    async def aggregate(
        self, start: datetime, end: datetime, *, group_by: Sequence[str]
    ) -> list[UsageAggregate]:
        buckets: dict[tuple[str, ...], list[UsageRecord]] = {}
        for record in self._records:
            if not self._in_period(record, start, end):
                continue
            key = tuple(str(getattr(record, dim)) for dim in group_by)
            buckets.setdefault(key, []).append(record)
        return [
            UsageAggregate(
                keys=dict(zip(group_by, key, strict=True)),
                amount_usd=sum((r.amount_usd or Decimal(0) for r in rows), start=Decimal(0)),
                record_count=len(rows),
            )
            for key, rows in buckets.items()
        ]

    async def top_users(
        self, start: datetime, end: datetime, *, limit: int = 10
    ) -> list[tuple[str, Decimal]]:
        totals: dict[str, Decimal] = {}
        for record in self._records:
            if not self._in_period(record, start, end):
                continue
            totals[record.user_id] = totals.get(record.user_id, Decimal(0)) + (
                record.amount_usd or Decimal(0)
            )
        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[:limit]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/usage/test_usage_store.py -v && ruff check app/usage tests/usage && mypy app/usage`
Expected: 10 passed，ruff 与 mypy 无输出

- [ ] **Step 5: 提交**

```bash
git add app/usage/store.py tests/usage/test_usage_store.py
git commit -m "feat(usage): UsageStore 协议与内存流水账本"
```

---

### Task 5: PostgresUsageStore

**Files:**
- Create: `app/usage/pg_store.py`
- Test: `tests/usage/test_usage_pg_store.py`

**Interfaces:**
- Consumes: Task 4 的 `UsageStore` 协议、`build_bill`、`UserBill`、`UsageAggregate`
- Produces: `PostgresUsageStore(engine)`、`create_schema()`、模块级 `metadata` 与 `usage_ledger` 表对象

**幂等的实现方式（关键，不要照抄别处）**

靠 `dedup_key` 的 UNIQUE 约束 + 捕获 `sqlalchemy.exc.IntegrityError`。

- **不能用** `sa.dialects.postgresql.insert(...).on_conflict_do_nothing()`——PG 专有，会破坏 sqlite 测试（`app/news/pg_store.py:137` 已记录这条教训）。
- **也不能抄** `app/news/pg_store.py` 的 SELECT-then-INSERT——那段的安全性依赖 RoundLock 保证单写者，注释里写明了。用量记账没有单写者：并发请求与语音重复上报会同时进来，SELECT-then-INSERT 在竞态下会插进两条，**直接变成重复计费**。
- `IntegrityError` 在 sqlite 与 PG 下都会抛，跨方言安全。
- **但只能吞唯一约束冲突。** 无条件 `except IntegrityError: return False` 会把 NOT NULL
  违反、长度超限也当成「重复上报」——流水静默丢失，调用方还以为写成功了。实测中这个坑
  真的踩到了（`id` 列在 sqlite 下不自增触发 NOT NULL），靠测试断言 `is True` 才暴露。

- [ ] **Step 1: 写失败测试**

```python
# tests/usage/test_usage_pg_store.py
import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from app.usage.models import UsageQuantity, UsageRecord
from app.usage.pg_store import PostgresUsageStore

T0 = datetime(2026, 9, 1, tzinfo=UTC)
T1 = datetime(2026, 9, 10, tzinfo=UTC)
T2 = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
async def store() -> PostgresUsageStore:
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    s = PostgresUsageStore(engine)
    await s.create_schema()
    return s


def _record(key: str, **kw: object) -> UsageRecord:
    defaults: dict[str, object] = {
        "dedup_key": key,
        "occurred_at": T1,
        "recorded_at": T1,
        "user_id": "u1",
        "kind": "llm",
        "provider": "anthropic",
        "scene": "chat",
        "quantity": UsageQuantity(input_tokens=1000, output_tokens=200),
        "amount_usd": Decimal("0.03"),
    }
    defaults.update(kw)
    return UsageRecord(**defaults)  # type: ignore[arg-type]


async def test_append_then_read_back_roundtrip(store: PostgresUsageStore) -> None:
    await store.append(
        _record(
            "k1",
            model="claude-opus-5",
            turn_id="trc_1",
            session_id="sess_1",
            agent_id="agent_1",
            price_version="v1",
            unit_price_snapshot={"input_tokens": "15"},
            amount_original=Decimal("0.03"),
            currency_original="USD",
        )
    )
    rows = await store.list_for_user("u1", T0, T2)
    assert len(rows) == 1
    row = rows[0]
    assert row.dedup_key == "k1"
    assert row.model == "claude-opus-5"
    assert row.turn_id == "trc_1"
    assert row.quantity.input_tokens == 1000
    assert row.quantity.output_tokens == 200
    assert row.unit_price_snapshot == {"input_tokens": "15"}
    assert row.amount_usd == Decimal("0.03")
    assert row.occurred_at.tzinfo is not None


async def test_amount_keeps_eight_decimal_precision(store: PostgresUsageStore) -> None:
    """回读必须还是 Decimal 且不丢精度——float 化会静默吃掉 token 级金额。"""
    await store.append(_record("tiny", amount_usd=Decimal("0.00000123")))
    row = (await store.list_for_user("u1", T0, T2))[0]
    assert isinstance(row.amount_usd, Decimal)
    assert row.amount_usd == Decimal("0.00000123")


async def test_duration_seconds_roundtrip_as_decimal(store: PostgresUsageStore) -> None:
    await store.append(
        _record("v", kind="voice", scene="voice", provider="volc",
                quantity=UsageQuantity(duration_seconds=Decimal("61.500")))
    )
    row = (await store.list_for_user("u1", T0, T2))[0]
    assert row.quantity.duration_seconds == Decimal("61.500")


async def test_duplicate_dedup_key_returns_false_and_inserts_once(
    store: PostgresUsageStore,
) -> None:
    assert await store.append(_record("dup")) is True
    assert await store.append(_record("dup")) is False
    assert len(await store.list_for_user("u1", T0, T2)) == 1


async def test_concurrent_duplicate_appends_insert_only_one(tmp_path: Path) -> None:
    """并发重复上报只能进一条。SELECT-then-INSERT 在这个用例下会漏成两条。

    必须用**文件型** sqlite：内存型 + StaticPool 下所有连接共用同一个物理连接，
    5 个 engine.begin() 实际落在同一个 sqlite 事务里，冲突的那几个 rollback 会把
    已成功的插入一起回滚——那是测试环境的产物，不是 PG 的行为。
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/usage.db")
    concurrent_store = PostgresUsageStore(engine)
    await concurrent_store.create_schema()
    try:
        results = await asyncio.gather(
            *[concurrent_store.append(_record("race")) for _ in range(5)],
            return_exceptions=True,
        )
        assert not [r for r in results if isinstance(r, Exception)]
        assert results.count(True) == 1
        assert len(await concurrent_store.list_for_user("u1", T0, T2)) == 1
    finally:
        await engine.dispose()


async def test_non_unique_integrity_error_is_raised_not_swallowed(
    store: PostgresUsageStore,
) -> None:
    """只有唯一冲突才算「重复上报」。

    NOT NULL 违反这类完整性错误必须抛出去：一并当成重复会让流水静默丢失，
    而调用方还以为写成功了（spec §7.2 的静默漏数）。
    """
    with pytest.raises(IntegrityError):
        await store.append(_record("bad", user_id=None))


async def test_nullable_fields_survive_roundtrip(store: PostgresUsageStore) -> None:
    """usage_missing 的流水金额与量纲全空，回读不得变成 0。"""
    await store.append(
        _record("miss", status="usage_missing", amount_usd=None, quantity=UsageQuantity())
    )
    row = (await store.list_for_user("u1", T0, T2))[0]
    assert row.status == "usage_missing"
    assert row.amount_usd is None
    assert row.quantity.input_tokens is None
    assert row.unit_price_snapshot is None


async def test_bill_matches_in_memory_semantics(store: PostgresUsageStore) -> None:
    await store.append(_record("a", amount_usd=Decimal("1")))
    await store.append(_record("b", kind="voice", scene="voice", amount_usd=Decimal("2")))
    await store.append(_record("c", status="usage_missing", amount_usd=None))
    bill = await store.bill_for_user("u1", T0, T2)
    assert bill.total_amount_usd == Decimal("3")
    assert bill.by_kind == {"llm": Decimal("1"), "voice": Decimal("2")}
    assert bill.usage_missing_count == 1
    assert bill.record_count == 3


async def test_bill_period_is_half_open(store: PostgresUsageStore) -> None:
    await store.append(_record("edge", occurred_at=T2))
    assert (await store.bill_for_user("u1", T0, T2)).record_count == 0


async def test_aggregate_and_top_users(store: PostgresUsageStore) -> None:
    await store.append(_record("a", user_id="u1", amount_usd=Decimal("1")))
    await store.append(_record("b", user_id="u2", amount_usd=Decimal("9"), scene="voice"))
    rows = await store.aggregate(T0, T2, group_by=("scene",))
    assert {r.keys["scene"]: r.amount_usd for r in rows} == {
        "chat": Decimal("1"),
        "voice": Decimal("9"),
    }
    assert await store.top_users(T0, T2, limit=1) == [("u2", Decimal("9"))]


async def test_list_for_user_rejects_unknown_group_by(store: PostgresUsageStore) -> None:
    """group_by 直接拼进 SQL，必须白名单校验，不能信外部字符串。"""
    with pytest.raises(ValueError, match="group_by"):
        await store.aggregate(T0, T2, group_by=("user_id; DROP TABLE usage_ledger",))
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/usage/test_usage_pg_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.usage.pg_store'`

- [ ] **Step 3: 写最小实现**

```python
# app/usage/pg_store.py
"""PostgreSQL 用量流水存储（SQLAlchemy 2.x async Core，直插不搞 ORM 关系）。

表结构与索引照 app/history/pg_store.py 的范式：模块级 MetaData + sa.Table。
金额列一律 Numeric(scale=8)、回读一律 Decimal(str(...))，照
app/strategy/equity_pg_store.py 的既有惯例——float 会静默吃掉 token 级金额。

幂等靠 dedup_key 的 UNIQUE 约束 + IntegrityError：
- 不用 PG 专有的 on_conflict_do_nothing（会破坏 sqlite 测试）；
- 也不用 news/pg_store 的 SELECT-then-INSERT（那依赖 RoundLock 单写者，
  用量记账没有这个保证，并发下会插两条 = 重复计费）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.usage.models import UsageQuantity, UsageRecord
from app.usage.store import UsageAggregate, UserBill, build_bill

metadata = sa.MetaData()

usage_ledger = sa.Table(
    "usage_ledger",
    metadata,
    # sqlite 只对 INTEGER PRIMARY KEY 自增，BIGINT 不自增；PG 侧流水量大仍要 BIGINT。
    sa.Column(
        "id",
        sa.BigInteger().with_variant(sa.Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    sa.Column("dedup_key", sa.String(128), nullable=False, unique=True),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("user_id", sa.String(64), nullable=False),
    sa.Column("session_id", sa.String(64), nullable=True),
    sa.Column("turn_id", sa.String(64), nullable=True),
    sa.Column("agent_id", sa.String(64), nullable=True),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("provider", sa.String(32), nullable=False),
    sa.Column("model", sa.String(64), nullable=True),
    sa.Column("scene", sa.String(32), nullable=False),
    sa.Column("input_tokens", sa.Integer, nullable=True),
    sa.Column("output_tokens", sa.Integer, nullable=True),
    sa.Column("cache_read_tokens", sa.Integer, nullable=True),
    sa.Column("cache_write_tokens", sa.Integer, nullable=True),
    sa.Column("duration_seconds", sa.Numeric(scale=3), nullable=True),
    sa.Column("call_count", sa.Integer, nullable=True),
    sa.Column("price_version", sa.String(32), nullable=True),
    sa.Column("unit_price_snapshot", sa.JSON, nullable=True),
    sa.Column("amount_original", sa.Numeric(scale=8), nullable=True),
    sa.Column("currency_original", sa.String(8), nullable=True),
    sa.Column("fx_rate_snapshot", sa.Numeric(scale=8), nullable=True),
    sa.Column("amount_usd", sa.Numeric(scale=8), nullable=True),
    sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
    # 单用户账期查询是主路径（spec §5.1）
    sa.Index("ix_usage_user_time", "user_id", "occurred_at"),
    sa.Index("ix_usage_time_kind", "occurred_at", "kind"),
    sa.Index("ix_usage_session", "session_id"),
)

def _is_unique_violation(exc: IntegrityError) -> bool:
    """只有唯一约束冲突才算「重复上报」。

    其它 IntegrityError（NOT NULL 违反、长度超限）必须抛出去。把它们一并当成
    重复，会让流水静默丢失且无人知晓——正是 spec §7.2 要防的静默漏数。
    """
    orig = exc.orig
    for attr in ("sqlstate", "pgcode"):
        code = getattr(orig, attr, None)
        if code:
            return str(code) == "23505"  # PG unique_violation
    return "UNIQUE constraint failed" in str(orig)


#: aggregate 的 group_by 会拼进 SQL，只允许这些列。
_GROUPABLE = frozenset({"kind", "provider", "model", "scene", "user_id", "status"})


def ensure_utc(value: datetime) -> datetime:
    """回读的时间统一成带时区（sqlite 会丢 tzinfo）。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


def _to_row(record: UsageRecord) -> dict[str, Any]:
    q = record.quantity
    return {
        "dedup_key": record.dedup_key,
        "occurred_at": record.occurred_at,
        "recorded_at": record.recorded_at,
        "user_id": record.user_id,
        "session_id": record.session_id,
        "turn_id": record.turn_id,
        "agent_id": record.agent_id,
        "kind": record.kind,
        "provider": record.provider,
        "model": record.model,
        "scene": record.scene,
        "input_tokens": q.input_tokens,
        "output_tokens": q.output_tokens,
        "cache_read_tokens": q.cache_read_tokens,
        "cache_write_tokens": q.cache_write_tokens,
        "duration_seconds": q.duration_seconds,
        "call_count": q.call_count,
        "price_version": record.price_version,
        "unit_price_snapshot": record.unit_price_snapshot,
        "amount_original": record.amount_original,
        "currency_original": record.currency_original,
        "fx_rate_snapshot": record.fx_rate_snapshot,
        "amount_usd": record.amount_usd,
        "status": record.status,
    }


def _from_row(row: Any) -> UsageRecord:
    snapshot = row.unit_price_snapshot
    if isinstance(snapshot, str):  # SQLite 把 JSON 列回读成字符串
        snapshot = json.loads(snapshot)
    return UsageRecord(
        dedup_key=row.dedup_key,
        occurred_at=ensure_utc(row.occurred_at),
        recorded_at=ensure_utc(row.recorded_at),
        user_id=row.user_id,
        session_id=row.session_id,
        turn_id=row.turn_id,
        agent_id=row.agent_id,
        kind=row.kind,
        provider=row.provider,
        model=row.model,
        scene=row.scene,
        quantity=UsageQuantity(
            input_tokens=_int_or_none(row.input_tokens),
            output_tokens=_int_or_none(row.output_tokens),
            cache_read_tokens=_int_or_none(row.cache_read_tokens),
            cache_write_tokens=_int_or_none(row.cache_write_tokens),
            duration_seconds=_decimal_or_none(row.duration_seconds),
            call_count=_int_or_none(row.call_count),
        ),
        price_version=row.price_version,
        unit_price_snapshot=snapshot,
        amount_original=_decimal_or_none(row.amount_original),
        currency_original=row.currency_original,
        fx_rate_snapshot=_decimal_or_none(row.fx_rate_snapshot),
        amount_usd=_decimal_or_none(row.amount_usd),
        status=row.status,
    )


class PostgresUsageStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_schema(self) -> None:
        """建表（MVP 用；生产改 Alembic 迁移）。"""
        async with self._engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def append(self, record: UsageRecord) -> bool:
        """写一条流水。返回 False 表示 dedup_key 已存在，本次没有新增。"""
        try:
            async with self._engine.begin() as conn:
                await conn.execute(sa.insert(usage_ledger).values(**_to_row(record)))
        except IntegrityError as exc:
            if not _is_unique_violation(exc):
                raise
            return False
        return True

    def _period(self, start: datetime, end: datetime) -> Any:
        return sa.and_(
            usage_ledger.c.occurred_at >= start, usage_ledger.c.occurred_at < end
        )

    async def bill_for_user(self, user_id: str, start: datetime, end: datetime) -> UserBill:
        rows = await self.list_for_user(user_id, start, end, limit=1_000_000)
        return build_bill(user_id, start, end, rows)

    async def list_for_user(
        self,
        user_id: str,
        start: datetime,
        end: datetime,
        *,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[UsageRecord]:
        query = (
            sa.select(usage_ledger)
            .where(usage_ledger.c.user_id == user_id, self._period(start, end))
            .order_by(usage_ledger.c.occurred_at.desc(), usage_ledger.c.id.desc())
            .limit(limit)
            .offset(offset)
        )
        if kind is not None:
            query = query.where(usage_ledger.c.kind == kind)
        async with self._engine.connect() as conn:
            result = await conn.execute(query)
            rows = result.fetchall()
        return [_from_row(row) for row in rows]

    async def aggregate(
        self, start: datetime, end: datetime, *, group_by: Sequence[str]
    ) -> list[UsageAggregate]:
        unknown = [dim for dim in group_by if dim not in _GROUPABLE]
        if unknown:
            raise ValueError(f"group_by 含不支持的维度: {unknown}")
        columns = [usage_ledger.c[dim] for dim in group_by]
        query = (
            sa.select(
                *columns,
                sa.func.sum(usage_ledger.c.amount_usd).label("amount"),
                sa.func.count().label("cnt"),
            )
            .where(self._period(start, end))
            .group_by(*columns)
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(query)
            rows = result.fetchall()
        return [
            UsageAggregate(
                keys={dim: str(row[i]) for i, dim in enumerate(group_by)},
                amount_usd=_decimal_or_none(row.amount) or Decimal(0),
                record_count=int(row.cnt),
            )
            for row in rows
        ]

    async def top_users(
        self, start: datetime, end: datetime, *, limit: int = 10
    ) -> list[tuple[str, Decimal]]:
        total = sa.func.sum(usage_ledger.c.amount_usd).label("amount")
        query = (
            sa.select(usage_ledger.c.user_id, total)
            .where(self._period(start, end))
            .group_by(usage_ledger.c.user_id)
            .order_by(total.desc())
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            result = await conn.execute(query)
            rows = result.fetchall()
        return [(row.user_id, _decimal_or_none(row.amount) or Decimal(0)) for row in rows]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/usage/test_usage_pg_store.py -v && ruff check app/usage tests/usage && mypy app/usage`
Expected: 10 passed，ruff 与 mypy 无输出

若 `test_amount_keeps_eight_decimal_precision` 失败并提示精度丢失，说明 sqlite 把
`Numeric` 回读成了 float——检查 `_from_row` 是否对每个金额列都走了 `Decimal(str(...))`，
不要改测试。

- [ ] **Step 5: 提交**

```bash
git add app/usage/pg_store.py tests/usage/test_usage_pg_store.py
git commit -m "feat(usage): PostgreSQL 流水存储，dedup_key 唯一约束兜幂等"
```

---

### Task 6: UsageRecorder 记账门面（fail-safe）

**Files:**
- Create: `app/usage/recorder.py`
- Test: `tests/usage/test_usage_recorder.py`

**Interfaces:**
- Consumes: Task 1-5 全部
- Produces: `UsageRecorder(store, price_book, fx_table)`，方法：
  - `record_llm(*, turn_id, step_index, user_id, session_id, agent_id, scene, provider, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, occurred_at) -> None`
  - `record_voice(*, session_id, user_id, provider, duration_seconds, audio_tokens, text_tokens, occurred_at) -> None`
  - `record_external_api(*, trace_id, tool_name, seq, user_id, session_id, turn_id, scene, provider, occurred_at) -> None`
  - `failure_count`（property，供监控）

**设计红线**
- 三个方法**都不抛异常**。记账是旁路，主链路（对话、通话）不能因为记账挂了而失败（spec §6.3）。
- 反向要求：失败必须可观测——`failure_count` 计数 + `logger.exception`。静默漏数比报错更糟。
- 类型不从 `app.agents.llm` 导入。`app/usage/` 不依赖 `app/agents/`，token 数以 `int | None` 传入，避免包间循环依赖。
- 状态判定优先级：**量全缺 → `usage_missing`（不查价）**；有量但缺价 → `unpriced`；缺汇率 → `unpriced`。

- [ ] **Step 1: 写失败测试**

```python
# tests/usage/test_usage_recorder.py
from datetime import UTC, date, datetime
from decimal import Decimal

from app.usage.fx import FxRate, FxTable
from app.usage.models import UsageRecord
from app.usage.pricing import PriceBook, PriceEntry
from app.usage.recorder import UsageRecorder
from app.usage.store import InMemoryUsageStore

NOW = datetime(2026, 9, 10, tzinfo=UTC)
FROM = datetime(2026, 1, 1, tzinfo=UTC)


def _llm_book() -> PriceBook:
    return PriceBook(
        [
            PriceEntry("anthropic", "llm", "input_tokens", Decimal("15"), "USD", FROM, "v1"),
            PriceEntry("anthropic", "llm", "output_tokens", Decimal("75"), "USD", FROM, "v1"),
        ]
    )


def _volc_book() -> PriceBook:
    return PriceBook(
        [PriceEntry("volc", "voice", "duration_minute", Decimal("0.6"), "CNY", FROM, "v1")]
    )


def _cny_table() -> FxTable:
    return FxTable(
        [FxRate(rate_date=date(2026, 9, 9), currency="CNY", rate_to_usd=Decimal("0.14"), source="ecb")]
    )


async def test_record_llm_prices_and_stores() -> None:
    store = InMemoryUsageStore()
    recorder = UsageRecorder(store, _llm_book(), FxTable([]))
    await recorder.record_llm(
        turn_id="turn_1", step_index=0, user_id="u1", session_id="s1", agent_id="a1",
        scene="chat", provider="anthropic", model="claude-opus-5",
        input_tokens=1000, output_tokens=200, cache_read_tokens=0, cache_write_tokens=0,
        occurred_at=NOW,
    )
    rows = await store.list_for_user("u1", NOW, datetime(2026, 9, 11, tzinfo=UTC))
    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].amount_usd == Decimal("0.03000000")
    assert rows[0].turn_id == "turn_1"
    assert rows[0].dedup_key == "llm:turn_1:0"
    assert rows[0].price_version == "v1"


async def test_record_llm_with_no_usage_is_usage_missing_not_zero() -> None:
    """拿不到 token 数时照样落流水，标 usage_missing、金额留空。
    漏掉这条会让 LLM 成本被系统性低估（spec §7.2）。"""
    store = InMemoryUsageStore()
    recorder = UsageRecorder(store, _llm_book(), FxTable([]))
    await recorder.record_llm(
        turn_id="turn_1", step_index=0, user_id="u1", session_id="s1", agent_id=None,
        scene="chat", provider="anthropic", model="claude-opus-5",
        input_tokens=None, output_tokens=None, cache_read_tokens=None, cache_write_tokens=None,
        occurred_at=NOW,
    )
    row = (await store.list_for_user("u1", NOW, datetime(2026, 9, 11, tzinfo=UTC)))[0]
    assert row.status == "usage_missing"
    assert row.amount_usd is None
    assert row.amount_original is None


async def test_record_llm_without_price_is_unpriced() -> None:
    store = InMemoryUsageStore()
    recorder = UsageRecorder(store, PriceBook([]), FxTable([]))
    await recorder.record_llm(
        turn_id="t", step_index=0, user_id="u1", session_id=None, agent_id=None,
        scene="chat", provider="anthropic", model="unknown-model",
        input_tokens=10, output_tokens=1, cache_read_tokens=None, cache_write_tokens=None,
        occurred_at=NOW,
    )
    row = (await store.list_for_user("u1", NOW, datetime(2026, 9, 11, tzinfo=UTC)))[0]
    assert row.status == "unpriced"
    assert row.amount_usd is None


async def test_record_voice_converts_cny_to_usd_with_snapshot() -> None:
    store = InMemoryUsageStore()
    recorder = UsageRecorder(store, _volc_book(), _cny_table())
    await recorder.record_voice(
        session_id="s1", user_id="u1", provider="volc",
        duration_seconds=Decimal("90"), audio_tokens=None, text_tokens=None, occurred_at=NOW,
    )
    row = (await store.list_for_user("u1", NOW, datetime(2026, 9, 11, tzinfo=UTC)))[0]
    assert row.amount_original == Decimal("0.90000000")
    assert row.currency_original == "CNY"
    assert row.fx_rate_snapshot == Decimal("0.14")
    assert row.amount_usd == Decimal("0.12600000")
    assert row.dedup_key == "voice:s1"
    assert row.kind == "voice"


async def test_record_voice_without_fx_rate_is_unpriced() -> None:
    """有原币金额但没汇率时不能瞎折算，也不能当 0（spec §4.2）。"""
    store = InMemoryUsageStore()
    recorder = UsageRecorder(store, _volc_book(), FxTable([]))
    await recorder.record_voice(
        session_id="s1", user_id="u1", provider="volc",
        duration_seconds=Decimal("90"), audio_tokens=None, text_tokens=None, occurred_at=NOW,
    )
    row = (await store.list_for_user("u1", NOW, datetime(2026, 9, 11, tzinfo=UTC)))[0]
    assert row.status == "unpriced"
    assert row.amount_usd is None
    assert row.amount_original == Decimal("0.90000000")  # 原币金额仍留档，便于事后补算


async def test_record_external_api_counts_one_call() -> None:
    store = InMemoryUsageStore()
    book = PriceBook(
        [PriceEntry("tavily", "external_api", "call", Decimal("0.008"), "USD", FROM, "v1")]
    )
    recorder = UsageRecorder(store, book, FxTable([]))
    await recorder.record_external_api(
        trace_id="trc_1", tool_name="web_search", seq=0, user_id="u1", session_id="s1",
        turn_id="turn_1", scene="chat", provider="tavily", occurred_at=NOW,
    )
    row = (await store.list_for_user("u1", NOW, datetime(2026, 9, 11, tzinfo=UTC)))[0]
    assert row.quantity.call_count == 1
    assert row.amount_usd == Decimal("0.00800000")
    assert row.dedup_key == "api:trc_1:web_search:0"


async def test_store_failure_never_propagates_but_is_counted() -> None:
    """记账挂了绝不能把对话或通话带崩（spec §6.3）。"""

    class BoomStore:
        async def append(self, record: UsageRecord) -> bool:
            raise RuntimeError("db down")

    recorder = UsageRecorder(BoomStore(), _llm_book(), FxTable([]))  # type: ignore[arg-type]
    await recorder.record_llm(
        turn_id="t", step_index=0, user_id="u1", session_id=None, agent_id=None,
        scene="chat", provider="anthropic", model="claude-opus-5",
        input_tokens=1, output_tokens=1, cache_read_tokens=None, cache_write_tokens=None,
        occurred_at=NOW,
    )
    assert recorder.failure_count == 1


async def test_missing_user_id_is_recorded_as_unknown_user() -> None:
    """user_id 缺失时不能丢掉这笔成本，落到 unknown 用户名下并标 usage_missing，
    这样报表里看得见（spec §11 风险表）。"""
    store = InMemoryUsageStore()
    recorder = UsageRecorder(store, _llm_book(), FxTable([]))
    await recorder.record_llm(
        turn_id="t", step_index=0, user_id="", session_id=None, agent_id=None,
        scene="chat", provider="anthropic", model="claude-opus-5",
        input_tokens=1000, output_tokens=200, cache_read_tokens=None, cache_write_tokens=None,
        occurred_at=NOW,
    )
    rows = await store.list_for_user("unknown", NOW, datetime(2026, 9, 11, tzinfo=UTC))
    assert len(rows) == 1
    assert rows[0].status == "usage_missing"


async def test_duplicate_record_does_not_raise() -> None:
    store = InMemoryUsageStore()
    recorder = UsageRecorder(store, _volc_book(), _cny_table())
    for _ in range(2):
        await recorder.record_voice(
            session_id="s1", user_id="u1", provider="volc",
            duration_seconds=Decimal("60"), audio_tokens=None, text_tokens=None, occurred_at=NOW,
        )
    assert recorder.failure_count == 0
    assert len(await store.list_for_user("u1", NOW, datetime(2026, 9, 11, tzinfo=UTC))) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/usage/test_usage_recorder.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.usage.recorder'`

- [ ] **Step 3: 写最小实现**

```python
# app/usage/recorder.py
"""记账门面。三条业务链路只调这里，不直接碰 store 与 pricing。

**这三个方法永远不抛异常**：记账是旁路，主链路不能因为它挂掉而失败
（spec §6.3）。代价是漏记会变静默，所以 failure_count 必须被监控。

本模块不导入 app.agents.* —— token 数以 int | None 传入，保持 app/usage
对业务包零依赖。
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal

from app.usage.fx import FxTable, convert_to_usd
from app.usage.models import (
    UsageKind,
    UsageQuantity,
    UsageRecord,
    UsageStatus,
    api_dedup_key,
    llm_dedup_key,
    voice_dedup_key,
)
from app.usage.pricing import PriceBook, price_quantity
from app.usage.store import UsageStore

logger = logging.getLogger(__name__)

#: user_id 缺失时的归属账户。不丢弃这笔成本，让它在报表里可见。
UNKNOWN_USER = "unknown"


def _has_any_quantity(quantity: UsageQuantity) -> bool:
    return any(
        getattr(quantity, name) is not None
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "duration_seconds",
            "call_count",
        )
    )


class UsageRecorder:
    def __init__(self, store: UsageStore, price_book: PriceBook, fx_table: FxTable) -> None:
        self._store = store
        self._price_book = price_book
        self._fx_table = fx_table
        self._failure_count = 0

    @property
    def failure_count(self) -> int:
        """记账失败次数。为 0 才说明流水是完整的。"""
        return self._failure_count

    async def _append(
        self,
        *,
        dedup_key: str,
        kind: UsageKind,
        provider: str,
        scene: str,
        quantity: UsageQuantity,
        user_id: str,
        occurred_at: datetime,
        session_id: str | None = None,
        turn_id: str | None = None,
        agent_id: str | None = None,
        model: str | None = None,
    ) -> None:
        try:
            record = self._build(
                dedup_key=dedup_key,
                kind=kind,
                provider=provider,
                scene=scene,
                quantity=quantity,
                user_id=user_id,
                occurred_at=occurred_at,
                session_id=session_id,
                turn_id=turn_id,
                agent_id=agent_id,
                model=model,
            )
            await self._store.append(record)
        except Exception:
            self._failure_count += 1
            logger.exception(
                "usage_record_failed kind=%s provider=%s user_id=%s dedup_key=%s",
                kind,
                provider,
                user_id or UNKNOWN_USER,
                dedup_key,
            )

    def _build(
        self,
        *,
        dedup_key: str,
        kind: UsageKind,
        provider: str,
        scene: str,
        quantity: UsageQuantity,
        user_id: str,
        occurred_at: datetime,
        session_id: str | None,
        turn_id: str | None,
        agent_id: str | None,
        model: str | None,
    ) -> UsageRecord:
        status: UsageStatus = "ok"
        resolved_user = user_id.strip() or UNKNOWN_USER
        if resolved_user == UNKNOWN_USER:
            status = "usage_missing"

        amount_original: Decimal | None = None
        currency_original: str | None = None
        amount_usd: Decimal | None = None
        fx_snapshot: Decimal | None = None
        price_version: str | None = None
        snapshot: dict[str, str] | None = None

        if not _has_any_quantity(quantity):
            # 量全缺就不去查价——查了也没有意义，且会误报 unpriced。
            status = "usage_missing"
        else:
            priced = price_quantity(
                quantity, self._price_book, provider=provider, kind=kind, model=model, at=occurred_at
            )
            if not priced.priced or priced.amount is None or priced.currency is None:
                if status == "ok":
                    status = "unpriced"
            else:
                amount_original = priced.amount
                currency_original = priced.currency
                price_version = priced.price_version
                snapshot = priced.snapshot
                rate = self._fx_table.rate_for(priced.currency, occurred_at)
                amount_usd = convert_to_usd(priced.amount, priced.currency, rate)
                fx_snapshot = None if rate is None else rate.rate_to_usd
                if amount_usd is None and status == "ok":
                    # 有原币金额但没汇率：原币留档，USD 留空，等汇率补齐后可重算。
                    status = "unpriced"

        return UsageRecord(
            dedup_key=dedup_key,
            occurred_at=occurred_at,
            recorded_at=occurred_at,
            user_id=resolved_user,
            kind=kind,
            provider=provider,
            scene=scene,
            quantity=quantity,
            session_id=session_id,
            turn_id=turn_id,
            agent_id=agent_id,
            model=model,
            price_version=price_version,
            unit_price_snapshot=snapshot,
            amount_original=amount_original,
            currency_original=currency_original,
            fx_rate_snapshot=fx_snapshot,
            amount_usd=amount_usd,
            status=status,
        )

    async def record_llm(
        self,
        *,
        turn_id: str,
        step_index: int,
        user_id: str,
        session_id: str | None,
        agent_id: str | None,
        scene: str,
        provider: str,
        model: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cache_read_tokens: int | None,
        cache_write_tokens: int | None,
        occurred_at: datetime,
    ) -> None:
        await self._append(
            dedup_key=llm_dedup_key(turn_id, step_index),
            kind="llm",
            provider=provider,
            scene=scene,
            quantity=UsageQuantity(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            ),
            user_id=user_id,
            occurred_at=occurred_at,
            session_id=session_id,
            turn_id=turn_id,
            agent_id=agent_id,
            model=model,
        )

    async def record_voice(
        self,
        *,
        session_id: str,
        user_id: str,
        provider: str,
        duration_seconds: Decimal | None,
        audio_tokens: int | None,
        text_tokens: int | None,
        occurred_at: datetime,
    ) -> None:
        await self._append(
            dedup_key=voice_dedup_key(session_id),
            kind="voice",
            provider=provider,
            scene="voice",
            quantity=UsageQuantity(
                duration_seconds=duration_seconds,
                input_tokens=audio_tokens,
                output_tokens=text_tokens,
            ),
            user_id=user_id,
            occurred_at=occurred_at,
            session_id=session_id,
        )

    async def record_external_api(
        self,
        *,
        trace_id: str,
        tool_name: str,
        seq: int,
        user_id: str,
        session_id: str | None,
        turn_id: str | None,
        scene: str,
        provider: str,
        occurred_at: datetime,
    ) -> None:
        await self._append(
            dedup_key=api_dedup_key(trace_id, tool_name, seq),
            kind="external_api",
            provider=provider,
            scene=scene,
            quantity=UsageQuantity(call_count=1),
            user_id=user_id,
            occurred_at=occurred_at,
            session_id=session_id,
            turn_id=turn_id,
            model=tool_name,
        )
```

> `record_voice` 把 audio/text token 借用 `input_tokens` / `output_tokens` 两列存放。
> 语音的 token 计价 metric 因此也是 `input_tokens` / `output_tokens`，配价时注意
> `kind="voice"` 与 `kind="llm"` 是两套独立条目，不会互相命中。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/usage/ -v && ruff check app/usage tests/usage && mypy app/usage`
Expected: 全部 passed（含前 5 个 Task 的用例），ruff 与 mypy 无输出

- [ ] **Step 5: 提交**

```bash
git add app/usage/recorder.py tests/usage/test_usage_recorder.py
git commit -m "feat(usage): 记账门面，失败不影响主链路且可计数"
```

---

### Task 7: 接入语音结算

**Files:**
- Modify: `app/api/voice.py`（在既有 `settle` 调用之后追加记账，不改既有逻辑）
- Test: `tests/usage/test_usage_voice_wiring.py`

**Interfaces:**
- Consumes: Task 6 的 `UsageRecorder.record_voice`
- Produces: 无新接口

**注意**
- **不改** `compute_billed_units` 与 `VoiceUsageStore`，不动既有 `billed_units` 返回值。语音配额逻辑保持原样（spec D10）。
- 只在 `settlement.duplicate is False` 时记账——重复上报本就不该产生第二笔成本。
- `container.usage_recorder` 为 None 时整段跳过，保证既有测试与 fake 模式不受影响。

- [ ] **Step 1: 写失败测试**

```python
# tests/usage/test_usage_voice_wiring.py
"""语音结算接入用量记账的接线回归。

关键点：记账不得改变 /settle 既有的响应体与幂等语义。
"""
from decimal import Decimal

from app.usage.store import InMemoryUsageStore


async def test_settle_records_voice_usage_once(voice_settle_client) -> None:
    """同一 session 重复 settle 只产生一条用量流水。"""
    client, store = voice_settle_client
    body = {
        "session_id": "sess_v1",
        "user_id": "u1",
        "provider": "volc",
        "duration_seconds": 90,
        "usage_tokens": {"audio_tokens": 100, "text_tokens": 20},
    }
    first = await client.post("/api/voice/sessions/sess_v1/settle", json=body)
    second = await client.post("/api/voice/sessions/sess_v1/settle", json=body)
    assert first.status_code == 200
    assert second.status_code == 200
    rows = await store.list_for_user("u1", MIN_TIME, MAX_TIME)
    assert len(rows) == 1
    assert rows[0].kind == "voice"
    assert rows[0].quantity.duration_seconds == Decimal("90")


async def test_settle_response_unchanged_when_recorder_absent(voice_settle_client_no_recorder) -> None:
    """recorder 为 None 时 /settle 行为与接入前完全一致。"""
    client = voice_settle_client_no_recorder
    resp = await client.post(
        "/api/voice/sessions/sess_v2/settle",
        json={
            "session_id": "sess_v2",
            "user_id": "u1",
            "provider": "volc",
            "duration_seconds": 30,
            "usage_tokens": {"audio_tokens": 0, "text_tokens": 0},
        },
    )
    assert resp.status_code == 200
    assert "billed_units" in resp.json()


async def test_settle_succeeds_even_if_recording_fails(voice_settle_client_broken_recorder) -> None:
    """记账挂了不能让用户挂断失败（spec §6.3）。"""
    client = voice_settle_client_broken_recorder
    resp = await client.post(
        "/api/voice/sessions/sess_v3/settle",
        json={
            "session_id": "sess_v3",
            "user_id": "u1",
            "provider": "volc",
            "duration_seconds": 30,
            "usage_tokens": {"audio_tokens": 0, "text_tokens": 0},
        },
    )
    assert resp.status_code == 200
```

> **实现者注意**：三个 fixture（`voice_settle_client` / `voice_settle_client_no_recorder`
> / `voice_settle_client_broken_recorder`）与 `MIN_TIME` / `MAX_TIME` 需按
> `tests/test_voice_lifecycle.py` 里既有的 client 搭建方式写在本测试文件顶部。
> 先读那个文件确认 `/settle` 的真实路由前缀、请求体字段名与鉴权头，**照抄它的搭建方式**，
> 不要自创。若既有测试用的是同步 `TestClient`，本文件也用同一种，别混用。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/usage/test_usage_voice_wiring.py -v`
Expected: FAIL（fixture 未定义 / 未记账）

- [ ] **Step 3: 写最小实现**

在 `app/api/voice.py` 中，紧跟既有的 `settlement = await container.voice_usage_store.settle(...)`
之后、既有 `if summary and not settlement.duplicate:` 之前，插入：

```python
    recorder = getattr(container, "usage_recorder", None)
    if recorder is not None and not settlement.duplicate:
        # 成本记账与配额结算是两套口径：这里存原始时长与 token，不存 billed_units。
        await recorder.record_voice(
            session_id=settlement.record.session_id,
            user_id=settlement.record.user_id,
            provider=settlement.record.provider,
            duration_seconds=Decimal(str(settlement.record.duration_seconds)),
            audio_tokens=settlement.record.audio_tokens,
            text_tokens=settlement.record.text_tokens,
            occurred_at=settlement.record.recorded_at,
        )
```

若 `app/api/voice.py` 顶部尚未导入 `Decimal`，加 `from decimal import Decimal`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/usage/ tests/test_voice_lifecycle.py -v && ruff check . && mypy app`
Expected: 全部 passed。**既有语音测试必须一条不红**——红了说明改动越界了，回退重做。

- [ ] **Step 5: 提交**

```bash
git add app/api/voice.py tests/usage/test_usage_voice_wiring.py
git commit -m "feat(usage): 语音结算接入用量记账"
```

---

### Task 8: 接入 LLM 记账与 turn_id

**Files:**
- Modify: `app/agents/llm.py`（`LLMResult` 加一个带默认值的字段；`_to_result` 填充它）
- Modify: `app/agents/runner.py`（`__init__` 加缺省参数、`_run_turn_events` 生成 turn_id、消费 `LLMStreamDone` 处记账）
- Test: `tests/usage/test_usage_llm_wiring.py`

**Interfaces:**
- Consumes: Task 6 的 `UsageRecorder.record_llm`
- Produces: `LLMResult.response_model: str | None`；`AgentRunner.__init__(..., usage_recorder: Any = None)`

**为什么这样改是安全的**
- `LLMResult` 新字段带默认值 → 现有全部构造点（含 `FakeLLM`、大量测试）零影响。
- `_to_result` 内部用既有的 `getattr(response, "model_name", None)` 取值，**不改它的两个调用点**。
- `runner.__init__` 加 `usage_recorder: Any = None`，与既有 `session_store` / `conversation_store` / `conversation_titler` 同一模式——那几个的注释已写明「缺省 None 时不启用，仓库里大量直接构造 runner 的测试因此不受影响」。
- `turn_id` 是 `_run_turn_events` 的**局部变量**，不进任何签名。

- [ ] **Step 1: 写失败测试**

```python
# tests/usage/test_usage_llm_wiring.py
"""LLM 用量记账接线。

三件事必须锁住：
1. 一轮内多步 LLM 调用都记账，且同属一个 turn_id（S4「每次聊天成本」靠它归并）；
2. usage 为 None 时落 usage_missing，不静默漏掉；
3. recorder 为 None 时 runner 行为与接入前完全一致。
"""
from datetime import UTC, datetime
from decimal import Decimal

from app.agents.llm import LLMResult, LLMUsage
from app.usage.fx import FxTable
from app.usage.pricing import PriceBook, PriceEntry
from app.usage.recorder import UsageRecorder
from app.usage.store import InMemoryUsageStore

NOW = datetime(2026, 9, 10, tzinfo=UTC)
WINDOW = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))


def _book() -> PriceBook:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    return PriceBook(
        [
            PriceEntry("anthropic", "llm", "input_tokens", Decimal("15"), "USD", at, "v1"),
            PriceEntry("anthropic", "llm", "output_tokens", Decimal("75"), "USD", at, "v1"),
        ]
    )


def test_llm_result_carries_response_model_with_safe_default() -> None:
    """新字段必须有默认值，否则现存所有 LLMResult 构造点都会炸。"""
    assert LLMResult(text="hi").response_model is None
    assert LLMResult(text="hi", response_model="claude-opus-5").response_model == "claude-opus-5"


async def test_single_turn_llm_usage_is_recorded(runner_with_recorder) -> None:
    runner, store, _ = runner_with_recorder(
        [LLMResult(text="答案", usage=LLMUsage(input_tokens=1000, output_tokens=200),
                   response_model="claude-opus-5")]
    )
    await runner.run_turn(SESSION, "问题", "trc_1", NOW)
    rows = await store.list_for_user("u1", *WINDOW)
    assert len(rows) == 1
    assert rows[0].kind == "llm"
    assert rows[0].scene == "chat"
    assert rows[0].model == "claude-opus-5"
    assert rows[0].amount_usd == Decimal("0.03000000")
    assert rows[0].turn_id is not None


async def test_multi_step_turn_shares_one_turn_id(runner_with_recorder) -> None:
    """一轮里模型先调工具、再作答 = 两次 LLM 调用，必须归并到同一个 turn_id，
    否则「一次聊天多少钱」算出来的是「一次 LLM 调用多少钱」。"""
    runner, store, _ = runner_with_recorder(
        [
            LLMResult(text="", tool_calls=(TOOL_CALL,),
                      usage=LLMUsage(input_tokens=500, output_tokens=50),
                      response_model="claude-opus-5"),
            LLMResult(text="答案", usage=LLMUsage(input_tokens=800, output_tokens=100),
                      response_model="claude-opus-5"),
        ]
    )
    await runner.run_turn(SESSION, "查一下行情", "trc_2", NOW)
    rows = await store.list_for_user("u1", *WINDOW)
    llm_rows = [r for r in rows if r.kind == "llm"]
    assert len(llm_rows) == 2
    assert len({r.turn_id for r in llm_rows}) == 1
    assert {r.dedup_key for r in llm_rows} == {
        f"llm:{llm_rows[0].turn_id}:0",
        f"llm:{llm_rows[0].turn_id}:1",
    }


async def test_turn_ids_differ_across_turns(runner_with_recorder) -> None:
    runner, store, _ = runner_with_recorder(
        [
            LLMResult(text="a", usage=LLMUsage(input_tokens=10, output_tokens=1)),
            LLMResult(text="b", usage=LLMUsage(input_tokens=10, output_tokens=1)),
        ]
    )
    await runner.run_turn(SESSION, "一", "trc_a", NOW)
    await runner.run_turn(SESSION, "二", "trc_b", NOW)
    rows = await store.list_for_user("u1", *WINDOW)
    assert len({r.turn_id for r in rows}) == 2


async def test_missing_usage_is_recorded_as_usage_missing(runner_with_recorder) -> None:
    """FakeLLM 的 usage 恒为 None（app/agents/llm.py:70）。这类调用照样要留痕。"""
    runner, store, _ = runner_with_recorder([LLMResult(text="答案", usage=None)])
    await runner.run_turn(SESSION, "问题", "trc_3", NOW)
    rows = await store.list_for_user("u1", *WINDOW)
    assert len(rows) == 1
    assert rows[0].status == "usage_missing"
    assert rows[0].amount_usd is None


async def test_runner_without_recorder_behaves_identically(runner_without_recorder) -> None:
    """回归：不注入 recorder 时不得有任何行为变化。"""
    runner = runner_without_recorder([LLMResult(text="答案")])
    result = await runner.run_turn(SESSION, "问题", "trc_4", NOW)
    assert result.text == "答案"


async def test_turn_survives_recorder_failure(runner_with_broken_recorder) -> None:
    """记账挂了不能把对话带崩（spec §6.3）。"""
    runner = runner_with_broken_recorder([LLMResult(text="答案")])
    result = await runner.run_turn(SESSION, "问题", "trc_5", NOW)
    assert result.text == "答案"
```

> **实现者注意**：`SESSION`、`TOOL_CALL` 与四个 fixture 需写在本文件顶部。
> 先读 `tests/test_agent_runner.py` 与 `tests/test_runner_streaming.py`，
> **照抄它们构造 `Session` / `AgentRunner` / 脚本化 LLM 的既有方式**，
> 只额外传 `usage_recorder=UsageRecorder(InMemoryUsageStore(), _book(), FxTable([]))`。
> `SESSION.user_id` 必须是 `"u1"`。别自创新的搭建方式。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/usage/test_usage_llm_wiring.py -v`
Expected: FAIL — `LLMResult` 无 `response_model` 字段

- [ ] **Step 3: 写最小实现**

**3a.** `app/agents/llm.py` 的 `LLMResult`（约 65 行），在 `usage` 字段之后追加：

```python
    # 实际响应的模型名。按模型定价必须知道真实值：网关有多模型与 fallback，
    # config 里的配置值不可靠。带默认值 → 现有构造点零影响。
    response_model: str | None = None
```

**3b.** 同文件 `_to_result`（约 336 行）的 `return LLMResult(...)`，加一行：

```python
    return LLMResult(
        text="".join(text_parts),
        tool_calls=tuple(tool_calls),
        usage=usage if usage is not None else _extract_usage(response),
        response_model=getattr(response, "model_name", None),
    )
```

`_to_result` 的两个调用点不用改。

**3c.** `app/agents/runner.py` 顶部导入区加 `import uuid`（现有导入里没有）。

**3d.** `runner.__init__` 参数表末尾加 `usage_recorder: Any = None`，并在赋值区末尾加：

```python
        # 用量与成本记账。缺省 None 时不记账，仓库里大量直接构造 runner 的测试
        # 因此不受影响（与 _session_store / _conversation_store 同一模式）。
        self._usage_recorder = usage_recorder
```

**3e.** `_run_turn_events` 的 docstring 之后、函数体第一行，加：

```python
        # 一轮的归并 id：本轮多步 LLM 调用与全部工具调用共用它，
        # 「一次聊天多少钱」靠它把一轮内的多笔成本加起来（spec D9）。
        # 刻意做成局部变量——不进任何函数签名，调用方一行都不用改。
        turn_id = "turn_" + uuid.uuid4().hex
```

**3f.** 同函数内，`if result is None: raise RuntimeError("LLM 流没有产出终止事件")` 之后插入：

```python
            await self._record_llm_usage(
                session=session, turn_id=turn_id, step_index=step, result=result, now=now
            )
```

**3g.** 在 `AgentRunner` 中新增私有方法（放在 `_stream_llm` 之后）：

```python
    async def _record_llm_usage(
        self,
        *,
        session: Session,
        turn_id: str,
        step_index: int,
        result: LLMResult,
        now: datetime,
    ) -> None:
        """把这一步的 token 消耗记进用量流水。

        usage 为 None 时也要记（recorder 会落 usage_missing）——「空着」在成本
        核算里等于免费，会让 LLM 成本被系统性低估（spec §7.2）。
        """
        if self._usage_recorder is None:
            return
        usage = result.usage
        await self._usage_recorder.record_llm(
            turn_id=turn_id,
            step_index=step_index,
            user_id=session.user_id,
            session_id=session.session_id,
            agent_id=getattr(session, "agent_id", None),
            scene="chat",
            provider=self._usage_provider,
            model=result.response_model,
            input_tokens=None if usage is None else usage.input_tokens,
            output_tokens=None if usage is None else usage.output_tokens,
            cache_read_tokens=None if usage is None else usage.cache_read_tokens,
            cache_write_tokens=None if usage is None else usage.cache_write_tokens,
            occurred_at=now,
        )
```

**3h.** `__init__` 参数表再加 `usage_provider: str = "anthropic"`，赋值 `self._usage_provider = usage_provider`。
定价按 provider 归集，容器接线时（Task 12）由配置传入真实值。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/usage/ tests/test_agent_runner.py tests/test_runner_streaming.py tests/test_llm_streaming.py tests/test_pydantic_llm.py -v`
Expected: 全部 passed

再跑全量：`python -m pytest -q && ruff check . && mypy app`
Expected: 全绿。**任何既有测试变红都说明改动越界**，回退重做，不要改既有测试来迁就。

- [ ] **Step 5: 提交**

```bash
git add app/agents/llm.py app/agents/runner.py tests/usage/test_usage_llm_wiring.py
git commit -m "feat(usage): LLM token 记账与一轮归并的 turn_id"
```

---

### Task 9: 接入外部 API 调用记账

**Files:**
- Create: `app/usage/tool_providers.py`
- Modify: `app/agents/runner.py`（`_run_tool` 内加一次记账）
- Test: `tests/usage/test_usage_tool_providers.py`, `tests/usage/test_usage_tool_wiring.py`

**Interfaces:**
- Consumes: Task 6 的 `UsageRecorder.record_external_api`
- Produces: `provider_for_tool(tool_name: str) -> str`

**口径**
- **调用发出即产生成本**：Tavily 这类按请求计费的供应商，超时与报错同样扣费。所以在工具执行的
  `finally` 里记一次 `call_count=1`，覆盖 ok / timeout / error 全部出口。
- 未在映射表里的工具记 `provider="unknown"`。新增工具漏配时会在报表里露出来，比默认归到某个
  provider 更安全。
- 内部工具记 `provider="internal"`，Core 工具记 `provider="core"`（单价 0，spec D2）。
- 只改 `_run_tool` 一处，**不动那 4 个 `record_tool_call`**。审计与用量是两件事，混在一起改
  4 个点容易漏。

- [ ] **Step 1: 写失败测试**

```python
# tests/usage/test_usage_tool_providers.py
from app.usage.tool_providers import provider_for_tool


def test_web_search_maps_to_tavily() -> None:
    assert provider_for_tool("web_search") == "tavily"


def test_core_tools_map_to_core() -> None:
    for name in (
        "get_market_price",
        "market_tickers",
        "search_market_catalog",
        "get_balances",
        "get_positions",
        "get_orders",
        "get_account_summary",
        "get_risk_summary",
        "get_trading_readiness",
        "get_transaction_history",
        "create_trade_draft",
        "create_perp_trade_draft",
        "create_spot_swap_draft",
        "agentos_get_balances",
        "agentos_get_positions",
        "agentos_get_orders",
        "agentos_create_trade_draft",
    ):
        assert provider_for_tool(name) == "core", name


def test_internal_tools_map_to_internal() -> None:
    assert provider_for_tool("update_user_preference") == "internal"
    assert provider_for_tool("ask_investment_expert") == "internal"


def test_unknown_tool_is_flagged_not_silently_bucketed() -> None:
    """新工具漏配时要在报表里看得见，不能默默归到某个 provider。"""
    assert provider_for_tool("some_new_tool_2027") == "unknown"
```

```python
# tests/usage/test_usage_tool_wiring.py
"""工具调用记账接线。"""
from datetime import UTC, datetime

NOW = datetime(2026, 9, 10, tzinfo=UTC)
WINDOW = (datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC))


async def test_successful_tool_call_is_recorded(runner_with_tool) -> None:
    runner, store = runner_with_tool(outcome="ok")
    await runner.run_turn(SESSION, "查行情", "trc_1", NOW)
    api_rows = [r for r in await store.list_for_user("u1", *WINDOW) if r.kind == "external_api"]
    assert len(api_rows) == 1
    assert api_rows[0].quantity.call_count == 1
    assert api_rows[0].turn_id is not None


async def test_failed_tool_call_is_still_recorded(runner_with_tool) -> None:
    """超时同样计费——供应商按请求收钱，不看我们有没有拿到结果。"""
    runner, store = runner_with_tool(outcome="timeout")
    await runner.run_turn(SESSION, "查行情", "trc_2", NOW)
    api_rows = [r for r in await store.list_for_user("u1", *WINDOW) if r.kind == "external_api"]
    assert len(api_rows) == 1


async def test_tool_usage_shares_turn_id_with_llm_usage(runner_with_tool) -> None:
    """同一轮的 LLM 成本与工具成本必须能加到一起（S4）。"""
    runner, store = runner_with_tool(outcome="ok")
    await runner.run_turn(SESSION, "查行情", "trc_3", NOW)
    rows = await store.list_for_user("u1", *WINDOW)
    assert len({r.turn_id for r in rows}) == 1


async def test_two_calls_of_same_tool_get_distinct_dedup_keys(runner_with_tool) -> None:
    """同名工具在一轮里被调两次，seq 必须把它们区分开，否则第二次被当重复丢掉。"""
    runner, store = runner_with_tool(outcome="ok", call_count=2)
    await runner.run_turn(SESSION, "查两个币", "trc_4", NOW)
    api_rows = [r for r in await store.list_for_user("u1", *WINDOW) if r.kind == "external_api"]
    assert len(api_rows) == 2
    assert len({r.dedup_key for r in api_rows}) == 2
```

> **实现者注意**：`runner_with_tool` 是本文件唯一的 fixture，返回一个工厂
> `(outcome: str, call_count: int = 1) -> tuple[AgentRunner, InMemoryUsageStore]`：
> `outcome="ok"` 让工具正常返回，`outcome="timeout"` 让它抛 `httpx.TimeoutException`；
> `call_count` 控制脚本化 LLM 在第一轮返回几个 `tool_calls`。
> 照 `tests/test_agent_runner.py` 的既有方式搭 `Session` / `AgentRunner` / 脚本化 LLM，
> 额外传 `usage_recorder`。`SESSION.user_id` 必须是 `"u1"`。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/usage/test_usage_tool_providers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.usage.tool_providers'`

- [ ] **Step 3: 写最小实现**

```python
# app/usage/tool_providers.py
"""工具名 → 上游 provider 的归属映射。

未登记的工具归 "unknown"：新增工具漏配时会在报表里露出来，比默默归到某个
provider 更安全（那会把成本算到错的供应商头上）。
"""

from __future__ import annotations

#: 走 Core 的工具。Core 定性为内部服务，单价暂记 0（spec D2），
#: 但流水照采，将来配上单价即可回算。
_CORE_TOOLS = frozenset(
    {
        "get_market_price",
        "market_tickers",
        "search_market_catalog",
        "get_balances",
        "get_positions",
        "get_orders",
        "get_account_summary",
        "get_risk_summary",
        "get_trading_readiness",
        "get_transaction_history",
        "create_trade_draft",
        "create_perp_trade_draft",
        "create_spot_swap_draft",
        "agentos_get_balances",
        "agentos_get_positions",
        "agentos_get_orders",
        "agentos_create_trade_draft",
    }
)

#: 不产生外部付费调用的工具（本地 PG、本地证据组装）。
_INTERNAL_TOOLS = frozenset({"update_user_preference", "ask_investment_expert"})

#: 按次计费的第三方。
_THIRD_PARTY_TOOLS = {"web_search": "tavily"}

UNKNOWN_PROVIDER = "unknown"


def provider_for_tool(tool_name: str) -> str:
    if tool_name in _THIRD_PARTY_TOOLS:
        return _THIRD_PARTY_TOOLS[tool_name]
    if tool_name in _CORE_TOOLS:
        return "core"
    if tool_name in _INTERNAL_TOOLS:
        return "internal"
    return UNKNOWN_PROVIDER
```

在 `app/agents/runner.py` 的 `_run_turn_events` 里，`turn_id` 那行之后加一个序号计数器：

```python
        # 同名工具一轮内可能被调多次，seq 用来区分，否则第二次会被当成重复丢掉。
        tool_call_seq = itertools.count()
```

顶部导入区加 `import itertools`。

然后在 `_run_tool(call)` 函数体最外层包一层 try/finally，把原有函数体整体缩进到 `try:` 下，
`finally:` 里记账：

```python
        async def _run_tool(call: Any) -> ToolOutcome:
            seq = next(tool_call_seq)
            try:
                ...  # 原有函数体，一行不改，仅整体缩进
            finally:
                # 调用发出即产生成本：Tavily 这类按请求计费的供应商，
                # 超时与报错同样扣费，所以放 finally 覆盖全部出口。
                if self._usage_recorder is not None:
                    await self._usage_recorder.record_external_api(
                        trace_id=trace_id,
                        tool_name=call.name,
                        seq=seq,
                        user_id=session.user_id,
                        session_id=session.session_id,
                        turn_id=turn_id,
                        scene="chat",
                        provider=provider_for_tool(call.name),
                        occurred_at=now,
                    )
```

导入 `from app.usage.tool_providers import provider_for_tool`。

> **缩进这一步最容易出错。** 改完先 `python -m pytest tests/test_agent_runner.py tests/test_tool_protocol.py -q`，
> 确认既有工具测试一条不红，再往下走。若 `_run_tool` 已经很长，缩进后仍保持原有逻辑不变——
> 不要顺手拆分它（本计划禁止顺手重构）。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/usage/ tests/test_agent_runner.py tests/test_tool_protocol.py tests/test_tool_registry.py -v`
Expected: 全部 passed

再跑全量：`python -m pytest -q && ruff check . && mypy app`

- [ ] **Step 5: 提交**

```bash
git add app/usage/tool_providers.py app/agents/runner.py tests/usage/test_usage_tool_providers.py tests/usage/test_usage_tool_wiring.py
git commit -m "feat(usage): 外部 API 调用记账与工具 provider 归属"
```

---

### Task 10: 语音时长服务端兜底（默认关闭）

**Files:**
- Create: `app/usage/voice_duration.py`
- Modify: `app/config.py`（两个事件类型配置项 + 一个开关）
- Test: `tests/usage/test_usage_voice_duration.py`

**Interfaces:**
- Consumes: 无
- Produces: `RoomWindow`、`derive_duration_seconds(join_at, leave_at) -> Decimal | None`、
  `DurationReconciliation`、`reconcile_duration(client_seconds, server_seconds, *, tolerance_ratio) -> DurationReconciliation`

**为什么默认关闭**

火山回调的 `EventType` 取值在本仓库代码里**没有枚举**——`app/voice/volc_callbacks.py`
是 schema-agnostic 的宽容解析（用 `_shape_hash` 记录 schema 形状）。进房/退房对应哪些
`EventType` 字面量必须在 staging 用真实回调确认，不能在代码里写猜测的魔法值。

所以本 Task 只交付**纯函数 + 配置位**：事件类型由配置注入，配置为空时兜底不生效，
`/settle` 的客户端上报口径保持现状。实测确认取值后改配置即可启用，无需改代码。

**本 Task 明确不做**（留给实测后的独立改动）：把 `derive_duration_seconds` 接到
真实回调入口。接线前必须先有 staging 的真实事件样本。

- [ ] **Step 1: 写失败测试**

```python
# tests/usage/test_usage_voice_duration.py
from datetime import UTC, datetime
from decimal import Decimal

from app.usage.voice_duration import (
    RoomWindow,
    derive_duration_seconds,
    reconcile_duration,
)

T = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


def test_duration_from_join_and_leave() -> None:
    assert derive_duration_seconds(RoomWindow(join_at=T, leave_at=T.replace(second=30))) == Decimal("30.000")


def test_missing_leave_event_yields_none() -> None:
    """只进房没退房（回调丢了）时返回 None，由调用方决定退回客户端口径。"""
    assert derive_duration_seconds(RoomWindow(join_at=T, leave_at=None)) is None


def test_missing_join_event_yields_none() -> None:
    assert derive_duration_seconds(RoomWindow(join_at=None, leave_at=T)) is None


def test_negative_window_yields_none_not_negative_duration() -> None:
    """乱序回调不能算出负时长——那会变成负成本。"""
    assert derive_duration_seconds(RoomWindow(join_at=T.replace(second=30), leave_at=T)) is None


def test_server_duration_wins_when_within_tolerance() -> None:
    r = reconcile_duration(Decimal("60"), Decimal("62"), tolerance_ratio=Decimal("0.1"))
    assert r.duration_seconds == Decimal("62")
    assert r.suspicious is False
    assert r.source == "server"


def test_client_underreport_beyond_tolerance_is_flagged() -> None:
    """客户端少报很多 = 疑似漏报（崩溃、被杀进程）。取服务端值并标记。"""
    r = reconcile_duration(Decimal("5"), Decimal("120"), tolerance_ratio=Decimal("0.1"))
    assert r.duration_seconds == Decimal("120")
    assert r.suspicious is True
    assert r.source == "server"


def test_falls_back_to_client_when_server_unavailable() -> None:
    """兜底关闭或回调缺失时退回客户端口径，行为与接入前一致。"""
    r = reconcile_duration(Decimal("60"), None, tolerance_ratio=Decimal("0.1"))
    assert r.duration_seconds == Decimal("60")
    assert r.source == "client"
    assert r.suspicious is False


def test_no_duration_at_all_yields_none() -> None:
    r = reconcile_duration(None, None, tolerance_ratio=Decimal("0.1"))
    assert r.duration_seconds is None
    assert r.source == "none"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/usage/test_usage_voice_duration.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.usage.voice_duration'`

- [ ] **Step 3: 写最小实现**

```python
# app/usage/voice_duration.py
"""语音通话时长的服务端兜底推导（spec §7.1 / D1）。

数据面直连，Runtime 看不到音频流，时长本来只能靠客户端挂断时上报。客户端崩溃、
被系统杀进程、断网未重试，这通电话就变成零成本——做单用户抄表账单时这类漏报
直接落在具体用户头上，不是宏观平均能摊掉的误差。

本模块只做纯推导。事件类型字面量由配置注入（火山的 EventType 取值必须在 staging
用真实回调确认），配置为空时兜底不生效。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

DurationSource = Literal["server", "client", "none"]

#: 时长保留 3 位小数，与 usage_ledger.duration_seconds 的 Numeric(scale=3) 对齐。
_DURATION_EXPONENT = Decimal("0.001")


@dataclass(frozen=True)
class RoomWindow:
    join_at: datetime | None
    leave_at: datetime | None


@dataclass(frozen=True)
class DurationReconciliation:
    duration_seconds: Decimal | None
    source: DurationSource
    #: 客户端与服务端差得过多。取服务端值，同时告警。
    suspicious: bool


def derive_duration_seconds(window: RoomWindow) -> Decimal | None:
    """由进房/退房时间算时长。任一端缺失或时间乱序都返回 None。"""
    if window.join_at is None or window.leave_at is None:
        return None
    delta = (window.leave_at - window.join_at).total_seconds()
    if delta < 0:
        # 乱序回调算出负时长会变成负成本，宁可当作拿不到。
        return None
    return Decimal(str(delta)).quantize(_DURATION_EXPONENT)


def reconcile_duration(
    client_seconds: Decimal | None,
    server_seconds: Decimal | None,
    *,
    tolerance_ratio: Decimal,
) -> DurationReconciliation:
    """服务端时长优先，客户端上报作交叉校验（spec §7.1）。"""
    if server_seconds is None:
        if client_seconds is None:
            return DurationReconciliation(None, "none", False)
        return DurationReconciliation(client_seconds, "client", False)

    suspicious = False
    if client_seconds is not None and server_seconds > 0:
        drift = abs(server_seconds - client_seconds) / server_seconds
        suspicious = drift > tolerance_ratio
    elif client_seconds is None:
        suspicious = True
    return DurationReconciliation(server_seconds, "server", suspicious)
```

`app/config.py` 在既有 `voice_quota_units_per_user` 之后加：

```python
    # 语音时长服务端兜底（spec §7.1 / D1）。火山回调的 EventType 取值需在 staging
    # 用真实回调确认，故默认留空 = 兜底关闭，此时时长仍只取客户端上报。
    voice_room_join_event_types: tuple[str, ...] = ()
    voice_room_leave_event_types: tuple[str, ...] = ()
    voice_duration_drift_tolerance: float = Field(default=0.1, ge=0.0, le=1.0)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/usage/test_usage_voice_duration.py -v && python -m pytest tests/test_volc_callback.py -q && ruff check . && mypy app`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add app/usage/voice_duration.py app/config.py tests/usage/test_usage_voice_duration.py
git commit -m "feat(usage): 语音时长服务端兜底推导与交叉校验（默认关闭）"
```

---

### Task 11: 查询接口

**Files:**
- Create: `app/api/usage.py`
- Modify: `app/main.py`（注册 router）
- Test: `tests/api/test_usage_api.py`

**Interfaces:**
- Consumes: Task 4/5 的 `UsageStore` 查询方法
- Produces: 五个端点
  - `GET /internal/usage/users/{user_id}/bill?from=&to=`
  - `GET /internal/usage/users/{user_id}/ledger?from=&to=&kind=&limit=&offset=`
  - `GET /internal/usage/summary?group_by=&from=&to=`
  - `GET /internal/usage/unit-economics?from=&to=`
  - `GET /internal/usage/top-users?from=&to=&limit=`

**要求**
- 账期一律左闭右开，`from` / `to` 为必填 ISO 8601。缺参返回 422（FastAPI 默认）。
- 所有响应带 `data_quality`（`usage_missing_count` / `unpriced_count` / `record_count`），
  报表不带质量信息就是在给假数（spec §7.2）。
- 账单响应带 `revision` 字段（spec D8）。一期取值恒为 1，字段先占位以便调用方
  从第一天起就不缓存无版本号的账单。
- `group_by` 必须白名单校验（`PostgresUsageStore.aggregate` 已挡，API 层给出 400 而非 500）。
- 单位经济学的「一次聊天」按 `turn_id` 归并，「一分钟通话」按 `duration_seconds` 归一。
- 鉴权照抄 `app/api/strategy.py` 的既有内部鉴权依赖，不自创。

- [ ] **Step 1: 写失败测试**

```python
# tests/api/test_usage_api.py
"""用量查询接口。"""


async def test_user_bill_returns_totals_and_quality(usage_api_client) -> None:
    client, store = usage_api_client
    await seed_records(store)
    resp = await client.get(
        "/internal/usage/users/u1/bill",
        params={"from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == "u1"
    assert body["total_amount_usd"] == "3.00000000"
    assert body["by_kind"]["llm"] == "1.00000000"
    assert body["data_quality"]["usage_missing_count"] == 1
    assert body["revision"] == 1


async def test_user_bill_requires_period(usage_api_client) -> None:
    client, _ = usage_api_client
    assert (await client.get("/internal/usage/users/u1/bill")).status_code == 422


async def test_user_bill_of_unknown_user_is_empty_not_404(usage_api_client) -> None:
    """没有用量的用户返回零账单——404 会让调用方无法区分「没用」和「查错了」。"""
    client, _ = usage_api_client
    resp = await client.get(
        "/internal/usage/users/nobody/bill",
        params={"from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"},
    )
    assert resp.status_code == 200
    assert resp.json()["total_amount_usd"] == "0.00000000"


async def test_ledger_paginates_and_filters_by_kind(usage_api_client) -> None:
    client, store = usage_api_client
    await seed_records(store)
    resp = await client.get(
        "/internal/usage/users/u1/ledger",
        params={"from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z", "kind": "llm", "limit": 1},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "llm"
    assert "unit_price_snapshot" in items[0]  # 可复算：单价快照必须能下钻到


async def test_summary_groups_by_whitelisted_dimensions(usage_api_client) -> None:
    client, store = usage_api_client
    await seed_records(store)
    resp = await client.get(
        "/internal/usage/summary",
        params={"from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z", "group_by": "scene,provider"},
    )
    assert resp.status_code == 200
    assert resp.json()["rows"]


async def test_summary_rejects_unknown_group_by_with_400(usage_api_client) -> None:
    client, _ = usage_api_client
    resp = await client.get(
        "/internal/usage/summary",
        params={"from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z", "group_by": "secret_column"},
    )
    assert resp.status_code == 400


async def test_unit_economics_answers_the_three_questions(usage_api_client) -> None:
    """甲方问的就是这三个数：每分钟通话、每次聊天、每次分析。"""
    client, store = usage_api_client
    await seed_records(store)
    resp = await client.get(
        "/internal/usage/unit-economics",
        params={"from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["units"]) >= {"voice_per_minute", "chat_per_turn", "analysis_per_call"}
    voice = body["units"]["voice_per_minute"]
    assert {"mean", "p50", "p95", "sample_size"} <= set(voice)
    assert body["data_quality"]["usage_missing_ratio"] is not None


async def test_unit_economics_with_no_samples_reports_zero_not_error(usage_api_client) -> None:
    client, _ = usage_api_client
    resp = await client.get(
        "/internal/usage/unit-economics",
        params={"from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"},
    )
    assert resp.status_code == 200
    assert resp.json()["units"]["chat_per_turn"]["sample_size"] == 0


async def test_top_users_orders_desc(usage_api_client) -> None:
    client, store = usage_api_client
    await seed_records(store)
    resp = await client.get(
        "/internal/usage/top-users",
        params={"from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z", "limit": 2},
    )
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert [r["user_id"] for r in rows] == ["u2", "u1"]


async def test_amounts_are_serialized_as_strings(usage_api_client) -> None:
    """金额走字符串，不走 JSON number——float 化会吃掉 token 级精度。"""
    client, store = usage_api_client
    await seed_records(store)
    resp = await client.get(
        "/internal/usage/users/u1/bill",
        params={"from": "2026-09-01T00:00:00Z", "to": "2026-10-01T00:00:00Z"},
    )
    assert isinstance(resp.json()["total_amount_usd"], str)
```

> **实现者注意**：`usage_api_client` fixture 与 `seed_records` 写在文件顶部。
> 照抄 `tests/api/` 下既有测试的 app 搭建与鉴权方式。`seed_records` 需造出：
> u1 的 llm 流水 1 USD、u1 的 voice 流水 2 USD（60 秒）、u1 的 usage_missing 一条、
> u2 的流水 9 USD。

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/api/test_usage_api.py -v`
Expected: FAIL — 404（路由未注册）

- [ ] **Step 3: 写最小实现**

```python
# app/api/usage.py
"""内部用量与成本查询。

只对内，不对外暴露；鉴权沿用既有内部依赖。
金额一律以字符串返回——JSON number 会被 float 化，吃掉 token 级精度。
账期左闭右开 [from, to)，相邻两月不双算。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.usage.models import UsageRecord
from app.usage.store import UsageAggregate, UserBill

router = APIRouter(tags=["usage"])

_GROUPABLE = ("kind", "provider", "model", "scene", "user_id", "status")
_ZERO = Decimal("0.00000000")


def _amount(value: Decimal | None) -> str:
    return str((value or Decimal(0)).quantize(_ZERO))


def _store(request: Request) -> Any:
    store = getattr(request.app.state.container, "usage_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="用量存储未启用")
    return store


def _percentile(values: list[Decimal], ratio: float) -> str:
    if not values:
        return _amount(None)
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * ratio))
    return _amount(ordered[index])


def _mean(values: list[Decimal]) -> str:
    if not values:
        return _amount(None)
    return _amount(sum(values, start=Decimal(0)) / Decimal(len(values)))


def _unit_block(values: list[Decimal]) -> dict[str, Any]:
    return {
        "mean": _mean(values),
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
        "sample_size": len(values),
    }


def _bill_payload(bill: UserBill) -> dict[str, Any]:
    return {
        "user_id": bill.user_id,
        "period": {"from": bill.start.isoformat(), "to": bill.end.isoformat()},
        # 迟到数据按 occurred_at 写回原账期并重算，账单因此有版本（spec D8）。
        # 一期恒为 1，字段先给出来，调用方从第一天就别缓存无版本号的账单。
        "revision": 1,
        "total_amount_usd": _amount(bill.total_amount_usd),
        "by_kind": {k: _amount(v) for k, v in bill.by_kind.items()},
        "by_scene": {k: _amount(v) for k, v in bill.by_scene.items()},
        "quantity_totals": {k: str(v) for k, v in bill.quantity_totals.items()},
        "data_quality": {
            "record_count": bill.record_count,
            "usage_missing_count": bill.usage_missing_count,
            "unpriced_count": bill.unpriced_count,
        },
    }


def _record_payload(record: UsageRecord) -> dict[str, Any]:
    q = record.quantity
    return {
        "dedup_key": record.dedup_key,
        "occurred_at": record.occurred_at.isoformat(),
        "kind": record.kind,
        "provider": record.provider,
        "model": record.model,
        "scene": record.scene,
        "session_id": record.session_id,
        "turn_id": record.turn_id,
        "status": record.status,
        "quantity": {
            "input_tokens": q.input_tokens,
            "output_tokens": q.output_tokens,
            "cache_read_tokens": q.cache_read_tokens,
            "cache_write_tokens": q.cache_write_tokens,
            "duration_seconds": None if q.duration_seconds is None else str(q.duration_seconds),
            "call_count": q.call_count,
        },
        "price_version": record.price_version,
        "unit_price_snapshot": record.unit_price_snapshot,
        "amount_original": None if record.amount_original is None else _amount(record.amount_original),
        "currency_original": record.currency_original,
        "fx_rate_snapshot": None if record.fx_rate_snapshot is None else str(record.fx_rate_snapshot),
        "amount_usd": None if record.amount_usd is None else _amount(record.amount_usd),
    }


def _aggregate_payload(rows: list[UsageAggregate]) -> list[dict[str, Any]]:
    return [
        {**row.keys, "amount_usd": _amount(row.amount_usd), "record_count": row.record_count}
        for row in rows
    ]


@router.get("/users/{user_id}/bill")
async def get_user_bill(
    user_id: str,
    request: Request,
    from_: datetime = Query(alias="from"),
    to: datetime = Query(...),
) -> dict[str, Any]:
    bill = await _store(request).bill_for_user(user_id, from_, to)
    return _bill_payload(bill)


@router.get("/users/{user_id}/ledger")
async def get_user_ledger(
    user_id: str,
    request: Request,
    from_: datetime = Query(alias="from"),
    to: datetime = Query(...),
    kind: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    rows = await _store(request).list_for_user(
        user_id, from_, to, kind=kind, limit=limit, offset=offset
    )
    return {"items": [_record_payload(r) for r in rows], "limit": limit, "offset": offset}


@router.get("/summary")
async def get_summary(
    request: Request,
    from_: datetime = Query(alias="from"),
    to: datetime = Query(...),
    group_by: str = Query(default="scene"),
) -> dict[str, Any]:
    dims = tuple(d.strip() for d in group_by.split(",") if d.strip())
    unknown = [d for d in dims if d not in _GROUPABLE]
    if unknown:
        raise HTTPException(status_code=400, detail=f"不支持的 group_by 维度: {unknown}")
    rows = await _store(request).aggregate(from_, to, group_by=dims)
    return {"group_by": list(dims), "rows": _aggregate_payload(rows)}


@router.get("/unit-economics")
async def get_unit_economics(
    request: Request,
    from_: datetime = Query(alias="from"),
    to: datetime = Query(...),
) -> dict[str, Any]:
    """回答甲方的三个问题：一分钟通话、一次聊天、一次分析各多少钱。"""
    store = _store(request)
    records: list[UsageRecord] = await store.list_in_period(from_, to, limit=1_000_000)

    voice_per_minute: list[Decimal] = []
    analysis_per_call: list[Decimal] = []
    chat_by_turn: dict[str, Decimal] = {}
    missing = 0

    for record in records:
        if record.status == "usage_missing":
            missing += 1
        amount = record.amount_usd or Decimal(0)
        if record.kind == "voice" and record.quantity.duration_seconds:
            minutes = record.quantity.duration_seconds / Decimal(60)
            if minutes > 0:
                voice_per_minute.append(amount / minutes)
        elif record.scene == "analysis":
            analysis_per_call.append(amount)
        elif record.turn_id is not None:
            # 一次聊天 = 一个 turn 内全部 LLM 与工具成本之和（spec §6.2.0）
            chat_by_turn[record.turn_id] = chat_by_turn.get(record.turn_id, Decimal(0)) + amount

    total = len(records)
    return {
        "period": {"from": from_.isoformat(), "to": to.isoformat()},
        "units": {
            "voice_per_minute": _unit_block(voice_per_minute),
            "chat_per_turn": _unit_block(list(chat_by_turn.values())),
            "analysis_per_call": _unit_block(analysis_per_call),
        },
        "data_quality": {
            "record_count": total,
            "usage_missing_count": missing,
            "usage_missing_ratio": (str(round(missing / total, 4)) if total else "0"),
        },
    }


@router.get("/top-users")
async def get_top_users(
    request: Request,
    from_: datetime = Query(alias="from"),
    to: datetime = Query(...),
    limit: int = Query(default=10, ge=1, le=200),
) -> dict[str, Any]:
    rows = await _store(request).top_users(from_, to, limit=limit)
    return {"rows": [{"user_id": uid, "amount_usd": _amount(amount)} for uid, amount in rows]}
```

`unit-economics` 用到一个新的 store 方法，两个 store 都要加：

```python
# app/usage/store.py —— UsageStore 协议加：
    async def list_in_period(
        self, start: datetime, end: datetime, *, limit: int = 100
    ) -> list[UsageRecord]: ...

# InMemoryUsageStore 实现：
    async def list_in_period(
        self, start: datetime, end: datetime, *, limit: int = 100
    ) -> list[UsageRecord]:
        rows = [r for r in self._records if self._in_period(r, start, end)]
        rows.sort(key=lambda r: r.occurred_at, reverse=True)
        return rows[:limit]

# app/usage/pg_store.py —— PostgresUsageStore 实现：
    async def list_in_period(
        self, start: datetime, end: datetime, *, limit: int = 100
    ) -> list[UsageRecord]:
        query = (
            sa.select(usage_ledger)
            .where(self._period(start, end))
            .order_by(usage_ledger.c.occurred_at.desc(), usage_ledger.c.id.desc())
            .limit(limit)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).fetchall()
        return [_from_row(row) for row in rows]
```

`app/main.py` 在既有 `include_router` 之后加（鉴权依赖照抄 strategy router 的写法）：

```python
    app.include_router(usage_router, prefix="/internal/usage")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/api/test_usage_api.py tests/usage/ tests/test_openapi.py -v && ruff check . && mypy app`
Expected: 全部 passed。`test_openapi.py` 会变红的话，按它的既有约定补 OpenAPI 描述，
不要改它的断言逻辑。

- [ ] **Step 5: 提交**

```bash
git add app/api/usage.py app/usage/store.py app/usage/pg_store.py app/main.py tests/api/test_usage_api.py
git commit -m "feat(usage): 单用户账单、明细、聚合与单位经济学查询接口"
```

---

### Task 12: 容器接线、配置与单价初始化

**Files:**
- Modify: `app/container.py`（装配 store / price_book / fx_table / recorder，startup 建表）
- Modify: `app/config.py`（单价与汇率配置）
- Create: `app/usage/price_seed.py`
- Test: `tests/usage/test_usage_container_wiring.py`

**Interfaces:**
- Consumes: 全部前置 Task
- Produces: `container.usage_store`、`container.usage_recorder`、`default_price_book(settings)`

**要求**
- `provider_mode == "real"` 且 `database_url` 非空 → `PostgresUsageStore`；否则 `InMemoryUsageStore`。
  照 `container.py:639-658` 既有的 try/ImportError 降级模式，不自创。
- `startup()` 里按既有 `getattr(store, "create_schema", None)` 模式建表。
- **`usage_recorder` 必须传给 `AgentRunner` 构造处**，否则 Task 8/9 的记账永远不触发——
  这是最容易漏的一步，测试专门锁它。
- 单价初始化值以配置可覆盖为准，代码里的默认值只是起点，**上线后须与供应商实际报价核对**。

- [ ] **Step 1: 写失败测试**

```python
# tests/usage/test_usage_container_wiring.py
"""容器接线回归。

最关键的一条：recorder 必须真的传到 AgentRunner 手里。漏了这一步，
前面所有记账代码都在，但一条流水都不会产生，而且没有任何报错。
"""
from app.usage.recorder import UsageRecorder
from app.usage.store import InMemoryUsageStore


async def test_fake_mode_uses_in_memory_usage_store(fake_container) -> None:
    assert isinstance(fake_container.usage_store, InMemoryUsageStore)
    assert isinstance(fake_container.usage_recorder, UsageRecorder)


async def test_runner_receives_usage_recorder(fake_container) -> None:
    """接线漏这一步 = 记账静默失效。"""
    runner = fake_container.build_runner() if hasattr(fake_container, "build_runner") else fake_container.runner
    assert getattr(runner, "_usage_recorder", None) is not None


async def test_startup_creates_usage_schema() -> None:
    """照 test_memory_integration.py 的既有写法验证建表被调用。"""
    called = []

    class SpyStore(InMemoryUsageStore):
        async def create_schema(self) -> None:
            called.append(True)

    container = build_container_with_usage_store(SpyStore())
    await container.startup()
    assert called == [True]


def test_default_price_book_covers_all_wired_providers() -> None:
    """每个会产生流水的 provider 都得有价，否则报表里全是 unpriced。"""
    from datetime import UTC, datetime

    from app.config import Settings
    from app.usage.price_seed import default_price_book

    book = default_price_book(Settings())
    at = datetime(2026, 9, 10, tzinfo=UTC)
    assert book.lookup(provider="anthropic", kind="llm", metric="input_tokens", model=None, at=at)
    assert book.lookup(provider="anthropic", kind="llm", metric="output_tokens", model=None, at=at)
    assert book.lookup(provider="volc", kind="voice", metric="duration_minute", model=None, at=at)
    assert book.lookup(provider="gemini", kind="voice", metric="duration_minute", model=None, at=at)
    assert book.lookup(provider="tavily", kind="external_api", metric="call", model=None, at=at)


def test_core_is_priced_zero_not_unpriced() -> None:
    """spec D2：Core 是内部服务，单价 0；0 与「查不到价」必须区分。"""
    from datetime import UTC, datetime
    from decimal import Decimal

    from app.config import Settings
    from app.usage.price_seed import default_price_book

    entry = default_price_book(Settings()).lookup(
        provider="core", kind="external_api", metric="call", model=None,
        at=datetime(2026, 9, 10, tzinfo=UTC),
    )
    assert entry is not None
    assert entry.unit_price == Decimal(0)


def test_internal_and_unknown_providers_are_priced_zero() -> None:
    from datetime import UTC, datetime

    from app.config import Settings
    from app.usage.price_seed import default_price_book

    book = default_price_book(Settings())
    at = datetime(2026, 9, 10, tzinfo=UTC)
    assert book.lookup(provider="internal", kind="external_api", metric="call", model=None, at=at)
    assert book.lookup(provider="unknown", kind="external_api", metric="call", model=None, at=at)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/usage/test_usage_container_wiring.py -v`
Expected: FAIL — `container` 无 `usage_store` 属性

- [ ] **Step 3: 写最小实现**

```python
# app/usage/price_seed.py
"""单价与汇率初始值。

这里的数字是**起点，不是事实**：上线后必须与供应商实际报价核对，调价也要来改。
匹配不到价时流水会落 status=unpriced 并在报表里告警，所以漏配不会变成静默的 0。

单位口径见 app/usage/pricing.py 的 _METRICS：token 类按 1M，语音按分钟，API 按次。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.usage.fx import FxTable
from app.usage.pricing import PriceBook, PriceEntry

#: 初始价生效起点。改价时**新增**一条 effective_from 更晚的条目，不要改这条——
#: 历史流水已经快照了单价，但重算时仍要按当时的价来。
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
_VERSION = "seed-2026-09"


def _entry(
    provider: str, kind: str, metric: str, price: str, currency: str = "USD"
) -> PriceEntry:
    return PriceEntry(
        provider=provider,
        kind=kind,
        metric=metric,
        unit_price=Decimal(price),
        currency=currency,
        effective_from=_EPOCH,
        price_version=_VERSION,
        model=None,
    )


def default_price_book(settings: object) -> PriceBook:
    """provider 级兜底价。需要按模型细分时，加 model 非空的条目即可覆盖。"""
    return PriceBook(
        [
            # LLM（USD / 1M tokens）
            _entry("anthropic", "llm", "input_tokens", "15"),
            _entry("anthropic", "llm", "output_tokens", "75"),
            _entry("anthropic", "llm", "cache_read", "1.5"),
            _entry("anthropic", "llm", "cache_write", "18.75"),
            _entry("gemini", "llm", "input_tokens", "1.25"),
            _entry("gemini", "llm", "output_tokens", "10"),
            # 语音（按分钟）
            _entry("volc", "voice", "duration_minute", "0.6", currency="CNY"),
            _entry("gemini", "voice", "duration_minute", "0.03"),
            # 外部 API（按次）
            _entry("tavily", "external_api", "call", "0.008"),
            _entry("followin", "external_api", "call", "0"),
            # Core 是内部服务，单价 0，但流水照采（spec D2）
            _entry("core", "external_api", "call", "0"),
            _entry("internal", "external_api", "call", "0"),
            # 未登记工具：计 0 但 provider 标 unknown，报表里能发现漏配
            _entry("unknown", "external_api", "call", "0"),
        ]
    )


def default_fx_table(settings: object) -> FxTable:
    """一期只需 CNY→USD 的固定兜底汇率（spec §4.2）。

    每日汇率接入属二期；固定值先保证 CNY 供应商的成本不会因缺汇率全落 unpriced。
    """
    rate = getattr(settings, "usage_fx_cny_to_usd", 0.1385)
    return FxTable([], fallback={"CNY": Decimal(str(rate))})
```

`app/config.py` 加：

```python
    # 用量核算（spec §4.2）。每日汇率接入属二期，一期用固定兜底值。
    usage_fx_cny_to_usd: float = Field(default=0.1385, gt=0.0, le=100.0)
    usage_llm_provider: str = "anthropic"
```

`app/container.py`：

1. 字段区（照 `voice_usage_store` 那行的位置）加：

```python
    usage_store: UsageStore = field(default_factory=InMemoryUsageStore)
    usage_recorder: UsageRecorder | None = None
```

2. 装配处（`provider_mode == "real"` 的 try 块内，与 `PostgresAuditStore` 并列）加：

```python
                from app.usage.pg_store import PostgresUsageStore

                usage_store: UsageStore = PostgresUsageStore(engine)
```

`except ImportError` 与 `else` 两个分支都加 `usage_store = InMemoryUsageStore()`。

3. 构造 container 时（与 `voice_usage_store=...` 并列）加：

```python
        usage_store=usage_store,
        usage_recorder=UsageRecorder(
            usage_store, default_price_book(settings), default_fx_table(settings)
        ),
```

4. `startup()` 里，紧跟 `create_conversation_schema` 那段之后加：

```python
        create_usage_schema = getattr(self.usage_store, "create_schema", None)
        if create_usage_schema is not None:
            await create_usage_schema()
```

5. **构造 `AgentRunner` 的地方**加两个参数：

```python
            usage_recorder=self.usage_recorder,
            usage_provider=self.settings.usage_llm_provider,
```

> 第 5 步是整个计划最容易漏的一步。漏了不会报错、不会有测试变红（除了本 Task 那条），
> 但一条流水都不会产生。改完先跑 `test_runner_receives_usage_recorder`。

- [ ] **Step 4: 跑全量测试确认通过**

Run: `python -m pytest -q`
Expected: 全部 passed

Run: `python -m pytest -v --cov=app.tools.agentos --cov=app.strategy --cov=app.api.strategy --cov-branch --cov-fail-under=100 && ruff check . && mypy app`
Expected: 与 CI 一致，全绿

Run: `python -m pytest tests/usage/ tests/api/test_usage_api.py --cov=app.usage --cov=app.api.usage --cov-report=term-missing`
Expected: `app/usage` 与 `app/api/usage.py` 覆盖率 ≥ 80%

- [ ] **Step 5: 提交**

```bash
git add app/container.py app/config.py app/usage/price_seed.py tests/usage/test_usage_container_wiring.py
git commit -m "feat(usage): 容器接线、单价初始值与汇率兜底"
```

---

## 验收清单（对应 spec §9）

全部 Task 完成后逐条核对：

- [ ] 单用户任意账期账单可查（S1/S2）：`GET /internal/usage/users/{id}/bill`
- [ ] 金额可复算：明细带 `price_version` + `unit_price_snapshot` + `fx_rate_snapshot`（S6）
- [ ] 三类用量均有流水：跑一轮带工具的对话 + 一次语音结算，`usage_ledger` 里能看到 `llm` / `external_api` / `voice` 三种 kind
- [ ] `usage_missing` 与 `unpriced` 在账单与单位经济学响应里可见（S7）
- [ ] 一轮对话内多次 LLM + 多次工具调用归并到同一 `turn_id`（S4 的前提）
- [ ] 重复上报不产生重复计费：同一 `session_id` 二次 settle 只有一条 voice 流水
- [ ] 并发重复不产生重复计费：`test_concurrent_duplicate_appends_insert_only_one` 通过
- [ ] 记账失败不影响主链路：三条 fail-safe 测试通过，`failure_count` 可读
- [ ] 用量排行可查（S5）
- [ ] 单位经济学三个数可查（S3/S4）
- [ ] `python -m pytest -q` 全绿，`ruff check .` 与 `mypy app` 无输出
- [ ] `app/usage` 覆盖率 ≥ 80%

## 一期明确不做（勿顺手加）

- 对外计费、扣费、余额、限流
- 内部看板前端
- 月度物化聚合表（明细实时聚合够用，超阈值再说）
- 每日汇率接入（一期固定兜底值）
- 真实账单对账校准系数
- 把 `derive_duration_seconds` 接到火山真实回调（需先有 staging 事件样本，见 Task 10）
- 历史数据回填（spec D3）
