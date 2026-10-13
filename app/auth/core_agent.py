"""Runtime-held Core AGENT credential validation and cached readiness.

Core remains the authority for signature, issuer, audience, actor type, and
scope enforcement.  The unverified JWT payload is inspected only to fail early
on expiry, identity drift, missing scopes, or accidentally over-privileged
tokens; a successful read-only Core request is still required for readiness.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from app.tools.core_client import CoreClient, MarketDataError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CoreCredentialSnapshot:
    status: str
    ready: bool
    core_verified: bool
    actor_type_valid: bool
    agent_id_valid: bool
    scopes_valid: bool
    forbidden_scopes_absent: bool
    expiring_soon: bool
    expires_in_seconds: int | None
    core_request_id: str | None
    last_verified_at: datetime | None

    def public_dict(self) -> dict[str, str | bool | int | None]:
        return {
            "status": self.status,
            "valid": self.ready,
            "core_verified": self.core_verified,
            "actor_type_valid": self.actor_type_valid,
            "agent_id_valid": self.agent_id_valid,
            "scopes_valid": self.scopes_valid,
            "forbidden_scopes_absent": self.forbidden_scopes_absent,
            "expiring_soon": self.expiring_soon,
            "expires_in_seconds": self.expires_in_seconds,
            "core_request_id": self.core_request_id,
            "last_verified_at": (
                self.last_verified_at.isoformat() if self.last_verified_at is not None else None
            ),
        }


def _initial_snapshot(status: str = "not_checked") -> CoreCredentialSnapshot:
    return CoreCredentialSnapshot(
        status=status,
        ready=False,
        core_verified=False,
        actor_type_valid=False,
        agent_id_valid=False,
        scopes_valid=False,
        forbidden_scopes_absent=False,
        expiring_soon=False,
        expires_in_seconds=None,
        core_request_id=None,
        last_verified_at=None,
    )


def _decode_unverified_claims(token: str) -> dict[str, Any]:
    """Decode payload for local safety checks; never authenticates the token."""

    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("credential is not a compact JWT")
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    decoded = base64.urlsafe_b64decode((payload + padding).encode("ascii"))
    claims = json.loads(decoded)
    if not isinstance(claims, dict):
        raise ValueError("JWT payload is not an object")
    return claims


Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


class CoreAgentCredentialReadiness:
    """Validates the Runtime AGENT credential and exposes a cached safe result."""

    def __init__(
        self,
        *,
        token: str,
        core_client: CoreClient,
        expected_agent_id: str,
        required_scopes: tuple[str, ...],
        forbidden_scopes: tuple[str, ...],
        warning_seconds: int,
        validation_interval_seconds: int,
        enabled: bool = True,
        clock: Clock | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._token = token
        self._core_client = core_client
        self._expected_agent_id = expected_agent_id
        self._required_scopes = frozenset(required_scopes)
        self._forbidden_scopes = frozenset(forbidden_scopes)
        self._warning_seconds = warning_seconds
        self._validation_interval_seconds = validation_interval_seconds
        self._enabled = enabled
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._snapshot = (
            _initial_snapshot() if enabled else replace(_initial_snapshot("not_applicable"), ready=True)
        )
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    @property
    def snapshot(self) -> CoreCredentialSnapshot:
        return self._snapshot

    async def start(self) -> None:
        if not self._enabled:
            return
        await self.validate_now()
        if self._task is None:
            self._task = asyncio.create_task(self._run_forever())

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run_forever(self) -> None:
        while True:
            await self._sleep(self._validation_interval_seconds)
            try:
                await self.validate_now()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - last-resort task containment
                logger.error(
                    "core_credential_validation_unexpected status=internal_error error_type=%s",
                    type(exc).__name__,
                )

    async def validate_now(self) -> CoreCredentialSnapshot:
        async with self._lock:
            snapshot = await self._validate()
            self._snapshot = snapshot
            self._log_snapshot(snapshot)
            return snapshot

    async def _validate(self) -> CoreCredentialSnapshot:
        now = self._clock()
        if not self._token:
            return replace(_initial_snapshot("missing"), last_verified_at=now)
        try:
            claims = _decode_unverified_claims(self._token)
        except (ValueError, TypeError, json.JSONDecodeError, binascii.Error, UnicodeError):
            return replace(_initial_snapshot("malformed"), last_verified_at=now)

        exp_raw = claims.get("exp")
        if not isinstance(exp_raw, (int, float)) or isinstance(exp_raw, bool):
            return replace(_initial_snapshot("malformed"), last_verified_at=now)
        expires_in = int(exp_raw - now.timestamp())
        expiring_soon = 0 < expires_in <= self._warning_seconds
        actor_valid = claims.get("actor_type") == "AGENT"
        agent_valid = (
            claims.get("agent_id") == self._expected_agent_id
            and claims.get("sub") == self._expected_agent_id
        )
        raw_scopes = claims.get("scopes")
        scopes = (
            {value for value in raw_scopes if isinstance(value, str)}
            if isinstance(raw_scopes, list)
            else set()
        )
        scopes_valid = self._required_scopes.issubset(scopes)
        forbidden_absent = not any(
            (
                forbidden.endswith("*")
                and any(scope.startswith(forbidden[:-1]) for scope in scopes)
            )
            or forbidden in scopes
            for forbidden in self._forbidden_scopes
        )
        base = CoreCredentialSnapshot(
            status="not_checked",
            ready=False,
            core_verified=False,
            actor_type_valid=actor_valid,
            agent_id_valid=agent_valid,
            scopes_valid=scopes_valid,
            forbidden_scopes_absent=forbidden_absent,
            expiring_soon=expiring_soon,
            expires_in_seconds=expires_in,
            core_request_id=None,
            last_verified_at=now,
        )
        if expires_in <= 0:
            return replace(base, status="expired")
        if not actor_valid:
            return replace(base, status="actor_type_mismatch")
        if not agent_valid:
            return replace(base, status="agent_id_mismatch")
        if not scopes_valid:
            return replace(base, status="scope_missing")
        if not forbidden_absent:
            return replace(base, status="forbidden_scope")

        request_id = f"req-runtime-credential-{int(now.timestamp())}"
        try:
            response = await self._core_client.verify_agent_credential(request_id)
        except MarketDataError as exc:
            if exc.code in {"MARKET_DATA_TIMEOUT", "MARKET_DATA_UNAVAILABLE"}:
                status = "core_unavailable"
            elif exc.code == "UNAUTHENTICATED":
                status = "core_rejected"
            elif exc.code in {"FORBIDDEN", "INSUFFICIENT_SCOPE"}:
                status = "core_forbidden"
            else:
                status = "core_preflight_failed"
            return replace(base, status=status, core_request_id=exc.request_id)
        return replace(
            base,
            status="valid",
            ready=True,
            core_verified=True,
            core_request_id=response.request_id,
        )

    def observe_market_data_error(self, exc: MarketDataError) -> None:
        """Immediately invalidate cached readiness after an operational failure."""

        if exc.code in {"MARKET_DATA_TIMEOUT", "MARKET_DATA_UNAVAILABLE"}:
            status = "core_unavailable"
        elif exc.code == "UNAUTHENTICATED" or exc.credential_refresh_required:
            status = "core_rejected"
        elif exc.code in {"FORBIDDEN", "INSUFFICIENT_SCOPE"}:
            status = "core_forbidden"
        else:
            return
        self._snapshot = replace(
            self._snapshot,
            status=status,
            ready=False,
            core_verified=False,
            core_request_id=exc.request_id,
            last_verified_at=self._clock(),
        )
        self._log_snapshot(self._snapshot)

    def observe_core_http_error(
        self, *, status_code: int, upstream_code: str, request_id: str | None
    ) -> None:
        if status_code == 401 or upstream_code == "UNAUTHENTICATED":
            status = "core_rejected"
        elif upstream_code == "INSUFFICIENT_SCOPE":
            status = "core_forbidden"
        elif status_code in {502, 503, 504}:
            status = "core_unavailable"
        else:
            return
        self._snapshot = replace(
            self._snapshot,
            status=status,
            ready=False,
            core_verified=False,
            core_request_id=request_id,
            last_verified_at=self._clock(),
        )
        self._log_snapshot(self._snapshot)

    @staticmethod
    def _log_snapshot(snapshot: CoreCredentialSnapshot) -> None:
        log = logger.warning if (not snapshot.ready or snapshot.expiring_soon) else logger.info
        log(
            "core_credential status=%s ready=%s core_verified=%s expires_in_seconds=%s "
            "expiring_soon=%s core_request_id=%s last_verified_at=%s",
            snapshot.status,
            snapshot.ready,
            snapshot.core_verified,
            snapshot.expires_in_seconds,
            snapshot.expiring_soon,
            snapshot.core_request_id,
            snapshot.last_verified_at.isoformat() if snapshot.last_verified_at else None,
        )
