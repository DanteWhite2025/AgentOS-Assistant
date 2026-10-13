from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.auth.core_agent import CoreAgentCredentialReadiness
from app.tools.core_client import (
    CoreCredentialVerification,
    MarketDataError,
    MarketTickerResponse,
)


def _token(now: datetime, **overrides: Any) -> str:
    claims: dict[str, Any] = {
        "sub": "agent-test-001",
        "actor_type": "AGENT",
        "agent_id": "agent-test-001",
        "exp": int((now + timedelta(days=7)).timestamp()),
        "scopes": ["market:read", "account:read", "trade:draft"],
    }
    claims.update(overrides)
    payload = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


def _ticker() -> MarketTickerResponse:
    return MarketTickerResponse.model_validate(
        {
            "data": [
                {
                    "symbol": "BTC",
                    "last_price": "1",
                    "mark_price": "1",
                    "oracle_price": "1",
                    "change_24h": "0",
                    "change_percent_24h": "0",
                    "high_24h": "1",
                    "low_24h": "1",
                    "volume_24h": "1",
                    "quote_volume_24h": "1",
                    "funding_rate": "0",
                    "open_interest": "1",
                    "source": "HYPERLIQUID_INFO_API",
                    "environment": "MAINNET",
                    "as_of": "2026-08-15T00:00:00+00:00",
                    "stale": False,
                }
            ],
            "meta": {
                "source": "HYPERLIQUID",
                "environment": "MAINNET",
                "as_of": "2026-08-15T00:00:00+00:00",
                "stale": False,
                "next_cursor": None,
            },
            "request_id": "req_core_verified",
        }
    )


class ProbeCore:
    def __init__(self, result: MarketTickerResponse | MarketDataError | None = None) -> None:
        self.result = result or _ticker()
        self.calls = 0

    async def get_market_tickers(
        self, symbols: list[str], request_id: str | None = None
    ) -> MarketTickerResponse:
        self.calls += 1
        if isinstance(self.result, MarketDataError):
            raise self.result
        return self.result

    async def verify_agent_credential(self, request_id: str) -> CoreCredentialVerification:
        self.calls += 1
        if isinstance(self.result, MarketDataError):
            raise self.result
        return CoreCredentialVerification(request_id="req_core_verified")


def _readiness(
    now: datetime,
    *,
    token: str,
    core: ProbeCore | None = None,
    warning_seconds: int = 86400,
) -> tuple[CoreAgentCredentialReadiness, ProbeCore]:
    probe = core or ProbeCore()
    service = CoreAgentCredentialReadiness(
        token=token,
        core_client=probe,  # type: ignore[arg-type]
        expected_agent_id="agent-test-001",
        required_scopes=("market:read", "account:read", "trade:draft"),
        forbidden_scopes=("trade:confirm", "trade:execute", "admin:*"),
        warning_seconds=warning_seconds,
        validation_interval_seconds=300,
        clock=lambda: now,
    )
    return service, probe


@pytest.mark.parametrize(
    ("token_factory", "expected"),
    [
        (lambda now: "", "missing"),
        (lambda now: "not-a-jwt", "malformed"),
        (lambda now: _token(now, exp=int((now - timedelta(seconds=1)).timestamp())), "expired"),
        (lambda now: _token(now, actor_type="USER"), "actor_type_mismatch"),
        (lambda now: _token(now, agent_id="agent-other"), "agent_id_mismatch"),
        (lambda now: _token(now, sub="agent-other"), "agent_id_mismatch"),
        (lambda now: _token(now, scopes=["market:read", "account:read"]), "scope_missing"),
        (
            lambda now: _token(
                now,
                scopes=["market:read", "account:read", "trade:draft", "trade:execute"],
            ),
            "forbidden_scope",
        ),
        (
            lambda now: _token(
                now,
                scopes=["market:read", "account:read", "trade:draft", "admin:write"],
            ),
            "forbidden_scope",
        ),
    ],
)
async def test_local_fail_closed_checks_do_not_call_core(token_factory, expected: str) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    service, core = _readiness(now, token=token_factory(now))
    snapshot = await service.validate_now()
    assert snapshot.status == expected
    assert snapshot.ready is False
    assert core.calls == 0


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (MarketDataError("UNAUTHENTICATED", "secret upstream body", 401, "req_401"), "core_rejected"),
        (MarketDataError("FORBIDDEN", "actor rejected", 403, "req_403"), "core_forbidden"),
        (
            MarketDataError("MARKET_DATA_UNAVAILABLE", "network detail", 503, "req_503", True),
            "core_unavailable",
        ),
    ],
)
async def test_core_is_authoritative_and_failure_states_are_distinct(
    error: MarketDataError, expected: str
) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    service, core = _readiness(now, token=_token(now), core=ProbeCore(error))
    snapshot = await service.validate_now()
    assert snapshot.status == expected
    assert snapshot.ready is False
    assert snapshot.core_request_id == error.request_id
    assert core.calls == 1


async def test_valid_token_requires_successful_core_preflight_and_is_cached() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    service, core = _readiness(now, token=_token(now))
    snapshot = await service.validate_now()
    assert snapshot.status == "valid"
    assert snapshot.ready is snapshot.core_verified is True
    assert snapshot.core_request_id == "req_core_verified"
    assert core.calls == 1

    # Reading the snapshot is a pure cache read and does not contact Core.
    assert service.snapshot.public_dict()["status"] == "valid"
    assert core.calls == 1

    core.result = MarketDataError(
        "MARKET_DATA_UNAVAILABLE", "not returned", 503, "req_later", True
    )
    refreshed = await service.validate_now()
    assert refreshed.status == "core_unavailable"
    assert core.calls == 2


async def test_expiry_warning_threshold_is_structured_and_never_logs_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    token = _token(now, exp=int((now + timedelta(hours=12)).timestamp()))
    service, _core = _readiness(now, token=token, warning_seconds=86400)
    with caplog.at_level(logging.INFO, logger="app.auth.core_agent"):
        snapshot = await service.validate_now()
    assert snapshot.ready is True
    assert snapshot.expiring_soon is True
    assert snapshot.expires_in_seconds == 12 * 60 * 60
    assert token not in caplog.text
    assert "expiring_soon=True" in caplog.text


def test_runtime_market_failure_immediately_invalidates_cached_readiness() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    service, _core = _readiness(now, token=_token(now))
    service.observe_market_data_error(
        MarketDataError(
            "UNAUTHENTICATED",
            "must never be logged",
            401,
            "req_runtime_401",
            credential_refresh_required=True,
        )
    )
    assert service.snapshot.status == "core_rejected"
    assert service.snapshot.ready is False


async def test_operation_forbidden_does_not_poison_valid_global_readiness() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    service, _core = _readiness(now, token=_token(now))
    await service.validate_now()

    service.observe_core_http_error(
        status_code=403,
        upstream_code="FORBIDDEN",
        request_id="req_business_forbidden",
    )

    assert service.snapshot.status == "valid"
    assert service.snapshot.ready is True
    assert service.snapshot.core_verified is True


async def test_background_task_periodically_revalidates_cached_state() -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    core = ProbeCore()
    gate = asyncio.Event()

    async def controlled_sleep(_seconds: float) -> None:
        await gate.wait()
        gate.clear()

    service = CoreAgentCredentialReadiness(
        token=_token(now),
        core_client=core,  # type: ignore[arg-type]
        expected_agent_id="agent-test-001",
        required_scopes=("market:read", "account:read", "trade:draft"),
        forbidden_scopes=("trade:confirm", "trade:execute"),
        warning_seconds=86400,
        validation_interval_seconds=300,
        clock=lambda: now,
        sleep=controlled_sleep,
    )
    await service.start()
    assert service.snapshot.status == "valid"
    core.result = MarketDataError(
        "MARKET_DATA_UNAVAILABLE", "not returned", 503, "req_background", True
    )
    gate.set()
    for _ in range(20):
        if service.snapshot.status == "core_unavailable":
            break
        await asyncio.sleep(0)
    assert service.snapshot.status == "core_unavailable"
    assert core.calls == 2
    await service.close()
