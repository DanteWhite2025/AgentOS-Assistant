from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents.runner import AgentProfile, AgentRunner
from app.audit.store import InMemoryAuditStore
from app.runtime.session import Scope, Session
from app.tools.agentos.models import CreateSpotSwapDraftRequest, MarketInstrument
from app.tools.agentos.trade_flow import (
    MarketInstrumentResolutionError,
    StrategyBindingError,
    TradingReadinessError,
    catalog_asset_query,
    parse_market_catalog,
    resolve_strategy_execution_context,
    resolve_unique_market_instrument,
)
from app.tools.builtin import build_default_registry
from app.tools.core_client import MarketTickerResponse
from app.tools.executor import ToolExecutor


def _readiness(*, symbols: list[str] | None = None, strategy_id: str = "strategy_multi"):
    return {
        "data": {
            "unified_account_id": "uac_1",
            "venue_account_id": "vac_1",
            "ledger_balance_by_asset": {"USDT": "25000000", "USDC": "25000000"},
            "venue_balance_by_asset": {"USDC": "25"},
            "venue_margin_minor": "25000000",
            "daily_notional_limit_minor": "50000000",
            "daily_notional_used_minor": "0",
            "daily_notional_remaining_minor": "50000000",
            "agent_trading_account": {
                "id": "atac_1",
                "agent_id": "agent-test-001",
                "unified_account_id": "uac_1",
                "venue_account_id": "vac_1",
                "status": "ACTIVE",
            },
            "mandate": {
                "id": "mandate_1",
                "status": "ACTIVE",
                "confirmation_mode": "ALWAYS_CONFIRM",
                "max_notional_per_order_minor": 10_000_000,
                "max_daily_notional_minor": 50_000_000,
                "max_total_exposure_minor": 10_000_000,
                "allowed_symbols": symbols or ["BTC", "ETH", "BNB", "USDT", "USDC"],
                "allowed_order_types": ["LIMIT", "EXACT_INPUT"],
                "max_leverage": "2",
                "max_slippage": "0.01",
            },
            "strategy_binding": {
                "strategy_id": strategy_id,
                "agent_trading_account_id": "atac_1",
                "venue_account_id": "vac_1",
                "mandate_id": "mandate_1",
                "capital_limit_minor": 10_000_000,
                "status": "ACTIVE",
            },
            "can_create_agent_draft": True,
            "blockers": [],
            "stale": False,
        },
        "request_id": "req_ready",
    }


def _binding(*, assets: list[str] | None = None, market_types: list[str] | None = None):
    return {
        "strategy_id": "strategy_multi",
        "agent_id": "agent-test-001",
        "agent_trading_account_id": "atac_1",
        "venue_account_id": "vac_1",
        "mandate_id": "mandate_1",
        "authorization_grant_id": "grant_1",
        "capital_limit_minor": 10_000_000,
        "market_types": market_types or ["SPOT", "PERP"],
        "allowed_assets": assets or ["BTC", "ETH", "BNB", "USDT", "USDC"],
        "allowed_instruments": [],
        "max_order_notional_minor": 10_000_000,
        "max_daily_notional_minor": 50_000_000,
        "max_leverage": "2",
        "max_slippage": "0.01",
        "status": "ACTIVE",
    }


def _perp(symbol: str, *, stale: bool = False):
    return {
        "instrument_id": f"perp_hl_{symbol.lower()}",
        "market_type": "PERP",
        "venue": "HYPERLIQUID",
        "environment": "TESTNET",
        "display_symbol": f"{symbol}-PERP",
        "venue_symbol": symbol,
        "base_asset": symbol,
        "quote_asset": "USDC",
        "trade_supported": True,
        "supported_order_types": ["LIMIT"],
        "price_decimals": 2,
        "size_decimals": 5,
        "minimum_quantity": "0.00001",
        "minimum_notional": "1",
        "maximum_leverage": "2",
        "status": "ACTIVE",
        "stale": stale,
        "as_of": "2026-08-18T00:00:00Z",
    }


def _spot():
    return {
        "instrument_id": "spot_bsc_bnb_usdt",
        "market_type": "SPOT",
        "venue": "PANCAKESWAP",
        "environment": "TESTNET",
        "display_symbol": "BNB/USDT",
        "venue_symbol": "BNB-USDT",
        "base_asset": "BNB",
        "quote_asset": "USDT",
        "trade_supported": True,
        "supported_order_types": ["EXACT_INPUT"],
        "price_decimals": 8,
        "size_decimals": 18,
        "minimum_quantity": "0.000000000000000001",
        "minimum_notional": "0.01",
        "maximum_leverage": "1",
        "status": "ACTIVE",
        "stale": False,
        "as_of": "2026-08-18T00:00:00Z",
        "chain_id": 97,
        "token_in_address": "0x1111111111111111111111111111111111111111",
        "token_out_address": "0x2222222222222222222222222222222222222222",
        "token_in_decimals": 18,
        "token_out_decimals": 18,
        "token_in_ledger_decimals": 18,
        "token_out_ledger_decimals": 18,
    }


def _spot_usdt_bnb():
    value = _spot() | {
        "instrument_id": "spot_bsc_usdt_bnb",
        "display_symbol": "USDT/BNB",
        "venue_symbol": "USDT-BNB",
        "base_asset": "USDT",
        "quote_asset": "BNB",
        "token_in_address": _spot()["token_out_address"],
        "token_out_address": _spot()["token_in_address"],
        "token_in_decimals": _spot()["token_out_decimals"],
        "token_out_decimals": _spot()["token_in_decimals"],
        "token_in_ledger_decimals": 6,
        "token_out_ledger_decimals": _spot()["token_in_ledger_decimals"],
    }
    return value


def _ticker(symbol: str, *, stale: bool = False) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "last_price": "3000",
        "mark_price": "3000",
        "oracle_price": "3000",
        "change_24h": "1",
        "change_percent_24h": "0.01",
        "high_24h": "3100",
        "low_24h": "2900",
        "volume_24h": "10",
        "quote_volume_24h": "30000",
        "funding_rate": "0.0001",
        "open_interest": "20",
        "source": "HYPERLIQUID_INFO_API",
        "environment": "MAINNET",
        "as_of": "2026-08-18T00:00:00+00:00",
        "stale": stale,
    }


class CatalogMarketCore:
    def __init__(self, *, stale: bool = False):
        self.calls: list[list[str]] = []
        self.stale = stale

    async def get_market_tickers(self, symbols, request_id=None):
        self.calls.append(list(symbols))
        return MarketTickerResponse.model_validate(
            {
                "data": [_ticker(symbol, stale=self.stale) for symbol in symbols],
                "meta": {
                    "source": "HYPERLIQUID",
                    "environment": "MAINNET",
                    "as_of": "2026-08-18T00:00:00+00:00",
                    "stale": self.stale,
                    "next_cursor": None,
                },
                "request_id": "req_tickers",
            }
        )

    async def get_resource(self, resource, user_id, params):
        return {}

    async def create_trade_draft(self, user_id, args):
        raise AssertionError("legacy Core draft path must not be used")


class StubAgentOs:
    def __init__(
        self,
        *,
        readiness=None,
        binding=None,
        instruments=None,
        write_delay=0,
        catalog_stale: bool = False,
    ):
        self.readiness = readiness or _readiness()
        self.binding = binding or _binding()
        self.instruments = instruments or {
            "perp_hl_btc": _perp("BTC"),
            "perp_hl_eth": _perp("ETH"),
            "perp_hl_hype": _perp("HYPE"),
            "spot_bsc_bnb_usdt": _spot(),
            "spot_bsc_usdt_bnb": _spot_usdt_bnb(),
        }
        self.catalog_stale = catalog_stale
        self.catalog_queries: list[dict[str, Any]] = []
        self.write_delay = write_delay
        self.perp_writes = 0
        self.spot_writes = 0
        self.confirm_calls = 0
        self.last_perp_body = None
        self.last_spot_body = None
        self.user_balance_queries: list[tuple[str, str]] = []

    async def get_user_trading_readiness(self, token):
        return self.readiness

    async def get_strategy_binding(self, strategy_id):
        return {"data": self.binding}

    async def get_agent_trading_accounts(self):
        return {"data": [self.readiness["data"]["agent_trading_account"]]}

    async def get_market_instrument(self, instrument_id):
        return {"data": self.instruments[instrument_id]}

    async def get_market_catalog(self, **kwargs):
        self.catalog_queries.append(dict(kwargs))
        values = list(self.instruments.values())
        return {
            "data": values,
            "meta": {"stale": self.catalog_stale},
            "request_id": "req_catalog",
        }

    async def get_account_summary(self, account_id):
        return {"data": {"balance": {}, "positions": [], "orders": []}}

    async def get_balances(self, account_id):
        return {"data": []}

    async def get_user_balances(self, account_id, user_bearer_token):
        self.user_balance_queries.append((account_id, user_bearer_token))
        return {"data": [{"asset": "USDC", "decimals": 6, "available_minor": "1000000"}]}

    async def get_positions(self):
        return {"data": []}

    async def get_orders(self):
        return {"data": []}

    async def create_agent_trade_intent(self, body, *, idempotency_key, user_bearer_token):
        self.perp_writes += 1
        self.last_perp_body = body
        assert user_bearer_token == "transient-user-token"
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        return {"data": {"id": "draft_perp_eth", "status": "PENDING_USER"}}

    async def create_spot_swap_draft(self, body, *, idempotency_key, user_bearer_token):
        self.spot_writes += 1
        self.last_spot_body = body
        assert user_bearer_token == "transient-user-token"
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        assert body["sell_token"] in (_spot()["token_in_address"], _spot()["token_out_address"])
        assert body["buy_token"] in (_spot()["token_in_address"], _spot()["token_out_address"])
        return {"data": {"id": "draft_spot_bnb_usdt", "version": 2, "status": "DRAFT"}}


def _executor(agentos: StubAgentOs, market: CatalogMarketCore | None = None):
    registry = build_default_registry()
    return ToolExecutor(
        registry,
        market or CatalogMarketCore(),
        agentos_client=agentos,  # type: ignore[arg-type]
        expected_agent_id="agent-test-001",
    ), registry


async def test_queries_btc_and_eth_tickers_without_fixed_single_symbol():
    agentos = StubAgentOs()
    market = CatalogMarketCore()
    executor, registry = _executor(agentos, market)
    output = await executor.execute(
        "market_tickers",
        "u1",
        {"symbols": ["BTC", "ETH"]},
        list(registry.names()),
    )
    assert [item["symbol"] for item in output["data"]] == ["BTC", "ETH"]
    assert market.calls == [["BTC", "ETH"]]
    assert agentos.catalog_queries == []
    assert "instruments" not in output


@pytest.mark.parametrize(
    ("model_symbol", "venue_symbol"),
    [
        ("BTCUSDC", "BTC"),
        ("BTC-USDC", "BTC"),
        ("BTC/USDC", "BTC"),
        ("BTCUSDT", "BTC"),
        ("BTCUSD", "BTC"),
        ("ETHUSD", "ETH"),
        ("ETHUSDC", "ETH"),
        ("HYPE-USDC", "HYPE"),
    ],
)
async def test_model_pair_symbol_normalizes_without_execution_catalog(
    model_symbol: str, venue_symbol: str
) -> None:
    agentos = StubAgentOs()
    market = CatalogMarketCore()
    executor, registry = _executor(agentos, market)

    output = await executor.execute(
        "get_market_price",
        "u1",
        {"symbol": model_symbol},
        list(registry.names()),
    )

    assert agentos.catalog_queries == []
    assert market.calls == [[venue_symbol]]
    assert output["symbol"] == venue_symbol
    assert "instrument" not in output


async def test_read_only_ticker_does_not_choose_spot_or_perp_execution_identity() -> None:
    spot_btc = {
        **_spot(),
        "instrument_id": "spot_btc_usdt",
        "display_symbol": "BTC/USDT",
        "venue_symbol": "BTC-USDT",
        "base_asset": "BTC",
    }
    agentos = StubAgentOs(
        instruments={
            "perp_hl_btc": _perp("BTC"),
            "spot_btc_usdt": spot_btc,
        }
    )
    market = CatalogMarketCore()
    executor, registry = _executor(agentos, market)

    output = await executor.execute(
        "get_market_price", "u1", {"symbol": "BTC/USDT"}, list(registry.names())
    )

    assert agentos.catalog_queries == []
    assert output["symbol"] == "BTC"
    assert "instrument" not in output


async def test_multiple_execution_catalog_matches_do_not_block_read_only_ticker() -> None:
    duplicate = {**_perp("BTC"), "instrument_id": "perp_other_btc", "venue_symbol": "BTC"}
    agentos = StubAgentOs(instruments={"perp_hl_btc": _perp("BTC"), "perp_other_btc": duplicate})
    market = CatalogMarketCore()
    executor, registry = _executor(agentos, market)

    output = await executor.execute(
        "market_tickers", "u1", {"symbols": ["BTCUSDC"]}, list(registry.names())
    )

    assert [item["symbol"] for item in output["data"]] == ["BTC"]
    assert agentos.catalog_queries == []


@pytest.mark.parametrize(
    ("instrument_changes", "catalog_stale"),
    [
        ({"stale": True}, False),
        ({"trade_supported": False}, False),
        ({"status": "INACTIVE"}, False),
        ({}, True),
    ],
)
async def test_execution_catalog_state_does_not_block_read_only_ticker(
    instrument_changes, catalog_stale
) -> None:
    agentos = StubAgentOs(
        instruments={"perp_hl_btc": {**_perp("BTC"), **instrument_changes}},
        catalog_stale=catalog_stale,
    )
    market = CatalogMarketCore()
    executor, registry = _executor(agentos, market)

    output = await executor.execute(
        "get_market_price", "u1", {"symbol": "BTCUSDC"}, list(registry.names())
    )

    assert output["symbol"] == "BTC"
    assert agentos.catalog_queries == []


@pytest.mark.parametrize(
    ("raw", "reason_code"),
    [
        ("", "MARKET_INSTRUMENT_INVALID"),
        ("-/-", "MARKET_INSTRUMENT_INVALID"),
        ("A" * 33, "MARKET_INSTRUMENT_INVALID"),
    ],
)
def test_catalog_asset_query_rejects_invalid_values(raw: str, reason_code: str) -> None:
    with pytest.raises(MarketInstrumentResolutionError) as captured:
        catalog_asset_query(raw)
    assert captured.value.reason_code == reason_code


@pytest.mark.parametrize(
    ("response", "market_type", "reason_code", "status_code"),
    [
        ({"data": [], "meta": {}}, "OPTIONS", "MARKET_TYPE_INVALID", 400),
        ({"data": None, "meta": {}}, "PERP", "MARKET_CATALOG_INVALID", 503),
        ({"data": [], "meta": {}}, "PERP", "MARKET_INSTRUMENT_NOT_FOUND", 400),
        (
            {
                "data": [
                    _perp("BTC"),
                    {**_perp("BTC"), "instrument_id": "perp_other_btc"},
                ],
                "meta": {"stale": False},
            },
            "PERP",
            "MARKET_INSTRUMENT_AMBIGUOUS",
            400,
        ),
        (
            {"data": [{**_perp("BTC"), "stale": True}], "meta": {"stale": False}},
            "PERP",
            "MARKET_INSTRUMENT_STALE",
            503,
        ),
        (
            {
                "data": [{**_perp("BTC"), "trade_supported": False}],
                "meta": {"stale": False},
            },
            "PERP",
            "MARKET_INSTRUMENT_NOT_TRADABLE",
            403,
        ),
        (
            {"data": [{**_perp("BTC"), "status": "INACTIVE"}], "meta": {"stale": False}},
            "PERP",
            "MARKET_INSTRUMENT_NOT_TRADABLE",
            403,
        ),
        (
            {"data": [None, _spot()], "meta": {"stale": False}},
            "PERP",
            "MARKET_INSTRUMENT_NOT_FOUND",
            400,
        ),
        (
            {
                "data": [{**_perp("BTC"), "price_decimals": "invalid"}],
                "meta": {"stale": False},
            },
            "PERP",
            "MARKET_CATALOG_INVALID",
            503,
        ),
    ],
)
def test_unique_market_resolution_classifies_invalid_catalog_results(
    response, market_type, reason_code, status_code
) -> None:
    with pytest.raises(MarketInstrumentResolutionError) as captured:
        resolve_unique_market_instrument(response, query="BTC", market_type=market_type)
    assert captured.value.reason_code == reason_code
    assert captured.value.status_code == status_code


def test_parse_market_catalog_accepts_empty_fresh_page() -> None:
    assert parse_market_catalog({"data": [], "meta": {"stale": False}}) == ()


async def test_balance_tool_uses_current_mobile_user_identity() -> None:
    agentos = StubAgentOs()
    executor, registry = _executor(agentos)

    output = await executor.execute(
        "get_balances",
        "u1",
        {},
        list(registry.names()),
        user_bearer_token="transient-user-token",
    )

    assert output["data"][0]["asset"] == "USDC"
    assert agentos.user_balance_queries == [
        (agentos.readiness["data"]["unified_account_id"], "transient-user-token")
    ]


async def test_creates_eth_perp_draft_from_core_owned_instrument_only():
    agentos = StubAgentOs()
    executor, registry = _executor(agentos)
    output = await executor.execute(
        "create_perp_trade_draft",
        "u1",
        {
            "symbol": "ETH",
            "side": "buy",
            "order_type": "limit",
            "notional_usdc": "9",
            "price": "3000",
            "leverage": "1",
        },
        list(registry.names()),
        user_bearer_token="transient-user-token",
        idempotency_key="intent-eth-perp-1",
    )
    assert output["core_draft_id"] == "draft_perp_eth"
    assert output["market_type"] == "PERP"
    assert output["instrument_id"] == "perp_hl_eth"
    assert output["execution_requested"] is False
    assert agentos.perp_writes == 1
    assert agentos.last_perp_body["symbol"] == "ETH"
    assert agentos.confirm_calls == 0


async def test_perp_draft_pair_like_symbol_uses_catalog_instrument_and_venue_symbol() -> None:
    agentos = StubAgentOs()
    market = CatalogMarketCore()
    executor, registry = _executor(agentos, market)

    output = await executor.execute(
        "create_perp_trade_draft",
        "u1",
        {
            "symbol": "BTCUSDC",
            "side": "buy",
            "order_type": "limit",
            "notional_usdc": "10",
            "price": "100000",
            "leverage": "1",
        },
        list(registry.names()),
        user_bearer_token="transient-user-token",
        idempotency_key="intent-btc-pair-symbol",
    )

    assert output["instrument_id"] == "perp_hl_btc"
    assert agentos.catalog_queries == [
        {
            "market_type": "PERP",
            "query": "BTC",
            "limit": 20,
            "trade_supported": True,
        }
    ]

    assert agentos.last_perp_body["symbol"] == "BTC"
    assert agentos.last_perp_body["symbol"] == "BTC"
    assert agentos.last_perp_body["leverage"] == "1"
    assert output["confirmation_mode"] == "ALWAYS_CONFIRM"
    assert output["execution_requested"] is False
    assert output["trade_intent_id"] == "draft_perp_eth"
    assert output["status"] == "PENDING_USER"
    assert agentos.confirm_calls == 0


async def test_stale_execution_catalog_still_blocks_pair_like_trade_draft() -> None:
    agentos = StubAgentOs(catalog_stale=True)
    market = CatalogMarketCore()
    executor, registry = _executor(agentos, market)

    with pytest.raises(MarketInstrumentResolutionError) as captured:
        await executor.execute(
            "create_perp_trade_draft",
            "u1",
            {
                "symbol": "BTCUSDC",
                "side": "buy",
                "order_type": "limit",
                "notional_usdc": "10",
                "price": "100000",
                "leverage": "1",
            },
            list(registry.names()),
            user_bearer_token="transient-user-token",
            idempotency_key="blocked-stale-catalog-draft",
        )

    assert captured.value.reason_code == "MARKET_CATALOG_STALE"
    assert captured.value.status_code == 503
    assert market.calls == []
    assert agentos.perp_writes == 0


async def test_creates_bnb_to_usdt_spot_draft_using_catalog_addresses():
    agentos = StubAgentOs()
    executor, registry = _executor(agentos)
    output = await executor.execute(
        "create_spot_swap_draft",
        "u1",
        {
            "sell_asset": "BNB",
            "buy_asset": "USDT",
            "amount": "0.01",
            "max_slippage": "0.005",
        },
        list(registry.names()),
        user_bearer_token="transient-user-token",
        idempotency_key="intent-bnb-spot-1",
    )
    assert output["core_draft_id"] == "draft_spot_bnb_usdt"
    assert output["market_type"] == "SPOT"
    assert output["instrument_id"] == "spot_bsc_bnb_usdt"
    assert output["execution_requested"] is False
    assert agentos.spot_writes == 1
    assert agentos.last_spot_body["instrument_id"] == "spot_bsc_bnb_usdt"


class _NoCallLLM:
    async def complete(self, system, messages, tools):
        raise AssertionError("ambiguous intent must stop before LLM or tools")


async def test_buy_eth_without_market_type_requests_clarification():
    executor, registry = _executor(StubAgentOs())
    runner = AgentRunner(
        AgentProfile("strategy", "test", "", registry.names()),
        executor,
        _NoCallLLM(),
        InMemoryAuditStore(),
    )
    now = datetime(2026, 8, 18, tzinfo=UTC)
    result = await runner.run_turn(
        Session(
            "sess_1", "u1", "text", Scope(("strategy",), registry.names()), "created", now, now
        ),
        "买 ETH",
        "trace_1",
        now,
        user_bearer_token="transient-user-token",
    )
    assert "现货还是永续" in result.text
    assert result.executed_tools == ()


@pytest.mark.parametrize(
    "tool_name,agentos,args,message",
    [
        (
            "create_spot_swap_draft",
            StubAgentOs(catalog_stale=True),
            {
                "sell_asset": "BNB",
                "buy_asset": "USDT",
                "amount": "10.0",
                "max_slippage": "0.005",
            },
            "已过期",
        ),
        (
            "create_perp_trade_draft",
            StubAgentOs(instruments={"perp_hl_eth": _perp("ETH") | {"stale": True}}),
            {
                "symbol": "ETH",
                "side": "buy",
                "order_type": "limit",
                "notional_usdc": "9",
                "price": "3000",
                "leverage": "1",
            },
            "已过期",
        ),
    ],
)
async def test_authoritative_scope_or_stale_market_blocks_write(tool_name, agentos, args, message):
    executor, registry = _executor(agentos)
    with pytest.raises((TradingReadinessError, ValueError, MarketInstrumentResolutionError), match=message):
        await executor.execute(
            tool_name,
            "u1",
            args,
            list(registry.names()),
            user_bearer_token="transient-user-token",
            idempotency_key="blocked-intent",
        )
    assert agentos.perp_writes == agentos.spot_writes == 0


async def test_concurrent_duplicate_intent_creates_exactly_one_draft_and_recovers_same_id():
    agentos = StubAgentOs(write_delay=0.02)
    executor, registry = _executor(agentos)
    kwargs = {
        "tool_name": "create_perp_trade_draft",
        "user_id": "u1",
        "args": {
            "symbol": "ETH",
            "side": "buy",
            "order_type": "limit",
            "notional_usdc": "9",
            "price": "3000",
            "leverage": "1",
        },
        "scope_tools": list(registry.names()),
        "user_bearer_token": "transient-user-token",
        "idempotency_key": "same-user-intent",
    }
    first, second = await asyncio.gather(executor.execute(**kwargs), executor.execute(**kwargs))
    third = await executor.execute(**kwargs)
    assert first["core_draft_id"] == second["core_draft_id"] == third["core_draft_id"]
    assert agentos.perp_writes == 1


async def test_forged_venue_or_token_fields_are_rejected_before_core_write():
    agentos = StubAgentOs()
    executor, registry = _executor(agentos)
    with pytest.raises(ValueError, match="未知字段"):
        await executor.execute(
            "create_spot_swap_draft",
            "u1",
            {
                "symbol": "BNB",
                "amount": "0.01",
                "max_slippage": "0.005",
                "token_in_address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "venue": "attacker-venue",
            },
            list(registry.names()),
            user_bearer_token="transient-user-token",
            idempotency_key="forged-input",
        )
    assert agentos.spot_writes == 0


async def test_strategy_and_readiness_account_mismatch_blocks_before_write():
    agentos = StubAgentOs()
    agentos.binding = {**agentos.binding, "venue_account_id": "vac_other"}
    with pytest.raises(StrategyBindingError, match="Venue 不一致"):
        await resolve_strategy_execution_context(agentos, "strategy_multi")


async def test_spot_and_perp_use_isolated_idempotency_spaces():
    agentos = StubAgentOs()
    executor, registry = _executor(agentos)
    common = {
        "user_id": "u1",
        "scope_tools": list(registry.names()),
        "user_bearer_token": "transient-user-token",
        "idempotency_key": "same-mobile-intent-key",
    }
    perp = await executor.execute(
        "create_perp_trade_draft",
        args={
            "symbol": "ETH",
            "side": "buy",
            "order_type": "limit",
            "notional_usdc": "9",
            "price": "3000",
            "leverage": "1",
        },
        **common,
    )
    spot = await executor.execute(
        "create_spot_swap_draft",
        args={
            "sell_asset": "BNB",
            "buy_asset": "USDT",
            "amount": "0.01",
            "max_slippage": "0.005",
        },
        **common,
    )
    assert perp["core_draft_id"] != spot["core_draft_id"]
    assert agentos.perp_writes == agentos.spot_writes == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: None,
        lambda value: {**value, "market_type": "OPTION"},
        lambda value: {key: item for key, item in value.items() if key != "venue"},
        lambda value: {**value, "supported_order_types": []},
        lambda value: {**value, "price_decimals": True},
        lambda value: {**value, "price_decimals": "not-an-int"},
        lambda value: {**value, "price_decimals": -1},
        lambda value: {**value, "trade_supported": False},
        lambda value: {**value, "venue": "OTHER"},
    ],
)
def test_market_instrument_rejects_malformed_or_untradable_perp(mutate):
    value = mutate(_perp("ETH"))
    with pytest.raises(ValueError):
        MarketInstrument.from_dict(value)


def test_market_instrument_rejects_incomplete_spot_contract():
    value = _spot()
    value["chain_id"] = 56
    with pytest.raises(ValueError, match="SPOT instrument"):
        MarketInstrument.from_dict(value)


def test_market_instrument_rejects_stale_catalog_entry():
    with pytest.raises(ValueError, match="已过期"):
        MarketInstrument.from_dict(_perp("ETH", stale=True))


@pytest.mark.parametrize(
    "changes",
    [
        {"instrument_id": ""},
        {"unified_account_id": ""},
        {"chain_id": 56},
        {"sell_token": "bad"},
        {"buy_token": "bad"},
        {"buy_token": "0x1111111111111111111111111111111111111111"},
        {"sell_amount_minor": "0"},
        {"max_slippage_bps": 0},
        {"max_slippage_bps": 1001},
    ],
)
def test_spot_request_rejects_unsafe_fields(changes):
    values = {
        "instrument_id": "spot_bsc_bnb_usdt",
        "unified_account_id": "uac_1",
        "chain_id": 97,
        "sell_token": "0x1111111111111111111111111111111111111111",
        "buy_token": "0x2222222222222222222222222222222222222222",
        "sell_amount_minor": "1",
        "max_slippage_bps": 50,
    }
    values.update(changes)
    with pytest.raises(ValueError):
        CreateSpotSwapDraftRequest(**values)


async def test_agent_trade_intent_missing_id_or_status_throws():
    agentos = StubAgentOs()
    executor, registry = _executor(agentos)

    agentos.create_agent_trade_intent = AsyncMock(return_value={"data": {"status": "PENDING_USER"}})
    with pytest.raises(ValueError, match="缺少 id"):
        await executor.execute(
            "create_perp_trade_draft",
            "u1",
            {
                "symbol": "ETH",
                "side": "buy",
                "order_type": "limit",
                "notional_usdc": "9",
                "price": "3000",
                "leverage": "1",
            },
            list(registry.names()),
            user_bearer_token="transient-user-token",
            idempotency_key="intent-missing-id",
        )

    agentos.create_agent_trade_intent = AsyncMock(return_value={"data": {"id": "123"}})
    with pytest.raises(ValueError, match="缺少 status"):
        await executor.execute(
            "create_perp_trade_draft",
            "u1",
            {
                "symbol": "ETH",
                "side": "buy",
                "order_type": "limit",
                "notional_usdc": "9",
                "price": "3000",
                "leverage": "1",
            },
            list(registry.names()),
            user_bearer_token="transient-user-token",
            idempotency_key="intent-missing-status",
        )


async def test_forged_instrument_id_is_ignored_and_resolves_by_symbol():
    agentos = StubAgentOs()
    executor, registry = _executor(agentos)

    # We pass both symbol="BTC" and a forged instrument_id="1"
    output = await executor.execute(
        "create_perp_trade_draft",
        "u1",
        {
            "instrument_id": "1",  # Forged ID, should be ignored
            "symbol": "BTC",
            "side": "buy",
            "order_type": "limit",
            "notional_usdc": "9",
            "price": "3000",
            "leverage": "1",
        },
        list(registry.names()),
        user_bearer_token="transient-user-token",
        idempotency_key="ignore-instrument-id",
    )

    # If the executor tried to query the instrument directly using ID="1",
    # it would either fail or hit get_market_instrument which doesn't know "1"
    # But because it ignores instrument_id and uses symbol="BTC" to query catalog,
    # it successfully resolves "perp_hl_btc".
    assert output["instrument_id"] == "perp_hl_btc"


async def test_creates_spot_swap_draft_with_sell_and_buy_assets():
    agentos = StubAgentOs()
    executor, registry = _executor(agentos)

    output = await executor.execute(
        "create_spot_swap_draft",
        "u1",
        {
            "sell_asset": "BNB",
            "buy_asset": "USDT",
            "amount": "0.01",
            "max_slippage": "0.005",
        },
        list(registry.names()),
        user_bearer_token="transient-user-token",
        idempotency_key="intent-spot-swap-1",
        intent_source="VOICE",
    )

    assert output["trade_draft_id"] == "draft_spot_bnb_usdt"
    assert output["core_draft_id"] == "draft_spot_bnb_usdt"
    assert output["market_type"] == "SPOT"
    assert output["instrument_id"] == "spot_bsc_bnb_usdt"
    assert output["execution_requested"] is False
    assert agentos.spot_writes == 1
    assert agentos.last_spot_body["sell_token"] == "0x1111111111111111111111111111111111111111"
    assert agentos.last_spot_body["buy_token"] == "0x2222222222222222222222222222222222222222"


async def test_creates_spot_swap_draft_reverse_usdt_to_bnb():
    agentos = StubAgentOs()
    executor, registry = _executor(agentos)

    output = await executor.execute(
        "create_spot_swap_draft",
        "u1",
        {
            "sell_asset": "USDT",
            "buy_asset": "BNB",
            "amount": "10.0",
            "max_slippage": "0.005",
        },
        list(registry.names()),
        user_bearer_token="transient-user-token",
        idempotency_key="intent-spot-swap-reverse",
        intent_source="VOICE",
    )

    assert output["trade_draft_id"] == "draft_spot_bnb_usdt"
    assert output["market_type"] == "SPOT"
    assert output["instrument_id"] == "spot_bsc_usdt_bnb"
    assert agentos.spot_writes == 1
    # Reverse direction owns a different Core instrument_id.
    assert agentos.last_spot_body["sell_token"] == "0x2222222222222222222222222222222222222222"
    assert agentos.last_spot_body["buy_token"] == "0x1111111111111111111111111111111111111111"


async def test_spot_swap_draft_rejects_mismatched_buy_asset():
    agentos = StubAgentOs()
    executor, registry = _executor(agentos)

    with pytest.raises(MarketInstrumentResolutionError, match="未找到支持 BNB -> XYZ"):
        await executor.execute(
            "create_spot_swap_draft",
            "u1",
            {
                "sell_asset": "BNB",
                "buy_asset": "XYZ",
                "amount": "0.01",
                "max_slippage": "0.005",
            },
            list(registry.names()),
            user_bearer_token="transient-user-token",
            idempotency_key="intent-spot-mismatch",
            intent_source="VOICE",
        )


async def test_spot_swap_draft_10_usdt_uses_ledger_decimals_6_producing_10000000():
    # Testnet USDT: ledger_decimals = 6, chain_decimals = 18
    # Base asset: BNB (18 decimals), Quote asset: USDT (6 ledger decimals, 18 chain decimals)
    inst = _spot() | {
        "base_asset": "BNB",
        "quote_asset": "USDT",
        "token_in_address": "0x1111111111111111111111111111111111111111",
        "token_out_address": "0x2222222222222222222222222222222222222222",
        "token_in_ledger_decimals": 18,
        "token_in_decimals": 18,
        "token_out_ledger_decimals": 6,
        "token_out_decimals": 18,
    }
    directional = _spot_usdt_bnb() | {
        "token_in_ledger_decimals": 6,
        "token_in_decimals": 18,
    }
    agentos = StubAgentOs(instruments={"spot_bsc_usdt_bnb": directional, "spot_bsc_bnb_usdt": inst})
    executor, registry = _executor(agentos)
    # User sells 10 USDT to buy BNB
    output = await executor.execute(
        "create_spot_swap_draft",
        "u1",
        {"sell_asset": "USDT", "buy_asset": "BNB", "amount": "10", "max_slippage": "0.005"},
        list(registry.names()),
        user_bearer_token="transient-user-token",
        idempotency_key="intent-usdt-10",
    )
    assert agentos.spot_writes == 1
    assert output["core_draft_id"] == "draft_spot_bnb_usdt"
    assert output["instrument_id"] == "spot_bsc_usdt_bnb"
    assert agentos.last_spot_body["sell_amount_minor"] == "10000000"
    assert agentos.last_spot_body["sell_amount_minor"] != "10000000000000000000"


async def test_spot_swap_draft_reverse_pair_uses_directional_token_in_ledger_decimals():
    inst = _spot() | {
        "base_asset": "BNB",
        "quote_asset": "USDT",
        "token_in_address": "0x1111111111111111111111111111111111111111",
        "token_out_address": "0x2222222222222222222222222222222222222222",
        "token_in_ledger_decimals": 18,
        "token_in_decimals": 18,
        "token_out_ledger_decimals": 6,
        "token_out_decimals": 18,
    }
    directional = _spot_usdt_bnb() | {
        "token_in_ledger_decimals": 6,
        "token_in_decimals": 18,
    }
    agentos = StubAgentOs(instruments={"spot_bsc_usdt_bnb": directional, "spot_bsc_bnb_usdt": inst})
    executor, registry = _executor(agentos)
    # USDT -> BNB uses its own directional instrument and tokenIn precision.
    await executor.execute(
        "create_spot_swap_draft",
        "u1",
        {"sell_asset": "USDT", "buy_asset": "BNB", "amount": "10", "max_slippage": "0.005"},
        list(registry.names()),
        user_bearer_token="transient-user-token",
        idempotency_key="intent-usdt-reverse",
    )
    assert agentos.spot_writes == 1
    assert agentos.last_spot_body["sell_amount_minor"] == "10000000"


async def test_spot_swap_draft_fails_closed_when_ledger_decimals_missing():
    inst = _spot() | {
        "token_in_ledger_decimals": None,
        "token_in_decimals": 18,
    }
    agentos = StubAgentOs(instruments={"spot_bsc_bnb_usdt": inst})
    executor, registry = _executor(agentos)
    with pytest.raises(TradingReadinessError, match="账本精度不完整"):
        await executor.execute(
            "create_spot_swap_draft",
            "u1",
            {
                "sell_asset": "BNB",
                "buy_asset": "USDT",
                "amount": "10",
                "max_slippage": "0.005",
            },
            list(registry.names()),
            user_bearer_token="transient-user-token",
            idempotency_key="intent-missing-decimals",
        )
    assert agentos.spot_writes == 0


async def test_spot_swap_draft_bypasses_hyperliquid_ticker_and_agent_account_summary():
    class TrackingAgentOs(StubAgentOs):
        def __init__(self):
            super().__init__()
            self.read_summary_calls = 0

        async def get_account_summary(self, account_id):
            self.read_summary_calls += 1
            return await super().get_account_summary(account_id)

    agentos = TrackingAgentOs()
    stale_market = CatalogMarketCore(stale=True)
    executor, registry = _executor(agentos, stale_market)

    output = await executor.execute(
        "create_spot_swap_draft",
        "u1",
        {
            "sell_asset": "BNB",
            "buy_asset": "USDT",
            "amount": "0.01",
            "max_slippage": "0.005",
        },
        list(registry.names()),
        user_bearer_token="transient-user-token",
        idempotency_key="spot-no-ticker-summary",
    )
    assert output["core_draft_id"] == "draft_spot_bnb_usdt"
    assert agentos.spot_writes == 1
    assert agentos.read_summary_calls == 0
