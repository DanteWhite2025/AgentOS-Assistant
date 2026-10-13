# 策略容器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `yesup-agent-runtime` 搭建策略容器——一人一 Agent 可并行多个隔离的逻辑配额策略（额度/容量/风控/有效期/运行状态），并接入现有交易链路。

**Architecture:** 1 Agent = 1 Core 账户；策略是 Runtime 侧逻辑配额容器（不为每策略申请 Core 账户/mandate）。新增独立域 `app/strategy/`（Container / Store+Protocol / Manager）+ REST 路由 `app/api/strategy.py`；复用现有 `AgentMandate`/`PolicyEngine` 做每单预检、复用 `app/tools/agentos` 交易链路以 Agent 账户建单。

**Tech Stack:** Python 3.12 / FastAPI / Pydantic v2 / pytest + pytest-cov / httpx MockTransport。

## Global Constraints

- 金额一律 `Decimal`，禁止 `float`（与 `app/tools/agentos/models.py` 一致）。
- 不可变数据：frozen dataclass；变更返回新实例，绝不原地修改。
- 身份从 token/会话派生：`user_id`/`agent_id` 从请求头（`X-User-Id` / `X-Agent-Id`，MVP）取，**不入 body**；账户经 `agent-trading-accounts` 发现，不信任客户端账户 id。
- Core 是资金/风控权威；runtime 预检为护栏。
- `app/strategy/` 达到 **行+分支 100%** 覆盖（CI 强制，比照 `app.tools.agentos`）。
- 错误统一走 `app.api.errors.error_response`；`ErrorCode` 仅限既有字面量。
- 文件小而聚焦（单一职责）。

---

### Task 1: 领域模型 `StrategyContainer` + 状态机

**Files:**
- Create: `app/strategy/__init__.py`（空）
- Create: `app/strategy/container.py`
- Test: `tests/test_strategy_container.py`

**Interfaces:**
- Produces:
  - `StrategyState`（Enum: `ACTIVE/PAUSED/TERMINATED/EXPIRED`）
  - `IllegalStateTransition(Exception)`
  - `StrategyContainer`（frozen dataclass，字段见下）+ 方法 `pause(now)/resume(now)/terminate(now)/expire(now)/with_allocation(value, now)/is_expired(now)->bool`，均返回新实例（`version+1`、`updated_at=now`）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_strategy_container.py
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.strategy.container import IllegalStateTransition, StrategyContainer, StrategyState

_NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def _c(**over):
    base = dict(
        strategy_id="strat_1", user_id="u1", agent_id="a1", name="趋势1",
        template_id="tpl_trend", runtime_env="paper",
        allocation="100", capital_capacity="500", strategy_capacity="1000",
        max_leverage="3", created_at=_NOW, updated_at=_NOW,
    )
    base.update(over)
    return StrategyContainer(**base)


def test_valid_container_defaults():
    c = _c()
    assert c.state is StrategyState.ACTIVE
    assert c.version == 1
    assert c.allocation == Decimal("100") and c.strategy_capacity == Decimal("1000")
    assert c.allowed_assets is None and c.expires_at is None


@pytest.mark.parametrize("over, match", [
    (dict(allocation="600"), "allocation"),          # allocation > capital_capacity
    (dict(capital_capacity="2000"), "capital"),      # capital_capacity > strategy_capacity
    (dict(allocation="-1"), "allocation"),           # 负数
    (dict(max_leverage="0"), "leverage"),            # 杠杆须 > 0
    (dict(runtime_env="prod"), "runtime_env"),       # 非法枚举
    (dict(template_id="  "), "template_id"),         # 空
    (dict(name=""), "name"),                         # 空
])
def test_invariants_rejected(over, match):
    with pytest.raises(ValueError, match=match):
        _c(**over)


def test_pause_resume_roundtrip():
    c = _c()
    p = c.pause(_NOW)
    assert p.state is StrategyState.PAUSED and p.version == 2 and c.state is StrategyState.ACTIVE
    r = p.resume(_NOW)
    assert r.state is StrategyState.ACTIVE and r.version == 3


@pytest.mark.parametrize("start, method", [
    (StrategyState.PAUSED, "pause"),        # 非 ACTIVE 不能 pause
    (StrategyState.ACTIVE, "resume"),       # 非 PAUSED 不能 resume
    (StrategyState.TERMINATED, "terminate"),# 终态不能再 terminate
])
def test_illegal_transitions(start, method):
    c = _c(state=start)
    with pytest.raises(IllegalStateTransition):
        getattr(c, method)(_NOW)


def test_terminate_and_expire_from_active_or_paused():
    assert _c().terminate(_NOW).state is StrategyState.TERMINATED
    assert _c(state=StrategyState.PAUSED).terminate(_NOW).state is StrategyState.TERMINATED
    assert _c().expire(_NOW).state is StrategyState.EXPIRED


def test_resume_blocked_when_expired():
    c = _c(state=StrategyState.PAUSED, expires_at=_NOW - timedelta(hours=1))
    with pytest.raises(IllegalStateTransition):
        c.resume(_NOW)


def test_with_allocation_respects_capacity():
    c = _c()
    assert c.with_allocation("400", _NOW).allocation == Decimal("400")
    with pytest.raises(ValueError, match="allocation"):
        c.with_allocation("600", _NOW)  # > capital_capacity


def test_is_expired():
    assert _c(expires_at=_NOW - timedelta(seconds=1)).is_expired(_NOW) is True
    assert _c(expires_at=None).is_expired(_NOW) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_strategy_container.py -q`
Expected: FAIL（`ModuleNotFoundError: app.strategy.container`）

- [ ] **Step 3: 写最小实现**

```python
# app/strategy/container.py
"""策略容器领域模型 —— Runtime 侧逻辑配额沙箱（不可变 + 构造即校验）。

1 Agent = 1 Core 账户；策略在 Agent 账户内做逻辑资金分配与风控预检。金额一律 Decimal。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Final

_RUNTIME_ENVS: Final = ("live", "paper", "backtest")


class StrategyState(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    TERMINATED = "TERMINATED"
    EXPIRED = "EXPIRED"


class IllegalStateTransition(Exception):
    """策略状态迁移非法（对应端点 409）。"""


def _money(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} 不能是布尔值")
    try:
        dec = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} 不是合法数值: {value!r}") from exc
    return dec


@dataclass(frozen=True)
class StrategyContainer:
    strategy_id: str
    user_id: str
    agent_id: str
    name: str
    template_id: str
    runtime_env: str
    allocation: Decimal
    capital_capacity: Decimal
    strategy_capacity: Decimal
    max_leverage: Decimal
    created_at: datetime
    updated_at: datetime
    allowed_assets: tuple[str, ...] | None = None
    expires_at: datetime | None = None
    state: StrategyState = StrategyState.ACTIVE
    version: int = 1

    def __post_init__(self) -> None:
        for name in ("strategy_id", "user_id", "agent_id", "name", "template_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} 不能为空")
        if self.runtime_env not in _RUNTIME_ENVS:
            raise ValueError(f"runtime_env 必须是 {_RUNTIME_ENVS}，收到 {self.runtime_env!r}")
        for name in ("allocation", "capital_capacity", "strategy_capacity", "max_leverage"):
            object.__setattr__(self, name, _money(getattr(self, name), name))
        if self.max_leverage <= 0:
            raise ValueError("max_leverage 必须 > 0")
        if self.allocation < 0:
            raise ValueError("allocation 不能为负")
        if self.allocation > self.capital_capacity:
            raise ValueError("allocation 不能超过 capital_capacity（本金容量）")
        if self.capital_capacity > self.strategy_capacity:
            raise ValueError("capital_capacity 不能超过 strategy_capacity（策略容量）")

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def _evolve(self, now: datetime, **changes: Any) -> StrategyContainer:
        from dataclasses import replace

        return replace(self, updated_at=now, version=self.version + 1, **changes)

    def pause(self, now: datetime) -> StrategyContainer:
        if self.state is not StrategyState.ACTIVE:
            raise IllegalStateTransition(f"仅 ACTIVE 可暂停，当前 {self.state}")
        return self._evolve(now, state=StrategyState.PAUSED)

    def resume(self, now: datetime) -> StrategyContainer:
        if self.state is not StrategyState.PAUSED:
            raise IllegalStateTransition(f"仅 PAUSED 可恢复，当前 {self.state}")
        if self.is_expired(now):
            raise IllegalStateTransition("策略已过期，不能恢复")
        return self._evolve(now, state=StrategyState.ACTIVE)

    def terminate(self, now: datetime) -> StrategyContainer:
        if self.state in (StrategyState.TERMINATED, StrategyState.EXPIRED):
            raise IllegalStateTransition(f"终态不可再终止，当前 {self.state}")
        return self._evolve(now, state=StrategyState.TERMINATED)

    def expire(self, now: datetime) -> StrategyContainer:
        if self.state in (StrategyState.TERMINATED, StrategyState.EXPIRED):
            raise IllegalStateTransition(f"终态不可再过期，当前 {self.state}")
        return self._evolve(now, state=StrategyState.EXPIRED)

    def with_allocation(self, value: Any, now: datetime) -> StrategyContainer:
        new_alloc = _money(value, "allocation")
        if new_alloc < 0 or new_alloc > self.capital_capacity:
            raise ValueError("allocation 必须在 [0, capital_capacity] 内")
        return self._evolve(now, allocation=new_alloc)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_strategy_container.py -q`
Expected: PASS（全绿）

- [ ] **Step 5: 提交**

```bash
git add app/strategy/__init__.py app/strategy/container.py tests/test_strategy_container.py
git commit -m "feat(strategy): StrategyContainer 领域模型 + 状态机（不可变+不变量）"
```

---

### Task 2: `StrategyStore` Protocol + 内存实现

**Files:**
- Create: `app/strategy/store.py`
- Test: `tests/test_strategy_store.py`

**Interfaces:**
- Consumes: `StrategyContainer`（Task 1）
- Produces:
  - `StrategyStore`（Protocol）：`create/get/update/delete/list_by_agent/list_by_user`（全 async）
  - `InMemoryStrategyStore`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_strategy_store.py
from datetime import datetime, timezone

import pytest

from app.strategy.container import StrategyContainer
from app.strategy.store import InMemoryStrategyStore

_NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def _c(sid, user="u1", agent="a1"):
    return StrategyContainer(
        strategy_id=sid, user_id=user, agent_id=agent, name="s", template_id="tpl",
        runtime_env="paper", allocation="1", capital_capacity="10", strategy_capacity="10",
        max_leverage="2", created_at=_NOW, updated_at=_NOW,
    )


async def test_create_get_update_delete():
    store = InMemoryStrategyStore()
    await store.create(_c("strat_1"))
    got = await store.get("strat_1")
    assert got is not None and got.strategy_id == "strat_1"
    await store.update(got.pause(_NOW))
    assert (await store.get("strat_1")).state.value == "PAUSED"
    await store.delete("strat_1")
    assert await store.get("strat_1") is None


async def test_get_missing_returns_none():
    assert await InMemoryStrategyStore().get("nope") is None


async def test_list_by_agent_and_user_isolation():
    store = InMemoryStrategyStore()
    await store.create(_c("s1", user="u1", agent="a1"))
    await store.create(_c("s2", user="u1", agent="a1"))
    await store.create(_c("s3", user="u1", agent="a2"))  # 同用户，另一 agent
    await store.create(_c("s4", user="u2", agent="a1"))  # 另一用户
    by_agent = await store.list_by_agent("u1", "a1")
    assert {s.strategy_id for s in by_agent} == {"s1", "s2"}
    by_user = await store.list_by_user("u1")
    assert {s.strategy_id for s in by_user} == {"s1", "s2", "s3"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_strategy_store.py -q`
Expected: FAIL（`ModuleNotFoundError: app.strategy.store`）

- [ ] **Step 3: 写最小实现**

```python
# app/strategy/store.py
"""策略容器存储 —— Protocol + 内存实现（real 模式 PG 落库后续接，配置即切换）。"""

from __future__ import annotations

from typing import Protocol

from app.strategy.container import StrategyContainer


class StrategyStore(Protocol):
    async def create(self, strategy: StrategyContainer) -> None: ...
    async def get(self, strategy_id: str) -> StrategyContainer | None: ...
    async def update(self, strategy: StrategyContainer) -> None: ...
    async def delete(self, strategy_id: str) -> None: ...
    async def list_by_agent(self, user_id: str, agent_id: str) -> list[StrategyContainer]: ...
    async def list_by_user(self, user_id: str) -> list[StrategyContainer]: ...


class InMemoryStrategyStore:
    """进程内存储；测试/MVP 用。不可变对象直接存引用。"""

    def __init__(self) -> None:
        self._by_id: dict[str, StrategyContainer] = {}

    async def create(self, strategy: StrategyContainer) -> None:
        self._by_id[strategy.strategy_id] = strategy

    async def get(self, strategy_id: str) -> StrategyContainer | None:
        return self._by_id.get(strategy_id)

    async def update(self, strategy: StrategyContainer) -> None:
        self._by_id[strategy.strategy_id] = strategy

    async def delete(self, strategy_id: str) -> None:
        self._by_id.pop(strategy_id, None)

    async def list_by_agent(self, user_id: str, agent_id: str) -> list[StrategyContainer]:
        return [
            s for s in self._by_id.values() if s.user_id == user_id and s.agent_id == agent_id
        ]

    async def list_by_user(self, user_id: str) -> list[StrategyContainer]:
        return [s for s in self._by_id.values() if s.user_id == user_id]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_strategy_store.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/strategy/store.py tests/test_strategy_store.py
git commit -m "feat(strategy): StrategyStore Protocol + InMemoryStrategyStore"
```

---

### Task 3: `StrategyManager`（编排 + 派生 mandate + 聚合额度 + 执行预检）

**Files:**
- Create: `app/strategy/manager.py`
- Test: `tests/test_strategy_manager.py`

**Interfaces:**
- Consumes: `StrategyContainer`/`StrategyState`/`IllegalStateTransition`（Task 1）、`StrategyStore`（Task 2）、`AgentMandate`（`app/policies/mandate.py`）
- Produces:
  - `StrategyNotFound(Exception)`、`OverAllocationError(Exception)`
  - `derive_mandate(container) -> AgentMandate`
  - `StrategyManager(store)`，方法：
    - `async create(*, user_id, agent_id, name, template_id, runtime_env, allocation, capital_capacity, strategy_capacity, max_leverage, allowed_assets, expires_at, now, agent_capital=None, id_factory=None) -> StrategyContainer`
    - `async get_owned(strategy_id, user_id, agent_id) -> StrategyContainer`（缺失/越权抛 `StrategyNotFound`）
    - `async list(user_id, agent_id) -> list[StrategyContainer]`
    - `async pause/resume/terminate(strategy_id, user_id, agent_id, now) -> StrategyContainer`
    - `async allocate(strategy_id, user_id, agent_id, value, now) -> StrategyContainer`
    - `ensure_tradable(container, now) -> None`（非 ACTIVE 或已过期抛 `IllegalStateTransition`）

- [ ] **Step 1: 写失败测试**

```python
# tests/test_strategy_manager.py
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.strategy.container import IllegalStateTransition, StrategyState
from app.strategy.manager import (
    OverAllocationError,
    StrategyManager,
    StrategyNotFound,
    derive_mandate,
)
from app.strategy.store import InMemoryStrategyStore

_NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)


def _mgr():
    return StrategyManager(InMemoryStrategyStore())


async def _make(mgr, **over):
    base = dict(
        user_id="u1", agent_id="a1", name="s", template_id="tpl", runtime_env="paper",
        allocation="100", capital_capacity="500", strategy_capacity="1000",
        max_leverage="3", allowed_assets=None, expires_at=None, now=_NOW,
    )
    base.update(over)
    return await mgr.create(**base)


async def test_create_generates_id_and_persists():
    mgr = _mgr()
    c = await _make(mgr, id_factory=lambda: "strat_fixed")
    assert c.strategy_id == "strat_fixed" and c.state is StrategyState.ACTIVE
    assert (await mgr.get_owned("strat_fixed", "u1", "a1")).strategy_id == "strat_fixed"


async def test_create_rejects_invariant_violation():
    mgr = _mgr()
    with pytest.raises(ValueError):
        await _make(mgr, allocation="600")  # > capital_capacity


async def test_create_aggregate_over_allocation_blocked():
    mgr = _mgr()
    await _make(mgr, allocation="400", id_factory=lambda: "s1")
    with pytest.raises(OverAllocationError):
        await _make(mgr, allocation="400", agent_capital="500", id_factory=lambda: "s2")
    # 400 + 400 > 500（Agent 可用本金）


async def test_get_owned_missing_or_cross_agent_raises():
    mgr = _mgr()
    await _make(mgr, id_factory=lambda: "s1")
    with pytest.raises(StrategyNotFound):
        await mgr.get_owned("nope", "u1", "a1")
    with pytest.raises(StrategyNotFound):
        await mgr.get_owned("s1", "u1", "a2")  # 越 agent


async def test_lifecycle_pause_resume_terminate_persist():
    mgr = _mgr()
    await _make(mgr, id_factory=lambda: "s1")
    assert (await mgr.pause("s1", "u1", "a1", _NOW)).state is StrategyState.PAUSED
    assert (await mgr.resume("s1", "u1", "a1", _NOW)).state is StrategyState.ACTIVE
    assert (await mgr.terminate("s1", "u1", "a1", _NOW)).state is StrategyState.TERMINATED


async def test_allocate_updates():
    mgr = _mgr()
    await _make(mgr, id_factory=lambda: "s1")
    assert (await mgr.allocate("s1", "u1", "a1", "250", _NOW)).allocation == Decimal("250")


async def test_ensure_tradable():
    mgr = _mgr()
    active = await _make(mgr, id_factory=lambda: "s1")
    mgr.ensure_tradable(active, _NOW)  # 不抛
    with pytest.raises(IllegalStateTransition):
        mgr.ensure_tradable(active.pause(_NOW), _NOW)
    with pytest.raises(IllegalStateTransition):
        mgr.ensure_tradable(
            await _make(mgr, expires_at=_NOW - timedelta(hours=1), id_factory=lambda: "s2"), _NOW
        )


def test_derive_mandate_maps_fields():
    from app.strategy.container import StrategyContainer

    c = StrategyContainer(
        strategy_id="s", user_id="u", agent_id="a", name="n", template_id="t",
        runtime_env="live", allocation="100", capital_capacity="500", strategy_capacity="500",
        max_leverage="3", allowed_assets=("BTC",), expires_at=_NOW, created_at=_NOW, updated_at=_NOW,
    )
    m = derive_mandate(c)
    assert m.max_leverage == Decimal("3")
    assert m.max_notional == Decimal("100")  # = allocation
    assert m.allowed_assets == ("BTC",)
    assert m.expires_at == _NOW
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_strategy_manager.py -q`
Expected: FAIL（`ModuleNotFoundError: app.strategy.manager`）

- [ ] **Step 3: 写最小实现**

```python
# app/strategy/manager.py
"""策略容器编排 —— 创建/查询/生命周期/额度 + 派生 mandate + 执行前预检。

红线：user_id/agent_id 由调用方（token/会话）派生；账户由 Agent 维度发现，策略不建 Core 账户。
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.policies.mandate import AgentMandate
from app.strategy.container import IllegalStateTransition, StrategyContainer
from app.strategy.store import StrategyStore


class StrategyNotFound(Exception):
    """策略不存在或不属于该 user/agent（对应端点 404）。"""


class OverAllocationError(Exception):
    """该 Agent 下 ACTIVE 策略额度之和超过 Agent 可用本金（对应端点 409）。"""


def _new_id() -> str:
    return "strat_" + secrets.token_hex(12)


def derive_mandate(container: StrategyContainer) -> AgentMandate:
    """从策略配置派生 AgentMandate，用于每单 runtime 预检（Core 仍权威）。"""
    return AgentMandate(
        max_notional=container.allocation,
        max_leverage=container.max_leverage,
        allowed_assets=container.allowed_assets,
        allowed_markets=None,
        expires_at=container.expires_at,
        require_confirmation=True,
    )


class StrategyManager:
    def __init__(self, store: StrategyStore) -> None:
        self._store = store

    async def create(
        self,
        *,
        user_id: str,
        agent_id: str,
        name: str,
        template_id: str,
        runtime_env: str,
        allocation: Any,
        capital_capacity: Any,
        strategy_capacity: Any,
        max_leverage: Any,
        allowed_assets: tuple[str, ...] | None,
        expires_at: datetime | None,
        now: datetime,
        agent_capital: Any = None,
        id_factory: Callable[[], str] | None = None,
    ) -> StrategyContainer:
        container = StrategyContainer(
            strategy_id=(id_factory or _new_id)(),
            user_id=user_id,
            agent_id=agent_id,
            name=name,
            template_id=template_id,
            runtime_env=runtime_env,
            allocation=allocation,
            capital_capacity=capital_capacity,
            strategy_capacity=strategy_capacity,
            max_leverage=max_leverage,
            allowed_assets=allowed_assets,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        if agent_capital is not None:
            cap = Decimal(str(agent_capital))
            existing = await self._store.list_by_agent(user_id, agent_id)
            active_sum = sum(
                (s.allocation for s in existing if s.state.value == "ACTIVE"), Decimal(0)
            )
            if active_sum + container.allocation > cap:
                raise OverAllocationError(
                    f"额度之和 {active_sum + container.allocation} 超过 Agent 可用本金 {cap}"
                )
        await self._store.create(container)
        return container

    async def get_owned(
        self, strategy_id: str, user_id: str, agent_id: str
    ) -> StrategyContainer:
        s = await self._store.get(strategy_id)
        if s is None or s.user_id != user_id or s.agent_id != agent_id:
            raise StrategyNotFound(strategy_id)
        return s

    async def list(self, user_id: str, agent_id: str) -> list[StrategyContainer]:
        return await self._store.list_by_agent(user_id, agent_id)

    async def _transition(
        self, strategy_id: str, user_id: str, agent_id: str, method: str, now: datetime
    ) -> StrategyContainer:
        s = await self.get_owned(strategy_id, user_id, agent_id)
        updated = getattr(s, method)(now)
        await self._store.update(updated)
        return updated

    async def pause(self, sid: str, user_id: str, agent_id: str, now: datetime) -> StrategyContainer:
        return await self._transition(sid, user_id, agent_id, "pause", now)

    async def resume(self, sid: str, user_id: str, agent_id: str, now: datetime) -> StrategyContainer:
        return await self._transition(sid, user_id, agent_id, "resume", now)

    async def terminate(self, sid: str, user_id: str, agent_id: str, now: datetime) -> StrategyContainer:
        return await self._transition(sid, user_id, agent_id, "terminate", now)

    async def allocate(
        self, sid: str, user_id: str, agent_id: str, value: Any, now: datetime
    ) -> StrategyContainer:
        s = await self.get_owned(sid, user_id, agent_id)
        updated = s.with_allocation(value, now)
        await self._store.update(updated)
        return updated

    def ensure_tradable(self, container: StrategyContainer, now: datetime) -> None:
        if container.state.value != "ACTIVE":
            raise IllegalStateTransition(f"策略非 ACTIVE（当前 {container.state}），不可交易")
        if container.is_expired(now):
            raise IllegalStateTransition("策略已过期，不可交易")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_strategy_manager.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/strategy/manager.py tests/test_strategy_manager.py
git commit -m "feat(strategy): StrategyManager（编排+派生mandate+聚合额度+执行预检）"
```

---

### Task 4: DI 装配 + REST 管理端点（CRUD + 生命周期）

**Files:**
- Modify: `app/container.py`（`Container` 加 `strategy_store` / `strategy_manager`；`build_container` 装配）
- Create: `app/api/strategy.py`（router）
- Modify: `app/main.py`（挂载 strategy router）
- Modify: `app/api/schemas.py`（加 `StrategyCreateRequest` / `StrategyAllocateRequest` / `StrategyView`）
- Test: `tests/test_strategy_api.py`

**Interfaces:**
- Consumes: `StrategyManager`/`StrategyNotFound`/`OverAllocationError`（Task 3）、`InMemoryStrategyStore`（Task 2）、`IllegalStateTransition`（Task 1）、`get_container`（`app/api/deps.py`）、`error_response`/`new_trace_id`（`app/api/errors.py`）
- Produces:
  - REST：`POST /v1/agent/strategies`、`GET /v1/agent/strategies`、`GET /{id}`、`POST /{id}/pause|resume|terminate`、`POST /{id}/allocate`
  - Headers：`X-User-Id`（默认 `u_dev`）、`X-Agent-Id`（默认 `agent-test-001`）
  - Schemas：`StrategyView`（响应）、`StrategyCreateRequest`、`StrategyAllocateRequest`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_strategy_api.py
import httpx
import pytest

from app.config import Settings
from app.container import build_container
from app.main import create_app

_HDR = {"X-User-Id": "u1", "X-Agent-Id": "a1"}


def _app():
    app = create_app()
    app.state.container = build_container(Settings(provider_mode="fake"))
    return app


def _api(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _body(**over):
    base = dict(
        name="趋势1", template_id="tpl_trend", runtime_env="paper",
        allocation="100", capital_capacity="500", strategy_capacity="1000", max_leverage="3",
    )
    base.update(over)
    return base


async def test_create_and_get_and_list():
    async with _api(_app()) as api:
        r = await api.post("/v1/agent/strategies", json=_body(), headers=_HDR)
        assert r.status_code == 201, r.text
        sid = r.json()["strategy_id"]
        assert r.json()["state"] == "ACTIVE"
        assert (await api.get(f"/v1/agent/strategies/{sid}", headers=_HDR)).status_code == 200
        lst = await api.get("/v1/agent/strategies", headers=_HDR)
        assert [s["strategy_id"] for s in lst.json()["items"]] == [sid]


async def test_create_invalid_invariant_400():
    async with _api(_app()) as api:
        r = await api.post("/v1/agent/strategies", json=_body(allocation="600"), headers=_HDR)
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_REQUEST"


async def test_get_cross_agent_404():
    async with _api(_app()) as api:
        sid = (await api.post("/v1/agent/strategies", json=_body(), headers=_HDR)).json()["strategy_id"]
        r = await api.get(f"/v1/agent/strategies/{sid}", headers={"X-User-Id": "u1", "X-Agent-Id": "OTHER"})
        assert r.status_code == 404


async def test_lifecycle_endpoints():
    async with _api(_app()) as api:
        sid = (await api.post("/v1/agent/strategies", json=_body(), headers=_HDR)).json()["strategy_id"]
        assert (await api.post(f"/v1/agent/strategies/{sid}/pause", headers=_HDR)).json()["state"] == "PAUSED"
        # 重复 pause → 409
        assert (await api.post(f"/v1/agent/strategies/{sid}/pause", headers=_HDR)).status_code == 409
        assert (await api.post(f"/v1/agent/strategies/{sid}/resume", headers=_HDR)).json()["state"] == "ACTIVE"
        assert (await api.post(f"/v1/agent/strategies/{sid}/terminate", headers=_HDR)).json()["state"] == "TERMINATED"


async def test_allocate_and_over_capacity_400():
    async with _api(_app()) as api:
        sid = (await api.post("/v1/agent/strategies", json=_body(), headers=_HDR)).json()["strategy_id"]
        assert (await api.post(f"/v1/agent/strategies/{sid}/allocate", json={"allocation": "250"}, headers=_HDR)).json()["allocation"] == "250"
        r = await api.post(f"/v1/agent/strategies/{sid}/allocate", json={"allocation": "600"}, headers=_HDR)
        assert r.status_code == 400
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_strategy_api.py -q`
Expected: FAIL（路由 404 / 导入错误）

- [ ] **Step 3a: DI 装配（`app/container.py`）**

在 `Container` dataclass 加字段（放在 `agentos_client` 附近）：

```python
    strategy_store: "StrategyStore | None" = None
    strategy_manager: "StrategyManager | None" = None
```

顶部加导入：

```python
from app.strategy.manager import StrategyManager
from app.strategy.store import InMemoryStrategyStore, StrategyStore
```

`build_container` 末尾（`return Container(...)` 之前）装配，并加入返回参数：

```python
    strategy_store: StrategyStore = InMemoryStrategyStore()
    strategy_manager = StrategyManager(strategy_store)
```
```python
        strategy_store=strategy_store,
        strategy_manager=strategy_manager,
```

- [ ] **Step 3b: Schemas（`app/api/schemas.py` 末尾追加）**

```python
class StrategyCreateRequest(BaseModel):
    name: str
    template_id: str
    runtime_env: Literal["live", "paper", "backtest"]
    allocation: str
    capital_capacity: str
    strategy_capacity: str
    max_leverage: str
    allowed_assets: list[str] | None = None
    expires_at: datetime | None = None


class StrategyAllocateRequest(BaseModel):
    allocation: str


class StrategyView(BaseModel):
    strategy_id: str
    user_id: str
    agent_id: str
    name: str
    template_id: str
    runtime_env: str
    allocation: str
    capital_capacity: str
    strategy_capacity: str
    max_leverage: str
    allowed_assets: list[str] | None
    expires_at: datetime | None
    state: str
    version: int


class StrategyList(BaseModel):
    items: list[StrategyView]
```

（`datetime` 已在 schemas 顶部导入；若无则加 `from datetime import datetime`。）

- [ ] **Step 3c: Router（`app/api/strategy.py`）**

```python
"""策略容器 REST 端点。前缀 /v1/agent/strategies 由 main.py 挂载。

user_id/agent_id 从请求头派生（MVP：X-User-Id / X-Agent-Id），不入 body。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Header, status

from app.api import schemas
from app.api.deps import get_container
from app.api.errors import error_response
from app.container import Container
from app.strategy.container import IllegalStateTransition, StrategyContainer
from app.strategy.manager import OverAllocationError, StrategyNotFound

router = APIRouter(tags=["strategy"])
_ERR = {"model": schemas.ErrorEnvelope}
Ctn = Annotated[Container, Depends(get_container)]
UserId = Annotated[str, Header(alias="X-User-Id")]
AgentId = Annotated[str, Header(alias="X-Agent-Id")]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _view(c: StrategyContainer) -> schemas.StrategyView:
    return schemas.StrategyView(
        strategy_id=c.strategy_id, user_id=c.user_id, agent_id=c.agent_id, name=c.name,
        template_id=c.template_id, runtime_env=c.runtime_env, allocation=str(c.allocation),
        capital_capacity=str(c.capital_capacity), strategy_capacity=str(c.strategy_capacity),
        max_leverage=str(c.max_leverage),
        allowed_assets=list(c.allowed_assets) if c.allowed_assets is not None else None,
        expires_at=c.expires_at, state=c.state.value, version=c.version,
    )


@router.post("/strategies", response_model=schemas.StrategyView,
             status_code=status.HTTP_201_CREATED, responses={400: _ERR, 409: _ERR})
async def create_strategy(
    body: schemas.StrategyCreateRequest, container: Ctn,
    x_user_id: UserId = "u_dev", x_agent_id: AgentId = "agent-test-001",
):
    try:
        c = await container.strategy_manager.create(
            user_id=x_user_id, agent_id=x_agent_id, name=body.name, template_id=body.template_id,
            runtime_env=body.runtime_env, allocation=body.allocation,
            capital_capacity=body.capital_capacity, strategy_capacity=body.strategy_capacity,
            max_leverage=body.max_leverage,
            allowed_assets=tuple(body.allowed_assets) if body.allowed_assets else None,
            expires_at=body.expires_at, now=_now(),
        )
    except ValueError as exc:
        return error_response("INVALID_REQUEST", f"策略参数非法：{exc}", 400)
    except OverAllocationError as exc:
        return error_response("POLICY_VIOLATION", str(exc), 409)
    return _view(c)


@router.get("/strategies", response_model=schemas.StrategyList)
async def list_strategies(container: Ctn, x_user_id: UserId = "u_dev", x_agent_id: AgentId = "agent-test-001"):
    items = await container.strategy_manager.list(x_user_id, x_agent_id)
    return schemas.StrategyList(items=[_view(c) for c in items])


@router.get("/strategies/{strategy_id}", response_model=schemas.StrategyView, responses={404: _ERR})
async def get_strategy(strategy_id: str, container: Ctn, x_user_id: UserId = "u_dev", x_agent_id: AgentId = "agent-test-001"):
    try:
        c = await container.strategy_manager.get_owned(strategy_id, x_user_id, x_agent_id)
    except StrategyNotFound:
        return error_response("SESSION_NOT_FOUND", "策略不存在", 404)
    return _view(c)


async def _do_transition(container, strategy_id, user_id, agent_id, method):
    try:
        c = await getattr(container.strategy_manager, method)(strategy_id, user_id, agent_id, _now())
    except StrategyNotFound:
        return error_response("SESSION_NOT_FOUND", "策略不存在", 404)
    except IllegalStateTransition as exc:
        return error_response("INVALID_REQUEST", str(exc), 409)
    return _view(c)


@router.post("/strategies/{strategy_id}/pause", response_model=schemas.StrategyView, responses={404: _ERR, 409: _ERR})
async def pause_strategy(strategy_id: str, container: Ctn, x_user_id: UserId = "u_dev", x_agent_id: AgentId = "agent-test-001"):
    return await _do_transition(container, strategy_id, x_user_id, x_agent_id, "pause")


@router.post("/strategies/{strategy_id}/resume", response_model=schemas.StrategyView, responses={404: _ERR, 409: _ERR})
async def resume_strategy(strategy_id: str, container: Ctn, x_user_id: UserId = "u_dev", x_agent_id: AgentId = "agent-test-001"):
    return await _do_transition(container, strategy_id, x_user_id, x_agent_id, "resume")


@router.post("/strategies/{strategy_id}/terminate", response_model=schemas.StrategyView, responses={404: _ERR, 409: _ERR})
async def terminate_strategy(strategy_id: str, container: Ctn, x_user_id: UserId = "u_dev", x_agent_id: AgentId = "agent-test-001"):
    return await _do_transition(container, strategy_id, x_user_id, x_agent_id, "terminate")


@router.post("/strategies/{strategy_id}/allocate", response_model=schemas.StrategyView, responses={400: _ERR, 404: _ERR})
async def allocate_strategy(
    strategy_id: str, body: schemas.StrategyAllocateRequest, container: Ctn,
    x_user_id: UserId = "u_dev", x_agent_id: AgentId = "agent-test-001",
):
    try:
        c = await container.strategy_manager.allocate(strategy_id, x_user_id, x_agent_id, body.allocation, _now())
    except StrategyNotFound:
        return error_response("SESSION_NOT_FOUND", "策略不存在", 404)
    except ValueError as exc:
        return error_response("INVALID_REQUEST", f"额度非法：{exc}", 400)
    return _view(c)
```

- [ ] **Step 3d: 挂载 router（`app/main.py`）**

在已有 router include 附近加：

```python
from app.api.strategy import router as strategy_router
app.include_router(strategy_router, prefix="/v1/agent")
```
（与现有 `/v1/agent` 前缀一致；确认不与 agent_router 路径冲突——strategies 是新子路径。）

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_strategy_api.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/container.py app/api/strategy.py app/main.py app/api/schemas.py tests/test_strategy_api.py
git commit -m "feat(strategy): DI 装配 + REST 管理端点（CRUD + 生命周期 + 额度）"
```

---

### Task 5: 策略上下文内交易端点（接入执行链路）

**Files:**
- Modify: `app/api/strategy.py`（加 `POST /strategies/{id}/trade-draft`）
- Test: `tests/test_strategy_trade_draft_api.py`

**Interfaces:**
- Consumes: `StrategyManager.get_owned`/`ensure_tradable`/`derive_mandate`（Task 3）、`discover_trading_account`/`parse_trade_intent`/`build_draft_request`/`render_confirmation_card`（`app/tools/agentos/trade_flow.py`）、`PolicyEngine`/`PolicyViolationError`（`app/policies/engine.py`）、`_map_upstream_error`（`app/api/agent.py`）、`args_hash`（`app/audit/hashing.py`）、`AuditRecord`（`app/audit/store.py`）
- Produces: `POST /v1/agent/strategies/{id}/trade-draft`（body `{text}`）→ `schemas.TradeDraftCard`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_strategy_trade_draft_api.py
import httpx
import pytest

from app.agents.llm import LLMResult
from app.config import Settings
from app.container import build_container
from app.main import create_app
from app.tools.agentos.client import AgentOsStagingClient

_HDR = {"X-User-Id": "u1", "X-Agent-Id": "a1"}
BASE = "https://api-staging.agentos-app.app"
GOOD = '{"symbol":"BTC","side":"BUY","quantity":"0.001","limit_price":"3000","leverage":"2"}'
_ACCTS = [{"status": "ACTIVE", "unified_account_id": "uac_d", "venue_account_id": "vac_d"}]


class StubLLM:
    def __init__(self, t): self._t = t
    async def complete(self, s, m, tools): return LLMResult(text=self._t)


def _upstream(create_status=201, create_body=None):
    def handler(req):
        if req.url.path.endswith("/agent-trading-accounts"):
            return httpx.Response(200, json={"data": _ACCTS})
        return httpx.Response(create_status, json=create_body or {"data": {
            "id": "draft_s", "version": 1, "symbol": "BTC", "side": "BUY",
            "quantity": "0.001", "limit_price": "3000", "status": "PENDING_CONFIRMATION"}})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _app(llm_text=GOOD, upstream=None):
    c = build_container(Settings(provider_mode="fake"))
    c.agentos_client = AgentOsStagingClient(BASE, token="T", client=upstream or _upstream())
    c.llm = StubLLM(llm_text)
    app = create_app(); app.state.container = c
    return app, c


def _api(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _mk_body(**over):
    b = dict(name="s", template_id="t", runtime_env="paper", allocation="100000",
             capital_capacity="100000", strategy_capacity="100000", max_leverage="3")
    b.update(over); return b


async def _new_strategy(api, **over):
    r = await api.post("/v1/agent/strategies", json=_mk_body(**over), headers=_HDR)
    return r.json()["strategy_id"]


async def test_strategy_trade_draft_happy():
    app, c = _app()
    async with _api(app) as api:
        sid = await _new_strategy(api)
        r = await api.post(f"/v1/agent/strategies/{sid}/trade-draft", json={"text": "买 0.001 BTC 限价 3000"}, headers=_HDR)
        assert r.status_code == 201, r.text
        assert r.json()["draft_id"] == "draft_s"
        # 审计留痕
        recs = await c.audit_store.list_by_session(sid)
        assert any(rr.result_ref == "draft_s" for rr in recs)


async def test_trade_draft_paused_strategy_409():
    app, c = _app()
    async with _api(app) as api:
        sid = await _new_strategy(api)
        await api.post(f"/v1/agent/strategies/{sid}/pause", headers=_HDR)
        r = await api.post(f"/v1/agent/strategies/{sid}/trade-draft", json={"text": "买"}, headers=_HDR)
        assert r.status_code == 409


async def test_trade_draft_leverage_over_mandate_403():
    # 策略 max_leverage=1，但意图 leverage=5 → 派生 mandate 预检拒
    app, c = _app(llm_text='{"symbol":"BTC","side":"BUY","quantity":"0.001","limit_price":"3000","leverage":"5"}')
    async with _api(app) as api:
        sid = await _new_strategy(api, max_leverage="1")
        r = await api.post(f"/v1/agent/strategies/{sid}/trade-draft", json={"text": "5x 买"}, headers=_HDR)
        assert r.status_code == 403
        assert r.json()["code"] == "POLICY_VIOLATION"


async def test_trade_draft_strategy_not_found_404():
    app, c = _app()
    async with _api(app) as api:
        r = await api.post("/v1/agent/strategies/nope/trade-draft", json={"text": "买"}, headers=_HDR)
        assert r.status_code == 404


async def test_trade_draft_not_configured_503():
    c = build_container(Settings(provider_mode="fake"))  # agentos_client=None
    from app.agents.llm import FakeLLM
    c.llm = FakeLLM()
    app = create_app(); app.state.container = c
    async with _api(app) as api:
        sid = await _new_strategy(api)
        r = await api.post(f"/v1/agent/strategies/{sid}/trade-draft", json={"text": "买"}, headers=_HDR)
        assert r.status_code == 503
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest tests/test_strategy_trade_draft_api.py -q`
Expected: FAIL（端点不存在 → 404/405）

- [ ] **Step 3: 实现端点（追加到 `app/api/strategy.py`）**

顶部补导入：

```python
import httpx

from app.agents.llm import LLMMessage  # noqa: F401  (仅类型参考，可省)
from app.api.agent import _map_upstream_error
from app.audit.hashing import args_hash
from app.audit.store import AuditRecord
from app.api.errors import new_trace_id
from app.policies.engine import PolicyEngine, PolicyViolationError
from app.strategy.manager import derive_mandate
from app.tools.agentos.trade_flow import (
    IntentParseError,
    build_draft_request,
    discover_trading_account,
    parse_trade_intent,
    render_confirmation_card,
)

_AGENTOS_DRAFT_TOOL = "agentos_create_trade_draft"
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
```

端点：

```python
@router.post("/strategies/{strategy_id}/trade-draft", response_model=schemas.TradeDraftCard,
             status_code=status.HTTP_201_CREATED, responses={400: _ERR, 403: _ERR, 404: _ERR, 409: _ERR, 502: _ERR, 503: _ERR})
async def strategy_trade_draft(
    strategy_id: str, body: schemas.TradeDraftCardRequest, container: Ctn,
    x_user_id: UserId = "u_dev", x_agent_id: AgentId = "agent-test-001",
    idempotency_key: IdempotencyKey = None,
):
    now = _now()
    trace_id = new_trace_id()
    try:
        strat = await container.strategy_manager.get_owned(strategy_id, x_user_id, x_agent_id)
    except StrategyNotFound:
        return error_response("SESSION_NOT_FOUND", "策略不存在", 404)
    try:
        container.strategy_manager.ensure_tradable(strat, now)
    except IllegalStateTransition as exc:
        return error_response("INVALID_REQUEST", str(exc), 409)
    if container.agentos_client is None:
        return error_response("INTERNAL", "AgentOS 真链路未配置（缺 AGENT_STAGING_TOKEN）", 503)

    try:
        unified, venue = await discover_trading_account(container.agentos_client)
    except httpx.HTTPStatusError as exc:
        return _map_upstream_error(exc)

    try:
        intent = await parse_trade_intent(container.llm, body.text)
    except IntentParseError as exc:
        return error_response("INVALID_REQUEST", f"无法解析交易意图：{exc}", 400)

    request = build_draft_request(
        intent, unified_account_id=unified, venue_account_id=venue,
        default_leverage=container.settings.agentos_default_leverage,
        default_max_slippage=container.settings.agentos_default_max_slippage,
    )
    body_dict = request.to_body()
    # 策略级 runtime 预检（Core 仍权威）
    decision = PolicyEngine(derive_mandate(strat)).check_trade_draft(
        {**body_dict, "notional": None, "qty": body_dict["quantity"], "price": body_dict["limit_price"]},
        now,
    )
    if not decision.allowed:
        return error_response("POLICY_VIOLATION", decision.reason, 403)

    try:
        draft = await container.agentos_client.create_trade_draft(body_dict, idempotency_key=idempotency_key)
    except httpx.HTTPStatusError as exc:
        return _map_upstream_error(exc)

    data = draft.get("data") or {}
    draft_id = data.get("id")
    await container.audit_store.record_tool_call(
        AuditRecord(
            trace_id=trace_id, session_id=strategy_id, tool_name=_AGENTOS_DRAFT_TOOL,
            args_hash=args_hash({"text": body.text, "strategy_id": strategy_id}),
            result_ref=draft_id or "no_draft", created_at=now, status="ok",
        )
    )
    return schemas.TradeDraftCard(
        card=render_confirmation_card(draft), draft_id=draft_id,
        draft_version=data.get("version"), status=data.get("status"), draft=draft, trace_id=trace_id,
    )
```

> 注：`check_trade_draft` 期望 `symbol`/`leverage`/`notional`/`qty`/`price`；`request.to_body()` 已含 `symbol`/`leverage`/`quantity`/`limit_price`，上面显式补 `qty`/`price` 供名义额度计算（名义 = qty*price 与 `allocation` 比）。

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest tests/test_strategy_trade_draft_api.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add app/api/strategy.py tests/test_strategy_trade_draft_api.py
git commit -m "feat(strategy): 策略上下文内交易端点（发现账户+派生mandate预检+建单+审计）"
```

---

### Task 6: 覆盖率门槛纳入 `app.strategy` + 全量验收

**Files:**
- Modify: `.github/workflows/deploy-staging.yml`（`--cov` 追加 `app.strategy`）

**Interfaces:** 无新增。

- [ ] **Step 1: 本地全量 + 策略模块覆盖**

Run:
```bash
.venv/bin/python -m pytest --cov=app.tools.agentos --cov=app.strategy --cov-branch --cov-report=term-missing -q
```
Expected: 全绿；`app/strategy/*` 行+分支 **100%**（0 miss）。若有 miss，补测试至 100% 再继续。

- [ ] **Step 2: 更新 CI 门槛**

`.github/workflows/deploy-staging.yml` 的测试命令改为：

```yaml
          python -m pytest -v --cov=app.tools.agentos --cov=app.strategy --cov-branch --cov-fail-under=100
          ruff check .
          mypy app
```

- [ ] **Step 3: 本地跑 CI 同命令验收**

Run:
```bash
.venv/bin/python -m pytest --cov=app.tools.agentos --cov=app.strategy --cov-branch --cov-fail-under=100 -q && .venv/bin/ruff check . && .venv/bin/mypy app
```
Expected: 覆盖率达标（exit 0）；ruff/mypy 通过（如既有 mypy 基线有告警，仅确保不新增 `app/strategy` 相关错误）。

- [ ] **Step 4: 提交**

```bash
git add .github/workflows/deploy-staging.yml
git commit -m "ci(strategy): 覆盖率门槛纳入 app.strategy（行+分支 100%）"
```

---

## Self-Review

**1. Spec coverage：**
- 隔离模型（1 Agent=1 账户、策略逻辑容器）→ 贯穿 container/manager/trade-draft ✓
- 数据模型全字段（allocation/capital_capacity/strategy_capacity/template_id/runtime_env + agent_id/state/version）→ Task 1 ✓
- 状态机 → Task 1 ✓
- 模块拆分（Container/Store+Protocol/Manager/API）→ Task 1/2/3/4 ✓
- 8 端点 API 契约 → Task 4（1-7）+ Task 5（8）✓
- 执行接线 + 派生 mandate 预检 + 聚合额度 → Task 3/5 ✓
- 错误处理（400/403/404/409/502/503）→ Task 4/5 ✓
- 测试与 100% 覆盖 → 各任务 + Task 6 ✓
- 玩法算法留桩、稳定性留后 → 非目标，未纳入 ✓

**2. Placeholder scan：** 无 TBD/TODO；每步含实际代码。✓

**3. Type consistency：** `StrategyContainer` 字段/方法名（`pause/resume/terminate/expire/with_allocation/is_expired`）跨 Task 1/3/5 一致；`StrategyManager` 方法签名（`create/get_owned/list/pause/resume/terminate/allocate/ensure_tradable`）Task 3 定义、Task 4/5 调用一致；`derive_mandate` Task 3 定义、Task 5 用；`StrategyView`/`StrategyCreateRequest`/`StrategyAllocateRequest` Task 4 定义并使用。✓

> 待执行时注意：`_map_upstream_error` 从 `app/api/agent.py` 导入（已存在）；`TradeDraftCardRequest`/`TradeDraftCard` 复用现有 schemas。
