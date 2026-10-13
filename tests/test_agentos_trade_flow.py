"""交易流编排：意图解析 → 建请求 → 确认卡片 → 端到端，全覆盖（离线）。

LLM 用 StubLLM 固定返回文本；上游用 MockTransport。绝不让模型编造账户身份。
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import httpx
import pytest

from app.agents.llm import LLMResult
from app.tools.agentos.client import AgentOsStagingClient
from app.tools.agentos.trade_flow import (
    IntentParseError,
    NoTradableAccountError,
    StrategyBindingError,
    StrategyExecutionContext,
    TradingReadinessContext,
    TradingReadinessError,
    build_draft_request,
    catalog_asset_query,
    create_draft_from_text,
    discover_trading_account,
    normalize_symbol,
    parse_account_summary,
    parse_market_catalog,
    parse_market_instrument,
    parse_spot_trading_readiness,
    parse_trade_intent,
    parse_trading_readiness,
    render_confirmation_card,
    require_draft_ready,
    require_instrument_allowed,
    resolve_strategy_execution_context,
)

BASE = "https://api-staging.agentos-app.app"


def _readiness_payload() -> dict:
    return {
        "data": {
            "unified_account_id": "uac_1",
            "venue_account_id": "vac_1",
            "ledger_balance_by_asset": {"USDC": "7000000"},
            "venue_balance_by_asset": {"USDC": "12.5"},
            "venue_margin_minor": "5000000",
            "daily_notional_limit_minor": "4000000",
            "daily_notional_used_minor": "500000",
            "daily_notional_remaining_minor": "3500000",
            "agent_trading_account": {
                "id": "atac_1",
                "agent_id": "agent-test-001",
                "unified_account_id": "uac_1",
                "venue_account_id": "vac_1",
            },
            "mandate": {
                "id": "mandate_1",
                "status": "ACTIVE",
                "confirmation_mode": "ALWAYS_CONFIRM",
                "max_notional_per_order_minor": 1000000,
                "max_daily_notional_minor": 4000000,
                "max_total_exposure_minor": 1500000,
                "allowed_symbols": ["BTC", "ETH", "USDC"],
                "allowed_order_types": ["LIMIT", "EXACT_INPUT"],
                "max_leverage": "2",
                "max_slippage": "0.01",
            },
            "strategy_binding": {
                "strategy_id": "strategy_btc_limit_v1",
                "agent_trading_account_id": "atac_1",
                "venue_account_id": "vac_1",
                "mandate_id": "mandate_1",
                "capital_limit_minor": 2000000,
                "status": "ACTIVE",
            },
            "can_create_agent_draft": True,
            "blockers": ["IGNORED", 123],
            "stale": False,
        },
        "request_id": "req_ready",
    }


class StubLLM:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, system, messages, tools) -> LLMResult:
        return LLMResult(text=self._text)


def _parse_readiness(payload: dict) -> TradingReadinessContext:
    return parse_trading_readiness(
        payload,
        expected_agent_id="agent-test-001",
        expected_strategy_id="strategy_btc_limit_v1",
    )


def test_parse_account_summary_preserves_ledger_semantics() -> None:
    output = parse_account_summary(
        {
            "data": {
                "balance": {"asset": "USDC", "available_minor": 7_000_000},
                "positions": [],
                "orders": [],
            },
            "request_id": "req_summary",
        }
    )
    assert output["balance_semantics"] == "CORE_LEDGER_NOT_VENUE_EQUITY"
    assert output["venue_equity"]["status"] == "NOT_AVAILABLE"
    assert output["request_id"] == "req_summary"


@pytest.mark.parametrize(
    "payload,message",
    [
        ({"data": None}, "缺少 data"),
        ({"data": {"balance": {}, "positions": [], "orders": None}}, "契约不完整"),
    ],
)
def test_parse_account_summary_rejects_incomplete_contract(payload, message) -> None:
    with pytest.raises(TradingReadinessError, match=message):
        parse_account_summary(payload)


def test_parse_trading_readiness_happy_and_non_list_blockers() -> None:
    parsed = _parse_readiness(_readiness_payload())
    assert parsed.available_trade_notional_minor == 1_000_000
    assert parsed.per_order_limit_minor == 1_000_000
    assert parsed.total_exposure_limit_minor == 1_500_000
    assert parsed.daily_notional_remaining_minor == 3_500_000
    assert parsed.blockers == ("IGNORED",)
    output = parsed.tool_output()
    assert output["request_id"] == "req_ready"
    assert output["venue_balance_by_asset"] == {"USDC": "12.5"}
    assert output["ledger_balance_by_asset"] == {"USDC": "7000000"}
    assert "formatted_ledger_balance_by_asset" not in output
    assert output["venue_equity"]["status"] == "NOT_AVAILABLE"
    assert output["total_exposure_remaining"]["status"] == "NOT_AVAILABLE"

    payload = _readiness_payload()
    payload["data"]["blockers"] = "not-a-list"
    payload.pop("request_id")
    parsed = _parse_readiness(payload)
    assert parsed.blockers == ()
    assert parsed.request_id is None


@pytest.mark.parametrize(
    "mutate,message",
    [
        (lambda p: p.update(data=None), "缺少 data"),
        (lambda p: p["data"].update(agent_trading_account=None), "缺少 Agent 账户"),
        (lambda p: p["data"].update(mandate=None), "缺少 Agent 账户"),
        (lambda p: p["data"].update(strategy_binding=None), "缺少 Agent 账户"),
        (lambda p: p["data"].update(unified_account_id=""), "缺少正式账户标识"),
        (
            lambda p: p["data"]["agent_trading_account"].update(unified_account_id="uac_other"),
            "账户关联不一致",
        ),
        (
            lambda p: p["data"]["agent_trading_account"].update(venue_account_id="vac_other"),
            "账户关联不一致",
        ),
        (
            lambda p: p["data"]["agent_trading_account"].update(agent_id="agent_other"),
            "非预期 Agent",
        ),
        (
            lambda p: p["data"]["strategy_binding"].update(strategy_id="strategy_other"),
            "非预期 Strategy Binding",
        ),
        (
            lambda p: p["data"]["strategy_binding"].update(agent_trading_account_id="atac_other"),
            "Agent Trading Account 不一致",
        ),
        (
            lambda p: p["data"]["strategy_binding"].update(venue_account_id="vac_other"),
            "Venue Account 不一致",
        ),
        (lambda p: p["data"]["mandate"].update(id=""), "Mandate 不一致"),
        (
            lambda p: p["data"]["strategy_binding"].update(mandate_id="mandate_other"),
            "Mandate 不一致",
        ),
        (
            lambda p: p["data"]["mandate"].update(confirmation_mode="DRAFT_ONLY"),
            "ALWAYS_CONFIRM",
        ),
        (lambda p: p["data"]["mandate"].update(allowed_symbols=[]), "allowed_symbols"),
        (lambda p: p["data"]["mandate"].update(allowed_order_types=None), "allowed_order_types"),
        (lambda p: p["data"]["mandate"].update(allowed_symbols=[""]), "允许范围无效"),
        (lambda p: p["data"]["mandate"].update(max_leverage="bad"), "杠杆或滑点"),
        (lambda p: p["data"]["mandate"].update(max_slippage="0"), "杠杆或滑点"),
        (lambda p: p["data"].update(venue_margin_minor="bad"), "venue_margin_minor 无效"),
        (lambda p: p["data"].update(venue_margin_minor="-1"), "venue_margin_minor 无效"),
        (
            lambda p: p["data"].update(ledger_balance_by_asset=None),
            "ledger_balance_by_asset 无效",
        ),
        (
            lambda p: p["data"].update(ledger_balance_by_asset={"": "1"}),
            "ledger_balance_by_asset 无效",
        ),
        (
            lambda p: p["data"].update(venue_balance_by_asset=None),
            "venue_balance_by_asset 无效",
        ),
        (
            lambda p: p["data"].update(venue_balance_by_asset={"USDC": "bad"}),
            "venue_balance_by_asset.USDC 无效",
        ),
        (
            lambda p: p["data"].update(venue_balance_by_asset={"USDC": "NaN"}),
            "venue_balance_by_asset 无效",
        ),
        (
            lambda p: p["data"].update(daily_notional_remaining_minor="1"),
            "日剩余额度不一致",
        ),
        (
            lambda p: p["data"]["mandate"].update(max_daily_notional_minor=1),
            "日额度与 Mandate 不一致",
        ),
        (
            lambda p: p["data"]["strategy_binding"].update(capital_limit_minor=0),
            "capital_limit_minor 无效",
        ),
    ],
)
def test_parse_trading_readiness_rejects_invalid_contract(mutate, message) -> None:
    payload = copy.deepcopy(_readiness_payload())
    mutate(payload)
    with pytest.raises(TradingReadinessError, match=message):
        _parse_readiness(payload)


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"stale": True}, "已过期"),
        ({"can_create_agent_draft": False, "blockers": ("CAPITAL_REQUIRED",)}, "CAPITAL_REQUIRED"),
        ({"can_create_agent_draft": False, "blockers": ()}, "UNKNOWN"),
        ({"mandate_status": "REVOKED"}, "Mandate"),
        ({"strategy_status": "PAUSED"}, "Strategy Binding"),
    ],
)
def test_require_draft_ready_fails_closed(changes, message) -> None:
    readiness = _parse_readiness(_readiness_payload())
    with pytest.raises(TradingReadinessError, match=message):
        require_draft_ready(replace(readiness, **changes))


# ---- parse_trade_intent ----


async def test_parse_plain_json():
    llm = StubLLM('{"symbol":"ETH-USDC","side":"BUY","quantity":"1.5","limit_price":"3000"}')
    intent = await parse_trade_intent(llm, "x")
    assert intent.symbol == "ETH-USDC"
    assert intent.side == "BUY"
    assert intent.leverage is None and intent.max_slippage is None
    assert intent.reduce_only is False


async def test_parse_fenced_json_with_prose():
    llm = StubLLM(
        '好的：\n```json\n{"symbol":"ETH-USDC","side":"sell",'
        '"quantity":"1","limit_price":"3000","leverage":"2",'
        '"max_slippage":"0.01","reduce_only":true}\n```'
    )
    intent = await parse_trade_intent(llm, "x")
    assert intent.side == "SELL"  # 小写归一为大写
    assert intent.leverage == "2"
    assert intent.max_slippage == "0.01"
    assert intent.reduce_only is True


@pytest.mark.parametrize(
    "payload",
    [
        '{"side":"BUY","quantity":"1","limit_price":"3000"}',  # 缺 symbol
        '{"symbol":"E","quantity":"1","limit_price":"3000"}',  # 缺 side
        '{"symbol":"E","side":"BUY","limit_price":"3000"}',  # 缺 quantity
        '{"symbol":"E","side":"BUY","quantity":"1"}',  # 缺 limit_price
    ],
)
async def test_parse_missing_required_raises(payload):
    with pytest.raises(IntentParseError, match="缺少必填要素"):
        await parse_trade_intent(StubLLM(payload), "x")


async def test_parse_no_json_raises():
    with pytest.raises(IntentParseError, match="未从模型输出中找到 JSON"):
        await parse_trade_intent(StubLLM("完全没有大括号的一句话"), "x")


async def test_parse_malformed_json_raises():
    with pytest.raises(IntentParseError, match="不是合法 JSON"):
        await parse_trade_intent(StubLLM('{"symbol": "E", "side": }'), "x")


# ---- build_draft_request ----


def _intent(**kw):
    from app.tools.agentos.trade_flow import TradeIntent

    base = {"symbol": "ETH-USDC", "side": "BUY", "quantity": "1.5", "limit_price": "3000"}
    base.update(kw)
    return TradeIntent(**base)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("BTC", "BTC"),  # 已是裸币名
        ("btc", "BTC"),  # 小写归一
        ("BTCUSDT", "BTC"),  # 连写剥离计价币
        ("BTCUSDC", "BTC"),  # 另一计价币
        ("BTCUSD", "BTC"),  # Gemini Live 外汇写法；线上 INVALID_SYMBOL
        ("ETHUSD", "ETH"),
        ("BUSD", "BUSD"),  # 稳定币本身，不能剥成 B
        ("BTC/USDT", "BTC"),  # 斜杠分隔 → 取 base
        ("BTC-USDT", "BTC"),  # 连字符 → 取 base（与 Hyperliquid 统一）
        ("ETH-USDC", "ETH"),  # 取 base
        ("  eth-usdt  ", "ETH"),  # 去空白 + 大写 + 取 base
        ("USDT", "USDT"),  # 边界：整串即计价币 → 原样（不剥成空）
    ],
)
def test_normalize_symbol(raw, expected):
    assert normalize_symbol(raw) == expected


def test_build_draft_request_normalizes_symbol():
    # LLM 给带计价币的 BTC-USDT，建请求应输出交易所裸币名 BTC（与 Core/Hyperliquid 统一）
    req = build_draft_request(
        _intent(symbol="BTC-USDT"), unified_account_id="u", venue_account_id="v"
    )
    assert req.to_body()["symbol"] == "BTC"


def test_build_applies_defaults():
    req = build_draft_request(_intent(), unified_account_id="uacc_1", venue_account_id="vacc_1")
    body = req.to_body()
    assert body["leverage"] == "1"  # 默认杠杆
    assert body["max_slippage"] == "0.005"  # 默认滑点
    assert body["order_type"] == "LIMIT"
    assert body["unified_account_id"] == "uacc_1"  # 身份来自上下文注入


def test_build_uses_intent_values_over_defaults():
    req = build_draft_request(
        _intent(leverage="3", max_slippage="0.02"),
        unified_account_id="u",
        venue_account_id="v",
        default_leverage="1",
        default_max_slippage="0.005",
    )
    body = req.to_body()
    assert body["leverage"] == "3" and body["max_slippage"] == "0.02"


def test_build_includes_core_strategy_context():
    req = build_draft_request(
        _intent(symbol="BTC"),
        unified_account_id="u",
        venue_account_id="v",
        strategy_id="strategy_1",
        mandate_id="mandate_1",
    )
    body = req.to_body()
    assert body["strategy_id"] == "strategy_1"
    assert body["mandate_id"] == "mandate_1"


# ---- render_confirmation_card ----


def test_render_full_card():
    card = render_confirmation_card(
        {
            "data": {
                "id": "draft_77",
                "version": 1,
                "symbol": "ETH-USDC",
                "side": "BUY",
                "quantity": "1.5",
                "limit_price": "3000.00",
                "leverage": "2",
                "max_slippage": "0.005",
                "status": "PENDING_CONFIRMATION",
                "expires_at": "2026-08-04T09:00:00Z",
            }
        }
    )
    assert "买入（限价单）" in card
    assert "draft_77" in card and "v1" in card
    assert "step-up" in card  # 提示需用户二次验证


def test_render_empty_data_falls_back():
    card = render_confirmation_card({})
    assert "—" in card  # 缺字段兜底
    # side 为空字符串时映射也为空，不应抛错
    assert "方向：（限价单）" in card


def test_render_unknown_side_uses_raw():
    card = render_confirmation_card({"data": {"side": "HOLD"}})
    assert "HOLD（限价单）" in card


# ---- create_draft_from_text 端到端 ----


class _StubAccountsClient:
    """只实现 get_agent_trading_accounts 的桩，用于 discover_trading_account 单测。"""

    def __init__(self, data) -> None:
        self._data = data

    async def get_agent_trading_accounts(self):
        return {"data": self._data}


class _StubStrategyClient(_StubAccountsClient):
    def __init__(self, binding, accounts) -> None:
        super().__init__(accounts)
        self._binding = binding

    async def get_strategy_binding(self, strategy_id):
        return {"data": self._binding}


def _binding(**overrides):
    value = {
        "strategy_id": "strategy_1",
        "agent_id": "agent-test-001",
        "agent_trading_account_id": "atac_1",
        "venue_account_id": "vac_1",
        "mandate_id": "mandate_1",
        "authorization_grant_id": "grant_1",
        "capital_limit_minor": 1_000_000,
        "status": "ACTIVE",
        "market_types": ["PERP", "SPOT"],
        "allowed_assets": ["BTC", "ETH", "USDC", "USDT", "BNB"],
        "allowed_instruments": [],
        "max_order_notional_minor": 1_000_000,
        "max_daily_notional_minor": 4_000_000,
        "max_leverage": "2",
        "max_slippage": "0.01",
    }
    value.update(overrides)
    return value


def _bound_account(**overrides):
    value = {
        "id": "atac_1",
        "agent_id": "agent-test-001",
        "unified_account_id": "uac_1",
        "venue_account_id": "vac_1",
        "status": "ACTIVE",
    }
    value.update(overrides)
    return value


async def test_discover_picks_first_active_with_venue():
    client = _StubAccountsClient(
        [
            {
                "status": "PENDING",
                "unified_account_id": "u0",
                "venue_account_id": "v0",
            },  # 非 ACTIVE
            {"status": "ACTIVE", "unified_account_id": "u1"},  # ACTIVE 但无 venue
            {"status": "ACTIVE", "unified_account_id": "u2", "venue_account_id": "v2"},  # 命中
        ]
    )
    assert await discover_trading_account(client) == ("u2", "v2")


async def test_discover_no_tradable_account_raises():
    client = _StubAccountsClient([])  # 空 → 无可交易账户
    with pytest.raises(NoTradableAccountError):
        await discover_trading_account(client)


async def test_resolve_strategy_context_matches_exact_bound_account():
    client = _StubStrategyClient(
        _binding(),
        [
            _bound_account(
                id="atac_other", unified_account_id="uac_other", venue_account_id="vac_other"
            ),
            _bound_account(),
        ],
    )
    context = await resolve_strategy_execution_context(client, " strategy_1 ")
    assert context.strategy_id == "strategy_1"
    assert context.agent_trading_account_id == "atac_1"
    assert context.unified_account_id == "uac_1"
    assert context.venue_account_id == "vac_1"
    assert context.mandate_id == "mandate_1"
    assert context.authorization_grant_id == "grant_1"
    assert context.capital_limit_minor == 1_000_000


@pytest.mark.parametrize(
    "strategy_id,binding,accounts,message",
    [
        ("   ", _binding(), [_bound_account()], "strategy_id 不能为空"),
        (
            "strategy_1",
            _binding(strategy_id="strategy_other"),
            [_bound_account()],
            "strategy_id 不一致",
        ),
        ("strategy_1", _binding(status="PAUSED"), [_bound_account()], "当前不可新建订单"),
        ("strategy_1", _binding(mandate_id=""), [_bound_account()], "缺少字段"),
        (
            "strategy_1",
            _binding(capital_limit_minor="bad"),
            [_bound_account()],
            "capital_limit_minor 无效",
        ),
        ("strategy_1", _binding(capital_limit_minor=0), [_bound_account()], "必须大于 0"),
        ("strategy_1", _binding(market_types=[]), [_bound_account()], "market_types"),
        ("strategy_1", _binding(allowed_assets=[]), [_bound_account()], "allowed_assets"),
        ("strategy_1", _binding(market_types=["OPTION"]), [_bound_account()], "交易范围"),
        (
            "strategy_1",
            _binding(allowed_instruments="not-a-list"),
            [_bound_account()],
            "allowed_instruments",
        ),
        (
            "strategy_1",
            _binding(max_order_notional_minor="bad"),
            [_bound_account()],
            "max_order_notional_minor",
        ),
        (
            "strategy_1",
            _binding(max_daily_notional_minor=0),
            [_bound_account()],
            "max_daily_notional_minor",
        ),
        ("strategy_1", _binding(max_leverage="bad"), [_bound_account()], "杠杆或滑点"),
        ("strategy_1", _binding(max_slippage="0"), [_bound_account()], "杠杆或滑点"),
        ("strategy_1", _binding(), [], "不可见"),
        ("strategy_1", _binding(), [_bound_account(status="PAUSED")], "账户不可交易"),
        (
            "strategy_1",
            _binding(),
            [_bound_account(agent_id="agent_other")],
            "Agent 不一致",
        ),
        (
            "strategy_1",
            _binding(),
            [_bound_account(unified_account_id="")],
            "缺少 unified_account_id",
        ),
        ("strategy_1", _binding(), [_bound_account(venue_account_id="vac_other")], "Venue 不一致"),
    ],
)
async def test_resolve_strategy_context_rejects_invalid_relationships(
    strategy_id, binding, accounts, message
):
    with pytest.raises(StrategyBindingError, match=message):
        await resolve_strategy_execution_context(
            _StubStrategyClient(binding, accounts), strategy_id
        )


async def test_resolve_strategy_context_rejects_unexpected_agent() -> None:
    with pytest.raises(StrategyBindingError, match="非预期 Agent"):
        await resolve_strategy_execution_context(
            _StubStrategyClient(_binding(), [_bound_account()]),
            "strategy_1",
            expected_agent_id="agent_other",
        )


async def test_resolve_strategy_context_rejects_missing_market_types_and_allowed_assets() -> None:
    binding = _binding()
    binding.pop("market_types")
    with pytest.raises(StrategyBindingError, match="market_types"):
        await resolve_strategy_execution_context(
            _StubStrategyClient(binding, [_bound_account()]), "strategy_1"
        )


def _catalog_instrument(*, market_type: str = "PERP"):
    value = {
        "instrument_id": "ins_eth",
        "market_type": market_type,
        "venue": "HYPERLIQUID",
        "environment": "TESTNET",
        "display_symbol": "ETH/USDC",
        "venue_symbol": "ETH",
        "base_asset": "ETH",
        "quote_asset": "USDC",
        "trade_supported": True,
        "supported_order_types": ["LIMIT"],
        "price_decimals": 2,
        "size_decimals": 5,
        "minimum_quantity": "0.00001",
        "minimum_notional": "1",
        "maximum_leverage": "2",
        "status": "ACTIVE",
        "stale": False,
        "as_of": "2026-08-18T00:00:00Z",
    }
    return value


def test_catalog_parsers_and_scope_intersection_fail_closed():
    with pytest.raises(TradingReadinessError, match="响应不完整"):
        parse_market_catalog({"data": []})
    with pytest.raises(TradingReadinessError, match="已过期"):
        parse_market_catalog({"data": [], "meta": {"stale": True}})

    instrument = parse_market_instrument({"data": _catalog_instrument()})
    readiness = _parse_readiness(_readiness_payload())
    strategy = StrategyExecutionContext(
        strategy_id="strategy_1",
        agent_trading_account_id="atac_1",
        unified_account_id="uac_1",
        venue_account_id="vac_1",
        mandate_id="mandate_1",
        authorization_grant_id="grant_1",
        capital_limit_minor=1_000_000,
        market_types=("PERP",),
        allowed_assets=("ETH", "USDC"),
        allowed_instruments=(),
        max_order_notional_minor=1_000_000,
        max_daily_notional_minor=4_000_000,
        max_leverage="2",
        max_slippage="0.01",
    )
    require_instrument_allowed(instrument, readiness, strategy)
    require_instrument_allowed(instrument, replace(readiness, allowed_symbols=("*",)), strategy)
    require_instrument_allowed(instrument, readiness, replace(strategy, allowed_assets=("*",)))
    require_instrument_allowed(
        instrument, replace(readiness, allowed_symbols=()), replace(strategy, allowed_assets=())
    )

    with pytest.raises(TradingReadinessError, match="交易类型"):
        require_instrument_allowed(instrument, readiness, replace(strategy, market_types=("SPOT",)))
    with pytest.raises(TradingReadinessError, match="交易标的"):
        require_instrument_allowed(
            instrument, readiness, replace(strategy, allowed_instruments=("ins_btc",))
        )
    with pytest.raises(TradingReadinessError, match="Mandate 不允许"):
        require_instrument_allowed(
            instrument, replace(readiness, allowed_symbols=("BTC", "USDC")), strategy
        )
    with pytest.raises(TradingReadinessError, match="策略不允许"):
        require_instrument_allowed(
            instrument, readiness, replace(strategy, allowed_assets=("BTC", "USDC"))
        )


async def test_create_draft_from_text_end_to_end():
    captured: list[httpx.Request] = []

    def handler(request):
        captured.append(request)
        return httpx.Response(
            201,
            json={
                "data": {
                    "id": "draft_e2e",
                    "version": 1,
                    "symbol": "ETH-USDC",
                    "side": "BUY",
                    "quantity": "1.5",
                    "limit_price": "3000.00",
                    "status": "PENDING_CONFIRMATION",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AgentOsStagingClient(BASE, token="T", client=http)
        llm = StubLLM('{"symbol":"ETH-USDC","side":"BUY","quantity":"1.5","limit_price":"3000.00"}')
        draft, card = await create_draft_from_text(
            client,
            llm,
            "买 1.5 ETH 限价 3000",
            unified_account_id="uacc_1",
            venue_account_id="vacc_1",
            strategy_id="strategy_1",
            mandate_id="mandate_1",
            idempotency_key="idem_flow",
        )
    assert draft["data"]["id"] == "draft_e2e"
    assert "买入" in card and "draft_e2e" in card
    sent = captured[-1]
    assert sent.url.path == "/internal/v1/trade-drafts"
    assert sent.headers.get("Idempotency-Key") == "idem_flow"  # 透传幂等键
    body = json.loads(sent.content)
    assert body["unified_account_id"] == "uacc_1"  # 账户身份注入，非模型产出
    assert body["strategy_id"] == "strategy_1"
    assert body["mandate_id"] == "mandate_1"


# --------------------------------------------------------------------------
# Ticker 别名清洗：LLM 会吐出带 PERP 后缀的形式（线上实测 query=ETHPERP）
# --------------------------------------------------------------------------


def test_catalog_asset_query_strips_perp_suffixes() -> None:
    """线上实测：LLM 发 ETHPERP 去查 Catalog，而 venue_symbol 其实是 ETH。"""

    for raw in ("ETHPERP", "ETH-PERP", "ETH/PERP", "eth perp", "ETHPERPETUAL"):
        assert catalog_asset_query(raw) == "ETH", raw
    assert catalog_asset_query("BTCPERP") == "BTC"
    # 计价币与合约后缀叠加。
    assert catalog_asset_query("BTCUSDTPERP") == "BTC"
    # Gemini Live 把 BTCUSD 当外汇代码发出；行情 venue_symbol 是 BTC。
    assert catalog_asset_query("BTCUSD") == "BTC"
    assert catalog_asset_query("ETHUSD") == "ETH"
    assert catalog_asset_query("BUSD") == "BUSD"


def test_catalog_asset_query_keeps_assets_that_merely_look_like_a_suffix() -> None:
    """不能把资产本身吃掉：裸 PERP、以及以这些字母结尾的正常资产。"""

    assert catalog_asset_query("PERP") == "PERP"
    assert catalog_asset_query("SWAP") == "SWAP"
    # HYPE 不含后缀，必须原样。
    assert catalog_asset_query("HYPE") == "HYPE"


def test_normalize_symbol_strips_perp_suffixes() -> None:
    assert normalize_symbol("ETHPERP") == "ETH"
    assert normalize_symbol("ETH-PERP") == "ETH"
    assert normalize_symbol("PERP") == "PERP"


def test_parse_spot_trading_readiness_validates_data_and_account_id() -> None:
    assert parse_spot_trading_readiness({"data": {"unified_account_id": " uac_1 "}}).unified_account_id == "uac_1"

    with pytest.raises(TradingReadinessError, match="缺少 data"):
        parse_spot_trading_readiness({"data": None})

    with pytest.raises(TradingReadinessError, match="缺少 unified_account_id"):
        parse_spot_trading_readiness({"data": {"unified_account_id": "  "}})
