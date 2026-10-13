"""AgentOsStagingClient 契约构造 + 错误路径全覆盖（离线，MockTransport）。

写操作不真打（团队拍板）：只断言「请求构造 100% 符合 OpenAPI 契约」+ 错误信封冒泡。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.tools.agentos.client import AgentOsStagingClient

BASE = "https://api-staging.agentos-app.app"


def _client_capturing(status_code: int = 200, body: dict | None = None):
    """返回 (client, captured_requests, http)；handler 记录每个请求并回固定响应。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status_code, json=body if body is not None else {"data": {}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AgentOsStagingClient(BASE, token="TESTTOKEN", client=http), captured, http


async def test_user_trading_readiness_rejects_blank_transient_token() -> None:
    client, _, http = _client_capturing()
    async with http:
        with pytest.raises(ValueError, match="USER Bearer Token"):
            await client.get_user_trading_readiness("   ")


# ---- 只读三件套 ----


async def test_get_balances_construction():
    client, captured, http = _client_capturing()
    async with http:
        await client.get_balances("uacc_42")
    r = captured[-1]
    assert r.method == "GET"
    assert r.url.path == "/internal/v1/balances"
    assert (
        r.url.params.get("unified_account_id") == "uacc_42"
    )  # 契约参数名（trading_account_id 已废弃）
    assert r.headers.get("Authorization") == "Bearer TESTTOKEN"
    # 契约：每个请求都带 X-Request-ID + Accept
    assert r.headers.get("X-Request-ID", "").startswith("req_")
    assert r.headers.get("Accept") == "application/json"


async def test_get_user_balances_uses_transient_user_identity():
    client, captured, http = _client_capturing()
    async with http:
        await client.get_user_balances("uacc_42", "CURRENT_USER_TOKEN")
    r = captured[-1]
    assert r.method == "GET"
    assert r.url.path == "/api/v1/trading-accounts/uacc_42/balances"
    assert r.url.query == b""
    assert r.headers.get("Authorization") == "Bearer CURRENT_USER_TOKEN"
    assert "TESTTOKEN" not in r.headers.get("Authorization", "")
    assert r.headers.get("X-Request-ID", "").startswith("req_")


async def test_get_user_balances_rejects_blank_identity_or_account():
    client, _, http = _client_capturing()
    async with http:
        with pytest.raises(ValueError, match="Unified Account ID"):
            await client.get_user_balances("   ", "CURRENT_USER_TOKEN")
        with pytest.raises(ValueError, match="USER Bearer Token"):
            await client.get_user_balances("uacc_42", "   ")


async def test_get_positions_construction():
    client, captured, http = _client_capturing()
    async with http:
        await client.get_positions()
    r = captured[-1]
    assert r.method == "GET"
    assert r.url.path == "/internal/v1/positions"
    assert r.url.query == b""  # 无参


async def test_get_orders_construction():
    client, captured, http = _client_capturing()
    async with http:
        await client.get_orders()
    assert captured[-1].url.path == "/internal/v1/orders"


async def test_get_account_summary_construction():
    client, captured, http = _client_capturing()
    async with http:
        await client.get_account_summary("uacc_7")
    r = captured[-1]
    assert r.url.path == "/internal/v1/account-summary"
    assert r.url.params.get("unified_account_id") == "uacc_7"


async def test_get_user_account_summary_uses_transient_user_identity():
    client, captured, http = _client_capturing()
    async with http:
        await client.get_user_account_summary("uacc_42", "CURRENT_USER_TOKEN")
    r = captured[-1]
    assert r.method == "GET"
    assert r.url.path == "/api/v1/me/account-summary"
    assert r.url.params.get("unified_account_id") == "uacc_42"
    assert r.headers.get("Authorization") == "Bearer CURRENT_USER_TOKEN"
    assert "TESTTOKEN" not in r.headers.get("Authorization", "")
    assert r.headers.get("X-Request-ID", "").startswith("req_")


async def test_get_user_asset_snapshot_construction():
    client, captured, http = _client_capturing()
    async with http:
        await client.get_user_asset_snapshot("CURRENT_USER_TOKEN")
    r = captured[-1]
    assert r.method == "GET"
    assert r.url.path == "/api/v1/me/asset-snapshot"
    assert r.url.query == b""
    assert r.headers.get("Authorization") == "Bearer CURRENT_USER_TOKEN"
    assert "TESTTOKEN" not in r.headers.get("Authorization", "")
    assert r.headers.get("X-Request-ID", "").startswith("req_")
    assert r.headers.get("Accept") == "application/json"


async def test_get_user_asset_snapshot_rejects_blank_token():
    client, _, http = _client_capturing()
    async with http:
        with pytest.raises(ValueError, match="USER Bearer Token"):
            await client.get_user_asset_snapshot("   ")
        with pytest.raises(ValueError, match="USER Bearer Token"):
            await client.get_user_asset_snapshot("")


async def test_get_user_account_summary_rejects_blank_identity_or_token():
    client, _, http = _client_capturing()
    async with http:
        with pytest.raises(ValueError, match="Unified Account ID"):
            await client.get_user_account_summary("   ", "CURRENT_USER_TOKEN")
        with pytest.raises(ValueError, match="USER Bearer Token"):
            await client.get_user_account_summary("uacc_42", "   ")


async def test_get_agent_trading_accounts_construction():
    client, captured, http = _client_capturing()
    async with http:
        await client.get_agent_trading_accounts()
    r = captured[-1]
    assert r.method == "GET"
    assert r.url.path == "/internal/v1/agent-trading-accounts"
    assert r.url.query == b""  # 无参，身份从 token 派生


async def test_get_strategy_bindings_construction():
    client, captured, http = _client_capturing()
    async with http:
        await client.get_strategy_bindings()
    r = captured[-1]
    assert r.method == "GET"
    assert r.url.path == "/internal/v1/strategy-bindings"


async def test_get_strategy_binding_construction():
    client, captured, http = _client_capturing()
    async with http:
        await client.get_strategy_binding(" strategy_1 ")
    r = captured[-1]
    assert r.method == "GET"
    assert r.url.path == "/internal/v1/strategy-bindings/strategy_1"


async def test_get_strategy_binding_rejects_blank_id():
    client, _captured, http = _client_capturing()
    async with http:
        with pytest.raises(ValueError, match="strategy_id 不能为空"):
            await client.get_strategy_binding("   ")


async def test_market_catalog_uses_agent_contract_and_agent_credential():
    client, captured, http = _client_capturing()
    async with http:
        await client.get_market_catalog(
            market_type="perp", query="ETH", cursor="next-page", limit=10
        )
        await client.get_market_instrument("ins_perp_hyperliquid_testnet_eth")

    page, instrument = captured
    assert page.url.path == "/internal/v1/markets"
    assert page.url.params.get("market_type") == "PERP"
    assert page.url.params.get("trade_supported") == "true"
    assert page.url.params.get("cursor") == "next-page"
    assert page.headers.get("Authorization") == "Bearer TESTTOKEN"
    assert instrument.url.path == "/internal/v1/markets/ins_perp_hyperliquid_testnet_eth"
    assert instrument.headers.get("Authorization") == "Bearer TESTTOKEN"


async def test_market_catalog_rejects_unknown_market_type():
    client, captured, http = _client_capturing()
    async with http:
        with pytest.raises(ValueError, match="SPOT 或 PERP"):
            await client.get_market_catalog(market_type="OPTIONS")
    assert captured == []


async def test_market_catalog_all_types_omits_optional_filters():
    client, captured, http = _client_capturing()
    async with http:
        await client.get_market_catalog()
    request = captured[-1]
    assert request.url.path == "/internal/v1/markets"
    assert "market_type" not in request.url.params
    assert "cursor" not in request.url.params


async def test_market_instrument_rejects_blank_id():
    client, captured, http = _client_capturing()
    async with http:
        with pytest.raises(ValueError, match="instrument_id"):
            await client.get_market_instrument("   ")
    assert captured == []


# ---- 写：create_agent_trade_intent ----


async def test_create_agent_trade_intent_construction():
    client, captured, http = _client_capturing(
        201, {"data": {"id": "aint_1", "status": "PENDING_USER"}, "request_id": "req_1"}
    )
    body = {
        "source": "VOICE",
        "venue": "HYPERLIQUID",
        "symbol": "BTC-USD",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": "0.001",
        "leverage": "1",
        "reduce_only": False,
    }
    async with http:
        out = await client.create_agent_trade_intent(
            body,
            idempotency_key="idem_fixed_123456",
            user_bearer_token="mobile-user-token",
        )
    r = captured[-1]
    assert r.method == "POST"
    assert r.url.path == "/api/v1/me/agent-trade-intents"
    assert r.headers.get("Authorization") == "Bearer mobile-user-token"
    assert r.headers.get("Idempotency-Key") == "idem_fixed_123456"
    assert r.headers.get("X-Request-ID", "").startswith("req_")
    assert json.loads(r.content) == body
    assert out == {"data": {"id": "aint_1", "status": "PENDING_USER"}, "request_id": "req_1"}


async def test_create_agent_trade_intent_rejects_short_idempotency_key():
    client, captured, http = _client_capturing(201, {"data": {}})
    async with http:
        with pytest.raises(ValueError, match="16"):
            await client.create_agent_trade_intent(
                {"x": 1},
                idempotency_key="shortkey",
                user_bearer_token="mobile-user-token",
            )
    assert captured == []


async def test_create_agent_trade_intent_rejects_blank_user_bearer_token():
    client, captured, http = _client_capturing(201, {"data": {}})
    async with http:
        with pytest.raises(ValueError, match="USER Bearer Token"):
            await client.create_agent_trade_intent(
                {"x": 1},
                idempotency_key="idem_fixed_123456",
                user_bearer_token="   ",
            )
    assert captured == []


async def test_create_agent_trade_intent_strips_idempotency_key():
    client, captured, http = _client_capturing(
        201, {"data": {"id": "aint_1", "status": "PENDING_USER"}, "request_id": "req_1"}
    )
    async with http:
        await client.create_agent_trade_intent(
            {"x": 1},
            idempotency_key="  idem_fixed_123456  ",
            user_bearer_token="mobile-user-token",
        )
    r = captured[-1]
    assert r.headers.get("Idempotency-Key") == "idem_fixed_123456"


# ---- 写：create_trade_draft ----


async def test_create_trade_draft_construction():
    client, captured, http = _client_capturing(201, {"data": {"id": "draft_1"}})
    body = {"symbol": "ETH-USDC", "side": "BUY", "order_type": "LIMIT"}
    async with http:
        out = await client.create_trade_draft(body, idempotency_key="idem_fixed")
    r = captured[-1]
    assert r.method == "POST"
    assert r.url.path == "/internal/v1/trade-drafts"
    assert r.headers.get("Idempotency-Key") == "idem_fixed"
    assert r.headers.get("X-Request-ID", "").startswith("req_")  # 写请求必带 X-Request-ID
    assert json.loads(r.content) == body
    assert out == {"data": {"id": "draft_1"}}


async def test_create_trade_draft_rejects_blank_idempotency_key():
    client, captured, http = _client_capturing(201, {"data": {}})
    async with http:
        with pytest.raises(ValueError, match="稳定 Idempotency-Key"):
            await client.create_trade_draft({"x": 1}, idempotency_key="   ")
    assert captured == []


async def test_create_trade_draft_timeout_reuses_same_idempotency_key() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if len(captured) == 1:
            raise httpx.ReadTimeout("accepted but response lost", request=request)
        return httpx.Response(201, json={"data": {"id": "draft_recovered", "version": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AgentOsStagingClient(BASE, token="T", client=http)
        response = await client.create_trade_draft(
            {"symbol": "ETH"}, idempotency_key="stable-user-intent"
        )

    assert response["data"]["id"] == "draft_recovered"
    assert len(captured) == 2
    assert {request.headers["Idempotency-Key"] for request in captured} == {"stable-user-intent"}


async def test_create_trade_draft_second_timeout_propagates_with_same_key() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        raise httpx.ReadTimeout("still unknown", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AgentOsStagingClient(BASE, token="T", client=http)
        with pytest.raises(httpx.ReadTimeout):
            await client.create_trade_draft({"symbol": "ETH"}, idempotency_key="stable-intent")

    assert len(captured) == 2
    assert {request.headers["Idempotency-Key"] for request in captured} == {"stable-intent"}


async def test_spot_draft_create_and_recovery_contract():
    client, captured, http = _client_capturing(200, {"data": {"id": "swap_1"}})
    async with http:
        await client.create_spot_swap_draft(
            {"chain_id": 97},
            idempotency_key="spot-intent",
            user_bearer_token="user-token-123",
        )
        await client.get_spot_swap_draft(" swap_1 ", user_bearer_token="user-token-123")

    create, recover = captured
    assert create.url.path == "/api/v1/swap-drafts"
    assert create.headers["Authorization"] == "Bearer user-token-123"
    assert create.headers["Idempotency-Key"] == "spot-intent"
    assert recover.url.path == "/api/v1/swap-drafts/swap_1"
    assert recover.headers["Authorization"] == "Bearer user-token-123"


async def test_spot_draft_recovery_rejects_blank_id_or_token():
    client, captured, http = _client_capturing()
    async with http:
        with pytest.raises(ValueError, match="draft_id"):
            await client.get_spot_swap_draft("  ", user_bearer_token="token")
        with pytest.raises(ValueError, match="USER Bearer Token"):
            await client.get_spot_swap_draft("swap_1", user_bearer_token="  ")
    assert captured == []


async def test_create_spot_swap_draft_validates_key_and_token():
    client, captured, http = _client_capturing()
    async with http:
        with pytest.raises(ValueError, match="idempotency_key"):
            await client.create_spot_swap_draft(
                {"chain_id": 97}, idempotency_key="  ", user_bearer_token="token"
            )
        with pytest.raises(ValueError, match="USER Bearer Token"):
            await client.create_spot_swap_draft(
                {"chain_id": 97}, idempotency_key="key", user_bearer_token="  "
            )
    assert captured == []


def test_runtime_client_has_no_confirm_execution_or_step_up_surface():
    client, _, _ = _client_capturing(200, {"data": {}})
    assert not hasattr(client, "confirm_trade_draft")
    assert not hasattr(client, "request_execution")
    assert not hasattr(client, "cancel_execution")
    assert not hasattr(client, "cancel_trade_draft")


# ---- base_url 末尾斜杠归一 ----


async def test_base_url_trailing_slash_normalized():
    captured: list[httpx.Request] = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"data": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AgentOsStagingClient(BASE + "///", token="T", client=http)
        await client.get_positions()
    # 不能出现双斜杠
    assert "//internal" not in str(captured[-1].url)
    assert captured[-1].url.path == "/internal/v1/positions"


# ---- 错误路径：4xx/5xx 必须 raise_for_status 冒泡，错误信封可解析 ----


@pytest.mark.parametrize(
    "code, err_code, retryable",
    [
        (400, "INVALID_REQUEST", False),
        (401, "UNAUTHENTICATED", False),
        (404, "NOT_FOUND", False),
        (409, "IDEMPOTENCY_CONFLICT", False),
        (500, "INTERNAL", True),
        (502, "UPSTREAM", True),
    ],
)
async def test_error_status_propagates(code, err_code, retryable):
    envelope = {"error": {"code": err_code, "message": "boom", "retryable": retryable}}

    def handler(request):
        return httpx.Response(code, json=envelope)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AgentOsStagingClient(BASE, token="T", client=http)
        with pytest.raises(httpx.HTTPStatusError) as exc:
            await client.get_positions()
    # 错误信封结构可被上层解析（用于映射为对用户友好的错误码）
    assert exc.value.response.status_code == code
    assert exc.value.response.json()["error"]["code"] == err_code


async def test_error_status_propagates_on_post():
    def handler(request):
        return httpx.Response(409, json={"error": {"code": "IDEMPOTENCY_CONFLICT"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AgentOsStagingClient(BASE, token="T", client=http)
        with pytest.raises(httpx.HTTPStatusError):
            await client.create_trade_draft({"x": 1}, idempotency_key="dup")
