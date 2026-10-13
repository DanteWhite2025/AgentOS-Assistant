from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.policies.engine import PolicyEngine
from app.policies.mandate import default_mandate
from app.tools.agentos.client import AgentOsStagingClient
from app.tools.builtin import build_default_registry
from app.tools.core_client import FakeCoreClient
from app.tools.executor import ToolExecutor
from tests.core_fixtures import trading_readiness_payload

BASE = "https://core.example"
USER_TOKEN = "user-token-test-123"


def _make_executor(
    handler: Any,
    expected_agent_id: str = "agent-test-001",
) -> tuple[ToolExecutor, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def wrapped_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped_handler))
    agentos = AgentOsStagingClient(BASE, "agent-secret-token", client)
    registry = build_default_registry()
    executor = ToolExecutor(
        registry,
        FakeCoreClient(),
        PolicyEngine(default_mandate()),
        agentos_client=agentos,
        expected_agent_id=expected_agent_id,
    )
    return executor, captured


@pytest.mark.asyncio
async def test_get_risk_summary_returns_notional_and_margin() -> None:
    """get_risk_summary 应当返回账户的可用交易额度、杠杆上限、保证金等风控信息。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me/trading-readiness":
            assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
            return httpx.Response(200, json=trading_readiness_payload())
        return httpx.Response(404)

    executor, _ = _make_executor(handler)
    scope = list(build_default_registry().names())
    out = await executor.execute(
        "get_risk_summary",
        "u1",
        {},
        scope,
        user_bearer_token=USER_TOKEN,
    )

    assert out["source"] == "CORE_RISK_SUMMARY"
    assert out["status"] == "ACTIVE"
    assert out["daily_notional_limit_minor"] == "50000000"
    assert out["daily_notional_remaining_minor"] == "49500000"
    assert out["available_trade_notional_minor"] == "10000000"
    assert out["max_leverage"] == "2"
    assert out["venue_margin_minor"] == "20000000"


@pytest.mark.asyncio
async def test_get_risk_summary_graceful_when_readiness_unavailable() -> None:
    """当 Core trading-readiness 失败时，get_risk_summary 降级返回安全说明，不抛异常。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "Core error"})

    executor, _ = _make_executor(handler)
    scope = list(build_default_registry().names())
    out = await executor.execute(
        "get_risk_summary",
        "u1",
        {},
        scope,
        user_bearer_token=USER_TOKEN,
    )

    assert out["source"] == "CORE_RISK_SUMMARY"
    assert out["status"] == "UNAVAILABLE"
    assert out["available_trade_notional_minor"] == "0"
    assert "未配置风控限额" in out["message"]


@pytest.mark.asyncio
async def test_readonly_tools_graceful_when_no_unified_account() -> None:
    """用户未开立 unified_account 时，只读工具不抛异常阻断会话，优雅返回空数据。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me/trading-readiness":
            # 模拟用户未开通，data 中没有 unified_account_id
            return httpx.Response(200, json={"data": {"capital_status": "NONE"}})
        return httpx.Response(404)

    executor, _ = _make_executor(handler)
    scope = list(build_default_registry().names())

    balances = await executor.execute(
        "get_balances", "u1", {}, scope, user_bearer_token=USER_TOKEN
    )
    assert balances["source"] == "CORE_BALANCES"
    assert balances["data"] == []

    positions = await executor.execute(
        "get_positions", "u1", {}, scope, user_bearer_token=USER_TOKEN
    )
    assert positions["source"] == "CORE_POSITIONS"
    assert positions["data"] == []

    orders = await executor.execute(
        "get_orders", "u1", {}, scope, user_bearer_token=USER_TOKEN
    )
    assert orders["source"] == "CORE_ORDERS"
    assert orders["data"] == []

    summary = await executor.execute(
        "get_account_summary", "u1", {}, scope, user_bearer_token=USER_TOKEN
    )
    assert summary["source"] == "CORE_ACCOUNT_SUMMARY"
    assert summary["account_summary"]["positions"] == []


@pytest.mark.asyncio
async def test_get_positions_and_orders_extracts_from_user_account_summary() -> None:
    """get_positions 和 get_orders 优先从用户的 account-summary 读取，并支持 symbol 过滤。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me/trading-readiness":
            return httpx.Response(200, json={"data": {"unified_account_id": "uac_42"}})
        if request.url.path == "/api/v1/me/account-summary":
            assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
            return httpx.Response(
                200,
                json={
                    "data": {
                        "balance": {"asset": "USDC", "available_minor": 5000000},
                        "positions": [
                            {"symbol": "BTC", "size": "0.5", "entry_price": "60000"},
                            {"symbol": "ETH", "size": "2.0", "entry_price": "3000"},
                        ],
                        "orders": [
                            {"symbol": "BTC", "side": "BUY", "quantity": "0.1", "price": "59000"},
                            {"symbol": "SOL", "side": "SELL", "quantity": "10", "price": "150"},
                        ],
                    },
                    "request_id": "req_user_summary",
                },
            )
        return httpx.Response(404)

    executor, _ = _make_executor(handler)
    scope = list(build_default_registry().names())

    # 1. 查询全部持仓
    all_pos = await executor.execute(
        "get_positions", "u1", {}, scope, user_bearer_token=USER_TOKEN
    )
    assert all_pos["source"] == "CORE_USER_POSITIONS"
    assert len(all_pos["data"]) == 2

    # 2. 标的筛选 BTC 持仓（支持 BTC-PERP 归一化）
    btc_pos = await executor.execute(
        "get_positions", "u1", {"symbol": "BTC-PERP"}, scope, user_bearer_token=USER_TOKEN
    )
    assert btc_pos["source"] == "CORE_USER_POSITIONS"
    assert len(btc_pos["data"]) == 1
    assert btc_pos["data"][0]["symbol"] == "BTC"

    # 3. 标的筛选订单
    btc_orders = await executor.execute(
        "get_orders", "u1", {"symbol": "BTC"}, scope, user_bearer_token=USER_TOKEN
    )
    assert btc_orders["source"] == "CORE_USER_ORDERS"
    assert len(btc_orders["data"]) == 1
    assert btc_orders["data"][0]["symbol"] == "BTC"


@pytest.mark.asyncio
async def test_get_balances_with_asset_filtering() -> None:
    """get_balances 支持按资产代币符号筛选。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me/trading-readiness":
            return httpx.Response(200, json={"data": {"unified_account_id": "uac_99"}})
        if request.url.path == "/api/v1/trading-accounts/uac_99/balances":
            assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"asset": "USDC", "balance": "1000.5"},
                        {"asset": "ETH", "balance": "0.8"},
                    ]
                },
            )
        return httpx.Response(404)

    executor, _ = _make_executor(handler)
    scope = list(build_default_registry().names())

    usdc_bal = await executor.execute(
        "get_balances", "u1", {"asset": "USDC"}, scope, user_bearer_token=USER_TOKEN
    )
    assert len(usdc_bal["data"]) == 1
    assert usdc_bal["data"][0]["asset"] == "USDC"


def _live_asset_snapshot_payload(*, unbound: bool = False) -> dict[str, Any]:
    if unbound:
        return {
            "data": {
                "source": "LIVE_CHAIN_AND_VENUE",
                "semantics": "ASSET_MANAGER",
                "trading_address": "",
                "collection_address": "",
                "spot": {"available": False, "usd_cents": None, "holdings": []},
                "contract": {
                    "available": False,
                    "account_value": "0",
                    "withdrawable": "0",
                    "usd_cents": None,
                    "withdrawable_usd_cents": None,
                },
                "total_usd_cents": None,
                "positions": [],
                "attention": {"required": False, "codes": []},
                "as_of": "2026-09-15T17:00:00Z",
                "stale": False,
            },
            "request_id": "trace-unbound-123",
        }
    return {
        "data": {
            "source": "LIVE_CHAIN_AND_VENUE",
            "semantics": "ASSET_MANAGER",
            "trading_address": "0x1111111111111111111111111111111111111111",
            "collection_address": "0x2222222222222222222222222222222222222222",
            "spot": {
                "available": True,
                "usd_cents": "200",
                "holdings": [
                    {
                        "asset": "USDT",
                        "token_address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "decimals": 18,
                        "trading_raw": "2000000000000000000",
                        "collection_raw": "0",
                        "total_raw": "2000000000000000000",
                        "usd_cents": "200",
                    }
                ],
            },
            "contract": {
                "available": True,
                "account_value": "2.79",
                "withdrawable": "2.79",
                "usd_cents": "279",
                "withdrawable_usd_cents": "279",
            },
            "total_usd_cents": "479",
            "positions": [
                {
                    "symbol": "BTC",
                    "side": "SHORT",
                    "quantity": "-0.01",
                    "entry_price": "64000",
                    "unrealized_pnl": "-1.25",
                    "as_of": "2026-09-15T17:00:00Z",
                }
            ],
            "attention": {"required": False, "codes": []},
            "as_of": "2026-09-15T17:00:00Z",
            "stale": False,
        },
        "request_id": "trace-live-snap-123",
    }


@pytest.mark.asyncio
async def test_get_risk_summary_with_live_asset_snapshot() -> None:
    """get_risk_summary 优先从 live asset-snapshot 读取可用额度与保证金。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me/asset-snapshot":
            assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
            return httpx.Response(200, json=_live_asset_snapshot_payload())
        return httpx.Response(500)

    executor, captured = _make_executor(handler)
    scope = list(build_default_registry().names())
    out = await executor.execute(
        "get_risk_summary", "u1", {}, scope, user_bearer_token=USER_TOKEN
    )

    assert len(captured) == 1
    assert captured[0].url.path == "/api/v1/me/asset-snapshot"
    assert out["source"] == "LIVE_ASSET_SNAPSHOT"
    assert out["status"] == "ACTIVE"
    assert out["available_trade_notional"] == "2.79"
    assert out["withdrawable_usd"] == "2.79"
    assert out["withdrawable_usd_cents"] == "279"
    assert out["available_trade_notional_minor"] == "279"
    assert out["venue_margin_minor"] == "279"
    assert out["contract_account_value"] == "2.79"
    assert out["contract_usd_cents"] == "279"
    assert out["contract_available"] is True
    assert out["spot_available"] is True
    assert out["spot_usd_cents"] == "200"
    assert out["total_usd_cents"] == "479"
    assert out["max_leverage"] == "50"


@pytest.mark.asyncio
async def test_get_positions_with_live_asset_snapshot() -> None:
    """get_positions 优先从 live asset-snapshot 读取真实持仓并支持 symbol 过滤。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me/asset-snapshot":
            assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
            return httpx.Response(200, json=_live_asset_snapshot_payload())
        return httpx.Response(500)

    executor, _ = _make_executor(handler)
    scope = list(build_default_registry().names())

    # 全部持仓
    all_pos = await executor.execute(
        "get_positions", "u1", {}, scope, user_bearer_token=USER_TOKEN
    )
    assert all_pos["source"] == "LIVE_VENUE_POSITIONS"
    assert len(all_pos["data"]) == 1
    assert all_pos["data"][0]["symbol"] == "BTC"
    assert all_pos["data"][0]["side"] == "SHORT"
    assert all_pos["data"][0]["quantity"] == "-0.01"

    # BTC-PERP 归一化后匹配 BTC
    btc_pos = await executor.execute(
        "get_positions", "u1", {"symbol": "BTC-PERP"}, scope, user_bearer_token=USER_TOKEN
    )
    assert len(btc_pos["data"]) == 1
    assert btc_pos["data"][0]["symbol"] == "BTC"

    # 非持仓标的筛选
    eth_pos = await executor.execute(
        "get_positions", "u1", {"symbol": "ETH"}, scope, user_bearer_token=USER_TOKEN
    )
    assert len(eth_pos["data"]) == 0


@pytest.mark.asyncio
async def test_get_balances_with_live_asset_snapshot() -> None:
    """get_balances 优先从 live asset-snapshot 提取现货 holdings 与合约可用保证金。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me/asset-snapshot":
            assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
            return httpx.Response(200, json=_live_asset_snapshot_payload())
        return httpx.Response(500)

    executor, _ = _make_executor(handler)
    scope = list(build_default_registry().names())

    # 查询全部余额
    all_bals = await executor.execute(
        "get_balances", "u1", {}, scope, user_bearer_token=USER_TOKEN
    )
    assert all_bals["source"] == "LIVE_ASSET_BALANCES"
    assert all_bals["total_usd_cents"] == "479"
    # 读的是哪两个地址必须回显，否则与 App 资产页不一致时无法判断钱包是否对得上。
    assert all_bals["trading_address"] == "0x1111111111111111111111111111111111111111"
    assert all_bals["collection_address"] == "0x2222222222222222222222222222222222222222"
    assets = {item["asset"] for item in all_bals["data"]}
    assert "USDT" in assets
    assert "USDC" in assets

    # 筛选 USDT
    usdt_bal = await executor.execute(
        "get_balances", "u1", {"asset": "USDT"}, scope, user_bearer_token=USER_TOKEN
    )
    assert len(usdt_bal["data"]) == 1
    assert usdt_bal["data"][0]["asset"] == "USDT"
    assert usdt_bal["data"][0]["balance"] == "2"

    # 筛选 USDC（读取合约可用保证金）
    usdc_bal = await executor.execute(
        "get_balances", "u1", {"asset": "USDC"}, scope, user_bearer_token=USER_TOKEN
    )
    assert len(usdc_bal["data"]) == 1
    assert usdc_bal["data"][0]["asset"] == "USDC"
    assert usdc_bal["data"][0]["balance"] == "2.79"


@pytest.mark.asyncio
async def test_get_account_summary_with_live_asset_snapshot() -> None:
    """get_account_summary 优先从 live asset-snapshot 组装现货、合约及持仓概览。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me/asset-snapshot":
            assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
            return httpx.Response(200, json=_live_asset_snapshot_payload())
        return httpx.Response(500)

    executor, _ = _make_executor(handler)
    scope = list(build_default_registry().names())

    summary = await executor.execute(
        "get_account_summary", "u1", {}, scope, user_bearer_token=USER_TOKEN
    )
    assert summary["source"] == "LIVE_ACCOUNT_SUMMARY"
    acc = summary["account_summary"]
    assert acc["spot"]["usd_cents"] == "200"
    assert acc["contract"]["withdrawable"] == "2.79"
    assert acc["total_usd_cents"] == "479"
    assert len(acc["positions"]) == 1


@pytest.mark.asyncio
async def test_live_asset_snapshot_unbound_wallet_graceful() -> None:
    """当用户未绑定钱包时，live asset-snapshot 返回 available=false，工具优雅说明不编造。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me/asset-snapshot":
            assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
            return httpx.Response(200, json=_live_asset_snapshot_payload(unbound=True))
        return httpx.Response(500)

    executor, _ = _make_executor(handler)
    scope = list(build_default_registry().names())

    risk = await executor.execute(
        "get_risk_summary", "u1", {}, scope, user_bearer_token=USER_TOKEN
    )
    assert risk["source"] == "LIVE_ASSET_SNAPSHOT"
    assert risk["status"] == "UNAVAILABLE"
    assert "未绑定" in risk["message"]

    pos = await executor.execute(
        "get_positions", "u1", {}, scope, user_bearer_token=USER_TOKEN
    )
    assert pos["source"] == "LIVE_VENUE_POSITIONS"
    assert pos["data"] == []
    assert pos["contract_available"] is False
    assert pos["trading_address"] == ""

    bals = await executor.execute(
        "get_balances", "u1", {}, scope, user_bearer_token=USER_TOKEN
    )
    assert bals["source"] == "LIVE_ASSET_BALANCES"
    assert bals["data"] == []

    summary = await executor.execute(
        "get_account_summary", "u1", {}, scope, user_bearer_token=USER_TOKEN
    )
    assert summary["source"] == "LIVE_ACCOUNT_SUMMARY"
    assert summary["account_summary"]["spot"]["available"] is False
    assert summary["account_summary"]["contract"]["available"] is False


@pytest.mark.asyncio
async def test_live_asset_snapshot_fallback_when_core_returns_404() -> None:
    """当 Core 尚未部署 asset-snapshot（返回 404）时，工具平滑回退至原有逻辑。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me/asset-snapshot":
            # 模拟 Core 尚未上线该端点
            return httpx.Response(404, json={"error": "Not Found"})
        if request.url.path == "/api/v1/me/trading-readiness":
            return httpx.Response(200, json=trading_readiness_payload())
        if request.url.path == "/api/v1/me/account-summary":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "balance": {"asset": "USDC", "available_minor": 5000000},
                        "positions": [
                            {"symbol": "BTC", "size": "0.5", "entry_price": "60000"},
                        ],
                        "orders": [],
                    }
                },
            )
        return httpx.Response(404)

    executor, _ = _make_executor(handler)
    scope = list(build_default_registry().names())

    # 1. get_risk_summary 回退到 trading-readiness
    risk = await executor.execute(
        "get_risk_summary", "u1", {}, scope, user_bearer_token=USER_TOKEN
    )
    assert risk["source"] == "CORE_RISK_SUMMARY"
    assert risk["status"] == "ACTIVE"
    assert risk["daily_notional_limit_minor"] == "50000000"

    # 2. get_positions 回退到 account-summary
    pos = await executor.execute(
        "get_positions", "u1", {}, scope, user_bearer_token=USER_TOKEN
    )
    assert pos["source"] == "CORE_USER_POSITIONS"
    assert len(pos["data"]) == 1
    assert pos["data"][0]["symbol"] == "BTC"


@pytest.mark.asyncio
async def test_get_balances_treats_spot_keyword_as_no_filter() -> None:
    """模型常把「现货」写进 asset 参数；这必须当成查全部，不能筛成空。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me/asset-snapshot":
            return httpx.Response(200, json=_live_asset_snapshot_payload())
        return httpx.Response(500)

    executor, _ = _make_executor(handler)
    scope = list(build_default_registry().names())
    out = await executor.execute(
        "get_balances", "u1", {"asset": "现货"}, scope, user_bearer_token=USER_TOKEN
    )
    assets = {item["asset"] for item in out["data"]}
    assert "USDT" in assets
    assert "USDC" in assets


@pytest.mark.asyncio
async def test_get_balances_explains_missing_bnb_instead_of_empty_success() -> None:
    """App 资产页能看到 BNB/Meme，但 Core 投影没有时，不能让模型当成查询失败或余额为 0。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me/asset-snapshot":
            return httpx.Response(200, json=_live_asset_snapshot_payload())
        return httpx.Response(500)

    executor, _ = _make_executor(handler)
    scope = list(build_default_registry().names())
    out = await executor.execute(
        "get_balances", "u1", {"asset": "BNB"}, scope, user_bearer_token=USER_TOKEN
    )
    assert out["data"] == []
    assert out["matched"] is False
    assert "BNB" in out["message"]
    assert "Meme" in out["message"]
    assert "投影里没有" in out["message"]


@pytest.mark.asyncio
async def test_get_balances_zero_usdt_is_projected_not_missing() -> None:
    """Core 会返回余额为 0 的 USDT；不能先滤掉再报「投影里没有」。"""

    payload = _live_asset_snapshot_payload()
    payload["data"]["spot"]["holdings"][0].update(
        {
            "trading_raw": "0",
            "collection_raw": "0",
            "total_raw": "0",
            "usd_cents": "0",
        }
    )
    payload["data"]["spot"]["usd_cents"] = "0"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me/asset-snapshot":
            return httpx.Response(200, json=payload)
        return httpx.Response(500)

    executor, _ = _make_executor(handler)
    scope = list(build_default_registry().names())
    out = await executor.execute(
        "get_balances", "u1", {"asset": "USDT"}, scope, user_bearer_token=USER_TOKEN
    )
    assert out["matched"] is True
    assert len(out["data"]) == 1
    assert out["data"][0]["asset"] == "USDT"
    assert "投影里没有" not in out["message"]
    assert "余额为 0" in out["message"]


@pytest.mark.asyncio
async def test_get_balances_matches_usdt_inside_messy_asset_filter() -> None:
    """模型可能把 BSC-USDT / 现货USDT 写进 asset，必须仍命中 USDT。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me/asset-snapshot":
            return httpx.Response(200, json=_live_asset_snapshot_payload())
        return httpx.Response(500)

    executor, _ = _make_executor(handler)
    scope = list(build_default_registry().names())
    for raw in ("BSC-USDT", "现货USDT"):
        out = await executor.execute(
            "get_balances", "u1", {"asset": raw}, scope, user_bearer_token=USER_TOKEN
        )
        assert out["data"][0]["asset"] == "USDT", raw
        assert out.get("matched") is not False


@pytest.mark.asyncio
async def test_get_positions_empty_points_to_spot_balances() -> None:
    """没有永续仓位时要说明现货不在 get_positions 里。"""

    payload = _live_asset_snapshot_payload()
    payload["data"]["positions"] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/me/asset-snapshot":
            return httpx.Response(200, json=payload)
        return httpx.Response(500)

    executor, _ = _make_executor(handler)
    scope = list(build_default_registry().names())
    out = await executor.execute(
        "get_positions", "u1", {}, scope, user_bearer_token=USER_TOKEN
    )
    assert out["data"] == []
    assert "get_balances" in out["message"]
    assert "现货" in out["message"]
