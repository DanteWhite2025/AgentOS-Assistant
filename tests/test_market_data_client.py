from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest

from app.tools.builtin import build_default_registry
from app.tools.core_client import (
    HttpCoreClient,
    MarketDataError,
    market_price_tool_output,
    market_tickers_tool_output,
)
from app.tools.executor import ToolExecutor


def _ticker(symbols: tuple[str, ...] = ("BTC",), *, stale: bool = False) -> dict[str, Any]:
    data = []
    for index, symbol in enumerate(symbols):
        data.append(
            {
                "symbol": symbol,
                "last_price": str(67345 + index),
                "mark_price": str(67340 + index),
                "oracle_price": str(67338 + index),
                "change_24h": "1234.50",
                "change_percent_24h": "1.87",
                "high_24h": None,
                "low_24h": None,
                "volume_24h": "12345.678",
                "quote_volume_24h": "831000000.12",
                "funding_rate": "0.0001",
                "open_interest": "20000.50",
                "source": "HYPERLIQUID_INFO_API",
                "environment": "MAINNET",
                "as_of": "2026-08-08T00:00:00Z",
                "stale": stale,
            }
        )
    return {
        "data": data,
        "meta": {
            "source": "HYPERLIQUID",
            "environment": "MAINNET",
            "as_of": "2026-08-08T00:00:00Z",
            "stale": stale,
            "next_cursor": None,
        },
        "request_id": "core_req_1",
    }


def _client(http: httpx.AsyncClient, **kwargs: Any) -> HttpCoreClient:
    return HttpCoreClient(
        "https://core.example",
        http,
        actor_token="agent-secret-never-log",
        jitter=lambda _start, _end: 0.0,
        **kwargs,
    )


async def test_market_tickers_use_agent_token_request_id_and_no_identity_params() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_ticker(("BTC", "ETH")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _client(http).get_market_tickers(
            [" btc ", "ETH", "btc"], request_id="runtime_req_1"
        )

    request = requests[0]
    assert request.url.path == "/internal/v1/market-data/tickers"
    assert dict(request.url.params) == {"symbols": "BTC,ETH"}
    assert not {"user_id", "unified_account_id", "venue_account_id", "wallet_address"} & set(
        request.url.params
    )
    assert request.headers["Authorization"] == "Bearer agent-secret-never-log"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["X-Request-ID"] == "runtime_req_1"
    assert [ticker.symbol for ticker in result.data] == ["BTC", "ETH"]


async def test_credential_preflight_checks_catalog_scope_with_get_only() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/internal/v1/market-data/tickers":
            return httpx.Response(200, json=_ticker())
        return httpx.Response(
            200,
            json={"data": [], "request_id": "req_catalog_read_verified"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        verified = await _client(http).verify_agent_credential("req_credential")

    assert verified.request_id == "req_catalog_read_verified"
    assert [request.method for request in requests] == ["GET"]
    assert [request.url.path for request in requests] == [
        "/internal/v1/markets",
    ]
    assert requests[0].headers["Authorization"] == "Bearer agent-secret-never-log"
    assert requests[0].headers["X-Request-ID"] == "req_credential"


async def test_credential_preflight_maps_missing_account_scope_without_leaking_body() -> None:
    leaked = "raw-core-body-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/internal/v1/market-data/tickers":
            return httpx.Response(200, json=_ticker())
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": "INSUFFICIENT_SCOPE",
                    "message": leaked,
                },
                "request_id": "req_account_scope",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(MarketDataError) as caught:
            await _client(http).verify_agent_credential("req_credential")
    assert caught.value.code == "INSUFFICIENT_SCOPE"
    assert caught.value.request_id == "req_account_scope"
    assert leaked not in str(caught.value)


async def test_market_ticker_decimal_fields_stay_strings_and_nullable_high_low() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_ticker()))
    ) as http:
        ticker = (await _client(http).get_market_tickers(["BTC"])).data[0]

    decimal_fields = (
        "last_price",
        "mark_price",
        "oracle_price",
        "change_24h",
        "change_percent_24h",
        "volume_24h",
        "quote_volume_24h",
        "funding_rate",
        "open_interest",
    )
    assert all(isinstance(getattr(ticker, field), str) for field in decimal_fields)
    assert ticker.high_24h is None
    assert ticker.low_24h is None


@pytest.mark.parametrize("symbols", [[], ["  "]])
async def test_market_tickers_reject_empty_symbols_locally(symbols: list[str]) -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(MarketDataError) as caught:
            await _client(http).get_market_tickers(symbols)
    assert caught.value.status_code == 400


async def test_market_tickers_reject_more_than_fifty_unique_symbols_locally() -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(MarketDataError, match="at most 50"):
            await _client(http).get_market_tickers([f"S{index}" for index in range(51)])


async def test_market_tickers_reject_missing_actor_token_and_bad_request_id_locally() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_ticker())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        missing = HttpCoreClient("https://core.example", http)
        with pytest.raises(MarketDataError) as caught:
            await missing.get_market_tickers(["BTC"])
        assert caught.value.credential_refresh_required is True
        with pytest.raises(MarketDataError, match="request_id"):
            await _client(http).get_market_tickers(["BTC"], request_id="")
    assert not requests


@pytest.mark.parametrize(
    ("status", "upstream_code", "expected", "refresh"),
    [
        (400, "INVALID_REQUEST", "INVALID_REQUEST", False),
        (400, "INVALID_SYMBOL", "INVALID_SYMBOL", False),
        (401, "UNAUTHENTICATED", "UNAUTHENTICATED", True),
        (403, "FORBIDDEN", "FORBIDDEN", False),
        (403, "INSUFFICIENT_SCOPE", "INSUFFICIENT_SCOPE", False),
        (500, "INTERNAL_ERROR", "INTERNAL_ERROR", False),
    ],
)
async def test_market_tickers_map_non_retryable_core_errors(
    status: int, upstream_code: str, expected: str, refresh: bool
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            json={"error": {"code": upstream_code}, "request_id": "core_error_req"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(MarketDataError) as caught:
            await _client(http).get_market_tickers(["BTC"])
    assert caught.value.code == expected
    assert caught.value.request_id == "core_error_req"
    assert caught.value.credential_refresh_required is refresh
    assert calls == 1


@pytest.mark.parametrize(
    ("status", "upstream_code", "expected"),
    [
        (429, "MARKET_DATA_RATE_LIMITED", "MARKET_DATA_RATE_LIMITED"),
        (503, "MARKET_DATA_TIMEOUT", "MARKET_DATA_TIMEOUT"),
        (503, "MARKET_DATA_UNAVAILABLE", "MARKET_DATA_UNAVAILABLE"),
    ],
)
async def test_market_tickers_retry_retryable_status_and_return_final_error(
    status: int, upstream_code: str, expected: str
) -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            headers={"Retry-After": "1"},
            json={"error": {"code": upstream_code}, "request_id": "retry_req"},
        )

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(MarketDataError) as caught:
            await _client(http, sleep=sleep).get_market_tickers(["BTC"])
    assert caught.value.code == expected
    assert calls == 3
    assert sleeps == [1.0, 1.0]


async def test_market_tickers_retry_timeout_then_succeed() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("timed out")
        return httpx.Response(200, json=_ticker())

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _client(http, sleep=sleep).get_market_tickers(["BTC"])
    assert result.data[0].symbol == "BTC"
    assert calls == 2
    assert sleeps == [0.25]


async def test_stale_success_is_returned_without_retry() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_ticker(stale=True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await _client(http).get_market_tickers(["BTC"])
    assert result.meta.stale is True
    assert calls == 1


async def test_market_tickers_reject_float_and_unexpected_provider_response() -> None:
    payload = _ticker()
    payload["data"][0]["mark_price"] = 67340.25
    payload["data"][0]["source"] = "UNTRUSTED_PROVIDER"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as http:
        with pytest.raises(MarketDataError, match="MARKET_DATA_INVALID_RESPONSE"):
            await _client(http).get_market_tickers(["BTC"])


async def test_market_logs_and_exception_never_contain_token_or_upstream_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "agent-secret-never-log"
    leaked_body = "private-upstream-stack-never-log"
    caplog.set_level(logging.INFO)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(500, text=leaked_body, headers={"X-Request-ID": "safe"})
        )
    ) as http:
        with pytest.raises(MarketDataError) as caught:
            await _client(http).get_market_tickers(["BTC"])
    rendered = caplog.text + str(caught.value)
    assert token not in rendered
    assert leaked_body not in rendered


async def test_market_tools_return_read_only_mainnet_context() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=_ticker(("BTC", "ETH"))))
    ) as http:
        response = await _client(http).get_market_tickers(["BTC", "ETH"])
    batch = market_tickers_tool_output(response)
    single = market_price_tool_output(response.model_copy(update={"data": [response.data[0]]}))
    assert batch["meta"]["source"] == "HYPERLIQUID"
    assert batch["meta"]["environment"] == "MAINNET"
    assert "not an order or fill" in batch["data_usage"]
    assert "not an order" in single["data_usage"]


async def test_market_tickers_agent_tool_uses_core_read_only_get() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_ticker(("BTC", "ETH")))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        core = _client(http)
        result = await ToolExecutor(build_default_registry(), core).execute(
            "market_tickers",
            "ignored-user-id",
            {"symbols": ["BTC", "ETH"]},
            ["market_tickers"],
        )
    assert result["meta"]["environment"] == "MAINNET"
    assert [item["symbol"] for item in result["data"]] == ["BTC", "ETH"]
    assert len(requests) == 1
    assert requests[0].method == "GET"


def test_runtime_source_has_no_direct_hyperliquid_market_call() -> None:
    from pathlib import Path

    app_source = "\n".join(path.read_text(encoding="utf-8") for path in Path("app").rglob("*.py"))
    assert "api.hyperliquid.xyz" not in app_source


async def test_market_tickers_caps_absurd_retry_after() -> None:
    """Core 给出分钟级 Retry-After 时不能照单全收——行情查询同样在用户等待的
    链路上，最多重试 2 次，累积起来就是分钟级的干等。上限取该客户端的超时预算。"""
    calls = 0
    sleeps: list[float] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            503,
            headers={"Retry-After": "300"},
            json={"error": {"code": "MARKET_DATA_UNAVAILABLE"}, "request_id": "slow_req"},
        )

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(MarketDataError):
            await _client(http, sleep=sleep, timeout_seconds=10.0).get_market_tickers(["BTC"])

    assert calls == 3
    assert sleeps == [10.0, 10.0], "退避被截断到 timeout_seconds，而不是 300 秒"
