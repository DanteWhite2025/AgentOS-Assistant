"""AgentOS Core client -- the Runtime's only controlled Core HTTP boundary.

Market data uses a Runtime-held AGENT Actor Token and never accepts a USER
Token or account identity from callers. Trading behavior remains unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

from app.tools.retry import backoff_sleep, retry_after_seconds

logger = logging.getLogger(__name__)

DecimalString = Annotated[
    str,
    StringConstraints(pattern=r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$"),
]


def _validate_rfc3339(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("must include an RFC3339 timezone")
    return value


class MarketTicker(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    symbol: Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{1,32}$")]
    last_price: DecimalString
    mark_price: DecimalString
    oracle_price: DecimalString
    change_24h: DecimalString
    change_percent_24h: DecimalString
    high_24h: DecimalString | None
    low_24h: DecimalString | None
    volume_24h: DecimalString
    quote_volume_24h: DecimalString
    funding_rate: DecimalString
    open_interest: DecimalString
    source: Literal["HYPERLIQUID_INFO_API"]
    environment: Literal["MAINNET"]
    as_of: str
    stale: bool

    _as_of_rfc3339 = field_validator("as_of")(_validate_rfc3339)


class MarketDataMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: Literal["HYPERLIQUID"]
    environment: Literal["MAINNET"]
    as_of: str
    stale: bool
    next_cursor: str | None

    _as_of_rfc3339 = field_validator("as_of")(_validate_rfc3339)


class MarketTickerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    data: list[MarketTicker]
    meta: MarketDataMeta
    request_id: str


@dataclass(frozen=True)
class CoreCredentialVerification:
    request_id: str


@dataclass(frozen=True)
class MarketDataError(RuntimeError):
    code: str
    message: str
    status_code: int
    request_id: str | None = None
    retryable: bool = False
    credential_refresh_required: bool = False

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class CoreClient(Protocol):
    async def get_resource(
        self, resource: str, user_id: str, params: dict[str, Any]
    ) -> dict[str, Any]: ...

    async def create_trade_draft(self, user_id: str, args: dict[str, Any]) -> dict[str, Any]: ...

    async def get_market_tickers(
        self,
        symbols: list[str],
        request_id: str | None = None,
    ) -> MarketTickerResponse: ...

    async def verify_agent_credential(self, request_id: str) -> CoreCredentialVerification: ...


_FAKE_DATA: dict[str, dict[str, Any]] = {
    "account_summary": {
        "cash_balance": "10000.00",
        "available_balance": "8500.00",
        "reserved_balance": "1500.00",
    },
    "balances": {"balances": [{"asset": "USDT", "amount": "10000.00"}]},
    "positions": {
        "positions": [{"symbol": "ETH", "side": "long", "qty": "1.2", "entry": "3000.00"}]
    },
    "orders": {"orders": []},
    "risk_summary": {"max_leverage": "5", "used": "1500.00", "limit": "5000.00"},
    "transaction_history": {"items": []},
}


class FakeCoreClient:
    """Stable non-market fixtures for isolated tests only."""

    async def get_resource(
        self, resource: str, user_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        data = _FAKE_DATA.get(resource)
        if data is None:
            return {"resource": resource, "user_id": user_id, "params": params}
        return {**data, "_source": "fake", "user_id": user_id}

    async def create_trade_draft(self, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "trade_draft_id": "td_" + secrets.token_hex(4),
            "status": "PENDING_CONFIRMATION",
            "user_id": user_id,
            "request": args,
        }

    async def get_market_tickers(
        self,
        symbols: list[str],
        request_id: str | None = None,
    ) -> MarketTickerResponse:
        raise RuntimeError("FakeCoreClient does not provide market prices")

    async def verify_agent_credential(self, request_id: str) -> CoreCredentialVerification:
        return CoreCredentialVerification(request_id=request_id)


Sleep = Callable[[float], Awaitable[None]]
Jitter = Callable[[float, float], float]


def _request_id() -> str:
    return "req_" + secrets.token_hex(16)


class HttpCoreClient:
    """Real Core client; market data is GET-only and uses the AGENT Actor Token."""

    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient,
        *,
        actor_token: str = "",
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        sleep: Sleep = asyncio.sleep,
        jitter: Jitter = random.uniform,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._client = client
        self._actor_token = actor_token
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._sleep = sleep
        self._jitter = jitter

    async def get_resource(
        self, resource: str, user_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        resp = await self._client.get(
            f"{self._base}/internal/v1/{resource}", params={"user_id": user_id, **params}
        )
        resp.raise_for_status()
        return resp.json()

    async def create_trade_draft(self, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.post(
            f"{self._base}/internal/v1/trade-drafts", json={"user_id": user_id, **args}
        )
        resp.raise_for_status()
        return resp.json()

    async def get_market_tickers(
        self,
        symbols: list[str],
        request_id: str | None = None,
    ) -> MarketTickerResponse:
        normalized = self._normalize_symbols(symbols)
        if not self._actor_token:
            raise MarketDataError(
                "UNAUTHENTICATED",
                "Runtime AGENT token is not configured",
                401,
                credential_refresh_required=True,
            )
        if request_id is not None and (not request_id.strip() or len(request_id) > 128):
            raise MarketDataError(
                "INVALID_REQUEST",
                "request_id must contain 1 to 128 characters",
                400,
            )
        outbound_request_id = request_id or _request_id()
        started = time.perf_counter()
        final_status: int | str = "request_error"
        try:
            for attempt in range(self._max_retries + 1):
                try:
                    response = await self._client.get(
                        f"{self._base}/internal/v1/market-data/tickers",
                        params={"symbols": ",".join(normalized)},
                        headers={
                            "Authorization": f"Bearer {self._actor_token}",
                            "Accept": "application/json",
                            "X-Request-ID": outbound_request_id,
                        },
                        timeout=self._timeout_seconds,
                    )
                    final_status = response.status_code
                except httpx.TimeoutException as exc:
                    final_status = "timeout"
                    if attempt < self._max_retries:
                        await self._retry_sleep(attempt, None)
                        continue
                    raise MarketDataError(
                        "MARKET_DATA_TIMEOUT",
                        "Core market data request timed out",
                        503,
                        outbound_request_id,
                        True,
                    ) from exc
                except httpx.RequestError as exc:
                    final_status = "unavailable"
                    if attempt < self._max_retries:
                        await self._retry_sleep(attempt, None)
                        continue
                    raise MarketDataError(
                        "MARKET_DATA_UNAVAILABLE",
                        "Core market data is temporarily unavailable",
                        503,
                        outbound_request_id,
                        True,
                    ) from exc

                if response.status_code >= 400:
                    error = self._response_error(response, outbound_request_id)
                    if error.retryable and attempt < self._max_retries:
                        await self._retry_sleep(attempt, response.headers.get("Retry-After"))
                        continue
                    raise error

                try:
                    parsed = MarketTickerResponse.model_validate(response.json())
                except (ValueError, TypeError) as exc:
                    raise MarketDataError(
                        "MARKET_DATA_INVALID_RESPONSE",
                        "Core returned an invalid market ticker response",
                        502,
                        outbound_request_id,
                    ) from exc
                returned_symbols = [ticker.symbol for ticker in parsed.data]
                if returned_symbols != normalized:
                    raise MarketDataError(
                        "MARKET_DATA_INVALID_RESPONSE",
                        "Core response symbols did not match the request",
                        502,
                        parsed.request_id,
                    )
                return parsed
            raise AssertionError("market retry loop exhausted without a result")
        finally:
            logger.info(
                "core_market_tickers request_id=%s symbols=%s status=%s latency_ms=%s stale=%s",
                outbound_request_id,
                ",".join(normalized),
                final_status,
                int((time.perf_counter() - started) * 1000),
                (
                    bool(parsed.meta.stale or any(ticker.stale for ticker in parsed.data))
                    if "parsed" in locals()
                    else False
                ),
            )

    async def verify_agent_credential(self, request_id: str) -> CoreCredentialVerification:
        """Exercise one Core AGENT-owned read without assuming a market symbol.

        Required scopes are checked from the short-lived credential before this
        call. Tradable symbols are Core catalog state, not Runtime health data.
        """

        credential_request_id = request_id
        try:
            response = await self._client.get(
                f"{self._base}/internal/v1/markets",
                headers={
                    "Authorization": f"Bearer {self._actor_token}",
                    "Accept": "application/json",
                    "X-Request-ID": credential_request_id,
                },
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise MarketDataError(
                "MARKET_DATA_TIMEOUT",
                "Core credential readiness request timed out",
                503,
                credential_request_id,
                True,
            ) from exc
        except httpx.RequestError as exc:
            raise MarketDataError(
                "MARKET_DATA_UNAVAILABLE",
                "Core credential readiness is temporarily unavailable",
                503,
                credential_request_id,
                True,
            ) from exc
        if response.status_code >= 400:
            raise self._credential_response_error(response, credential_request_id)
        try:
            payload = response.json()
        except ValueError as exc:
            raise MarketDataError(
                "CORE_CREDENTIAL_INVALID_RESPONSE",
                "Core returned an invalid credential readiness response",
                502,
                credential_request_id,
            ) from exc
        if not isinstance(payload, dict):
            raise MarketDataError(
                "CORE_CREDENTIAL_INVALID_RESPONSE",
                "Core returned an invalid credential readiness response",
                502,
                credential_request_id,
            )
        returned_request_id = str(payload.get("request_id") or credential_request_id)[:128]
        logger.info(
            "core_credential_read_preflight request_id=%s status=%s",
            returned_request_id,
            response.status_code,
        )
        return CoreCredentialVerification(request_id=returned_request_id)

    @staticmethod
    def _normalize_symbols(symbols: list[str]) -> list[str]:
        if not isinstance(symbols, list) or not symbols:
            raise MarketDataError("INVALID_REQUEST", "symbols must contain 1 to 50 items", 400)
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in symbols:
            if not isinstance(raw, str):
                raise MarketDataError("INVALID_SYMBOL", "each symbol must be a string", 400)
            symbol = raw.strip().upper()
            if not symbol:
                raise MarketDataError("INVALID_SYMBOL", "symbol must not be empty", 400)
            if symbol not in seen:
                normalized.append(symbol)
                seen.add(symbol)
        if len(normalized) > 50:
            raise MarketDataError("INVALID_REQUEST", "symbols must contain at most 50 items", 400)
        return normalized

    async def _retry_sleep(self, attempt: int, retry_after: str | None) -> None:
        # 退避曲线搬到 app/tools/retry.py 共享给 Tavily。曲线本身不变，只多了
        # 一个上限：行情查询同样在用户等待的链路上，而这里最多重试 2 次，
        # Core 若给出几十秒的 Retry-After，照单全收会累积成分钟级的等待。
        await backoff_sleep(
            attempt,
            retry_after,
            max_delay=self._timeout_seconds,
            sleep=self._sleep,
            jitter=self._jitter,
        )

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        return retry_after_seconds(value)

    @staticmethod
    def _response_error(response: httpx.Response, fallback_request_id: str) -> MarketDataError:
        payload: dict[str, Any]
        try:
            raw = response.json()
            payload = raw if isinstance(raw, dict) else {}
        except ValueError:
            payload = {}
        error = payload.get("error")
        error = error if isinstance(error, dict) else {}
        upstream_code = str(error.get("code") or "")[:128]
        request_id = str(payload.get("request_id") or fallback_request_id)[:128]

        if response.status_code == 400:
            code = (
                upstream_code
                if upstream_code in {"INVALID_REQUEST", "INVALID_SYMBOL"}
                else "INVALID_REQUEST"
            )
            return MarketDataError(code, "Core rejected the market data request", 400, request_id)
        if response.status_code == 401:
            return MarketDataError(
                "UNAUTHENTICATED",
                "Runtime AGENT token is invalid or expired",
                401,
                request_id,
                credential_refresh_required=True,
            )
        if response.status_code == 403:
            code = "INSUFFICIENT_SCOPE" if upstream_code == "INSUFFICIENT_SCOPE" else "FORBIDDEN"
            message = (
                "Runtime AGENT token lacks market:read"
                if code == "INSUFFICIENT_SCOPE"
                else "Core rejected the Runtime actor type"
            )
            return MarketDataError(code, message, 403, request_id)
        if response.status_code == 429:
            return MarketDataError(
                "MARKET_DATA_RATE_LIMITED",
                "Core market data is rate limited",
                429,
                request_id,
                True,
            )
        if response.status_code == 503:
            code = (
                upstream_code
                if upstream_code in {"MARKET_DATA_TIMEOUT", "MARKET_DATA_UNAVAILABLE"}
                else "MARKET_DATA_UNAVAILABLE"
            )
            message = (
                "Core market data request timed out"
                if code == "MARKET_DATA_TIMEOUT"
                else "Core market data is temporarily unavailable"
            )
            return MarketDataError(code, message, 503, request_id, True)
        if response.status_code == 500:
            return MarketDataError(
                "INTERNAL_ERROR",
                "Core market data request failed",
                500,
                request_id,
            )
        return MarketDataError(
            "CORE_MARKET_DATA_ERROR",
            "Core market data request failed",
            response.status_code,
            request_id,
        )

    @staticmethod
    def _credential_response_error(
        response: httpx.Response, fallback_request_id: str
    ) -> MarketDataError:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        error = payload.get("error")
        error = error if isinstance(error, dict) else {}
        upstream_code = str(error.get("code") or "")[:128]
        request_id = str(payload.get("request_id") or fallback_request_id)[:128]
        if response.status_code == 401:
            return MarketDataError(
                "UNAUTHENTICATED",
                "Runtime AGENT token is invalid or expired",
                401,
                request_id,
                credential_refresh_required=True,
            )
        if response.status_code == 403:
            code = "INSUFFICIENT_SCOPE" if upstream_code == "INSUFFICIENT_SCOPE" else "FORBIDDEN"
            return MarketDataError(code, "Core rejected Runtime account readiness", 403, request_id)
        if response.status_code == 429:
            return MarketDataError(
                "MARKET_DATA_RATE_LIMITED",
                "Core credential readiness is rate limited",
                429,
                request_id,
                True,
            )
        if response.status_code in {502, 503, 504}:
            return MarketDataError(
                "MARKET_DATA_UNAVAILABLE",
                "Core account readiness is temporarily unavailable",
                503,
                request_id,
                True,
            )
        return MarketDataError(
            "CORE_CREDENTIAL_ERROR",
            "Core account readiness failed",
            response.status_code,
            request_id,
        )


def market_tickers_tool_output(response: MarketTickerResponse) -> dict[str, Any]:
    return {
        "data": [ticker.model_dump(mode="json") for ticker in response.data],
        "meta": response.meta.model_dump(mode="json"),
        "request_id": response.request_id,
        "market_type": "PERP",
        "data_usage": "Hyperliquid Mainnet read-only market data; not an order or fill result",
    }


def market_price_tool_output(response: MarketTickerResponse) -> dict[str, Any]:
    ticker = response.data[0]
    return {
        "symbol": ticker.symbol,
        "market_type": "PERP",
        "last_price": ticker.last_price,
        "mark_price": ticker.mark_price,
        "change_percent_24h": ticker.change_percent_24h,
        "source": "hyperliquid",
        "environment": "mainnet",
        "as_of": ticker.as_of,
        "stale": bool(ticker.stale or response.meta.stale),
        "request_id": response.request_id,
        "data_usage": "market data only; not an order, fill, balance, or settlement result",
    }
