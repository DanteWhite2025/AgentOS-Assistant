"""净值采样器 —— 上游 payload 解析、按额度分摊、单轮采样落盘。"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.strategy.container import StrategyContainer, StrategyState
from app.strategy.equity_store import InMemoryEquityStore
from app.strategy.sampler import (
    EquitySampler,
    EquityUnavailable,
    allocate_equity,
    extract_equity,
    extract_realized_pnl,
)
from app.strategy.store import InMemoryStrategyStore

_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _strategy(sid, allocation, state=StrategyState.ACTIVE, user="u1", agent="a1"):
    return StrategyContainer(
        strategy_id=sid, user_id=user, agent_id=agent, name=sid, template_id="tpl",
        runtime_env="paper", allocation=allocation, capital_capacity="100000",
        strategy_capacity="100000", max_leverage="3", state=state,
        created_at=_NOW, updated_at=_NOW,
    )


class _FakeAgentOs:
    """只实现采样器用到的两个方法。"""

    def __init__(self, summary, accounts=None):
        self._summary = summary
        self._accounts = accounts if accounts is not None else {
            "data": [
                {"unified_account_id": "ua_1", "venue_account_id": "va_1", "status": "ACTIVE"}
            ]
        }
        self.summary_calls = []

    async def get_agent_trading_accounts(self):
        return self._accounts

    async def get_account_summary(self, unified_account_id):
        self.summary_calls.append(unified_account_id)
        return self._summary


# --- extract_equity ------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"venue_equity": "22804.63"}},
        {"venue_equity_usd": "22804.63"},
    ],
)
def test_extract_equity_accepts_only_explicit_venue_equity(payload):
    assert extract_equity(payload) == Decimal("22804.63")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": {}},
        {"data": {"positions": []}},
        None,
        {"data": {"balances": []}},
        {"data": {"balances": "not-a-list"}},
        {"data": {"balances": [{"usd_value": "10"}, "not-a-dict"]}},
        {"data": {"balances": [{"usd_value": "n/a"}]}},
        {"data": {"balances": [{"other": "10"}]}},
        {"data": {"total_equity": "22804.63"}},
        {"data": {"equity": "22804.63"}},
        {"data": {"balance": {"total_equity_minor": "22804630000"}}},
        {
            "data": {
                "balance": {"asset": "USDC", "available_minor": "22804630000"},
                "positions": [],
                "orders": [],
            }
        },
    ],
)
def test_extract_equity_raises_when_absent(payload):
    """拿不到就报错，绝不静默写 0 —— 0 会被大屏当成真实亏损。"""
    with pytest.raises(EquityUnavailable):
        extract_equity(payload)


def test_extract_equity_rejects_non_numeric():
    with pytest.raises(EquityUnavailable) as captured:
        extract_equity({"data": {"venue_equity": "n/a"}})
    assert captured.value.to_dict() == {
        "code": "EQUITY_UNAVAILABLE",
        "status": "NOT_AVAILABLE",
        "source": "CORE_ACCOUNT_SUMMARY",
        "reason": "AUTHORITATIVE_VENUE_EQUITY_MISSING",
    }


def test_extract_equity_rejects_boolean() -> None:
    with pytest.raises(EquityUnavailable):
        extract_equity({"data": {"venue_equity": True}})


def test_extract_realized_pnl_optional():
    assert extract_realized_pnl({"data": {"venue_realized_pnl": "45.20"}}) == Decimal(
        "45.20"
    )
    assert extract_realized_pnl({"data": {"realized_pnl": -12}}) is None
    assert extract_realized_pnl({"data": {}}) is None
    assert extract_realized_pnl({"data": {"realized_pnl": "n/a"}}) is None
    assert extract_realized_pnl({"data": {"realized_pnl": None}}) is None
    assert extract_realized_pnl({"data": {"realized_pnl": True}}) is None


# --- allocate_equity -----------------------------------------------------


def test_allocate_splits_by_allocation_weight():
    got = allocate_equity(Decimal(30000), [_strategy("s1", "1000"), _strategy("s2", "2000")])
    assert got["s1"] == Decimal(10000)
    assert got["s2"] == Decimal(20000)


def test_allocate_skips_non_active():
    got = allocate_equity(
        Decimal(10000),
        [_strategy("s1", "1000"), _strategy("s2", "1000", state=StrategyState.PAUSED)],
    )
    assert got == {"s1": Decimal(10000)}


def test_allocate_returns_empty_when_no_allocation():
    assert allocate_equity(Decimal(10000), [_strategy("s1", "0")]) == {}
    assert allocate_equity(Decimal(10000), []) == {}


def test_allocate_weights_sum_back_to_total():
    strategies = [_strategy("s1", "1"), _strategy("s2", "1"), _strategy("s3", "1")]
    got = allocate_equity(Decimal(100), strategies)
    assert sum(got.values()) == Decimal(100)  # 余数补给最后一个，不丢钱


# --- EquitySampler -------------------------------------------------------


async def _sampler(summary, strategies):
    strategy_store = InMemoryStrategyStore()
    for s in strategies:
        await strategy_store.create(s)
    equity_store = InMemoryEquityStore()
    client = _FakeAgentOs(summary)
    return EquitySampler(client, strategy_store, equity_store), equity_store, client


async def test_sample_once_writes_one_snapshot_per_active_strategy():
    sampler, equity_store, client = await _sampler(
        {"data": {"venue_equity": "30000", "venue_realized_pnl": "45.2"}},
        [_strategy("s1", "1000"), _strategy("s2", "2000")],
    )
    written = await sampler.sample_once(_NOW)

    assert client.summary_calls == ["ua_1"]
    assert {s.strategy_id for s in written} == {"s1", "s2"}
    stored = await equity_store.list_range("u1", "a1")
    assert [s.equity for s in stored] == [Decimal(10000), Decimal(20000)]
    assert stored[0].taken_at == _NOW
    # 已实现盈亏同样按额度权重分摊（8 位小数，余数补最后一个）
    assert stored[0].realized_pnl == Decimal("15.06666667")
    assert stored[0].realized_pnl + stored[1].realized_pnl == Decimal("45.2")


async def test_sample_once_without_strategies_writes_nothing():
    sampler, equity_store, _ = await _sampler({"data": {"venue_equity": "30000"}}, [])
    assert await sampler.sample_once(_NOW) == []
    assert await equity_store.list_range("u1", "a1") == []


async def test_sample_once_propagates_equity_unavailable():
    sampler, equity_store, _ = await _sampler({"data": {}}, [_strategy("s1", "1000")])
    with pytest.raises(EquityUnavailable):
        await sampler.sample_once(_NOW)
    assert await equity_store.list_range("u1", "a1") == []


async def test_sample_once_groups_by_owner():
    """多个 (user, agent) 归属的策略各自记账，不串号。"""
    sampler, equity_store, _ = await _sampler(
        {"data": {"venue_equity": "30000"}},
        [_strategy("s1", "1000"), _strategy("s2", "1000", user="u2", agent="a2")],
    )
    await sampler.sample_once(_NOW)
    assert [s.strategy_id for s in await equity_store.list_range("u1", "a1")] == ["s1"]
    assert [s.strategy_id for s in await equity_store.list_range("u2", "a2")] == ["s2"]


async def test_sample_once_skips_realized_split_when_upstream_omits_it():
    sampler, equity_store, _ = await _sampler(
        {"data": {"venue_equity": "1000"}}, [_strategy("s1", "1000")]
    )
    await sampler.sample_once(_NOW)
    assert (await equity_store.list_range("u1", "a1"))[0].realized_pnl is None


# --- run_forever ---------------------------------------------------------


async def test_run_forever_keeps_going_after_a_failed_round():
    """上游抖动不能把常驻采样打死，否则曲线从此断流。"""
    sampler, _, _ = await _sampler({"data": {"venue_equity": "1000"}}, [_strategy("s1", "1000")])
    calls = 0
    second_round = asyncio.Event()

    async def flaky(now=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("上游 502")
        second_round.set()
        return []

    sampler.sample_once = flaky
    task = asyncio.create_task(sampler.run_forever(0.001))
    await asyncio.wait_for(second_round.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls >= 2


async def test_run_forever_propagates_cancellation_during_sampling():
    """采样进行中被取消要如实向上抛，不能被兜底 except 吞成一次「失败重试」。"""
    sampler, _, _ = await _sampler({"data": {"venue_equity": "1000"}}, [_strategy("s1", "1000")])
    entered = asyncio.Event()

    async def hang(now=None):
        entered.set()
        await asyncio.Event().wait()  # 永不返回，等着被取消

    sampler.sample_once = hang
    task = asyncio.create_task(sampler.run_forever(0.001))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_run_forever_logs_equity_unavailable_once_without_traceback(caplog) -> None:
    sampler, _, _ = await _sampler(
        {"data": {"balance": {}, "positions": [], "orders": []}},
        [_strategy("s1", "1000")],
    )
    repeated = asyncio.Event()
    calls = 0

    async def unavailable(now=None):
        nonlocal calls
        calls += 1
        if calls >= 2:
            repeated.set()
        raise EquityUnavailable

    sampler.sample_once = unavailable
    caplog.set_level("INFO", logger="app.strategy.sampler")
    task = asyncio.create_task(sampler.run_forever(0.001))
    await asyncio.wait_for(repeated.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    records = [
        record for record in caplog.records if record.message == "equity_sampler source data unavailable"
    ]
    assert len(records) == 1
    assert records[0].code == "EQUITY_UNAVAILABLE"
    assert records[0].status == "NOT_AVAILABLE"
    assert records[0].exc_info is None
