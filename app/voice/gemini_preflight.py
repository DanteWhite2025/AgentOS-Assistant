"""Cached Gemini Live v1beta credential and Constrained WebSocket preflight."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlencode

import certifi
from websockets.asyncio.client import connect

from app.voice.gemini_live import (
    GEMINI_CONTRACT_VERSION,
    GeminiLiveProviderError,
    GeminiLiveVoiceProvider,
)

logger = logging.getLogger(__name__)

GeminiPreflightStatus = Literal[
    "not_checked",
    "valid",
    "credentials_missing",
    "credential_rejected",
    "contract_rejected",
    "upstream_unavailable",
]


@dataclass(frozen=True)
class GeminiLivePreflightSnapshot:
    status: GeminiPreflightStatus
    credentials_configured: bool
    contract_version: str
    model: str
    connectivity_verified: bool
    last_verified_at: datetime | None = None
    detail: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "gemini_credentials_configured": self.credentials_configured,
            "gemini_contract_version": self.contract_version,
            "gemini_model": self.model,
            "gemini_connectivity_verified": self.connectivity_verified,
            "last_verified_at": (
                self.last_verified_at.isoformat() if self.last_verified_at is not None else None
            ),
            "detail": self.detail,
        }


class GeminiLiveConnectivityPreflight:
    """Mint one single-use token and require a real v1beta ``setupComplete``."""

    def __init__(
        self,
        *,
        provider: GeminiLiveVoiceProvider,
        api_key_configured: bool,
        model: str,
        timeout_seconds: float = 15.0,
        connector: Any = connect,
    ) -> None:
        self._provider = provider
        self._api_key_configured = api_key_configured
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._connector = connector
        self._ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._snapshot = GeminiLivePreflightSnapshot(
            status="not_checked",
            credentials_configured=api_key_configured,
            contract_version=GEMINI_CONTRACT_VERSION,
            model=model,
            connectivity_verified=False,
        )

    @property
    def snapshot(self) -> GeminiLivePreflightSnapshot:
        return self._snapshot

    async def check(self) -> GeminiLivePreflightSnapshot:
        checked_at = datetime.now(UTC)
        if not self._api_key_configured:
            return self._set_failure(
                "credentials_missing", "Gemini API credential is not configured", checked_at
            )
        try:
            credential = await self._provider.issue_credentials(
                "preflight_gemini_live",
                "preflight_runtime",
                ("market_tickers",),
                self._model,
                checked_at,
            )
        except GeminiLiveProviderError as exc:
            status: GeminiPreflightStatus = (
                "credential_rejected"
                if exc.upstream_status in {400, 401, 403}
                else "upstream_unavailable"
            )
            return self._set_failure(status, exc.reason, checked_at)

        if not credential.client_secret or not credential.endpoint_url:
            return self._set_failure(
                "contract_rejected", "Runtime credential contract is incomplete", checked_at
            )

        # Never store or log this URL: it contains the single-use ephemeral token.
        url = f"{credential.endpoint_url}?{urlencode({'access_token': credential.client_secret})}"
        try:
            async with self._connector(
                url,
                open_timeout=self._timeout_seconds,
                close_timeout=2,
                max_size=1_048_576,
                ssl=self._ssl_context,
            ) as websocket:
                await websocket.send(json.dumps(self._provider.client_setup()))
                raw = await asyncio.wait_for(
                    websocket.recv(), timeout=self._timeout_seconds
                )
        except Exception as exc:
            # Exception text can include the full URI and token, so only retain the type.
            return self._set_failure(
                "upstream_unavailable", f"websocket_{type(exc).__name__}", checked_at
            )

        try:
            payload = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._set_failure(
                "contract_rejected", "setup response is not valid JSON", checked_at
            )
        if not isinstance(payload, dict) or "setupComplete" not in payload:
            return self._set_failure(
                "contract_rejected", "setupComplete was not received", checked_at
            )

        self._snapshot = GeminiLivePreflightSnapshot(
            status="valid",
            credentials_configured=True,
            contract_version=GEMINI_CONTRACT_VERSION,
            model=self._model,
            connectivity_verified=True,
            last_verified_at=checked_at,
        )
        logger.info(
            "gemini_live_preflight status=valid contract=%s model=%s setup_complete=true",
            GEMINI_CONTRACT_VERSION,
            self._model,
        )
        return self._snapshot

    def _set_failure(
        self,
        status: GeminiPreflightStatus,
        detail: str,
        checked_at: datetime,
    ) -> GeminiLivePreflightSnapshot:
        self._snapshot = GeminiLivePreflightSnapshot(
            status=status,
            credentials_configured=self._api_key_configured,
            contract_version=GEMINI_CONTRACT_VERSION,
            model=self._model,
            connectivity_verified=False,
            last_verified_at=checked_at,
            detail=detail,
        )
        logger.warning(
            "gemini_live_preflight status=%s contract=%s model=%s detail=%s",
            status,
            GEMINI_CONTRACT_VERSION,
            self._model,
            detail,
        )
        return self._snapshot
