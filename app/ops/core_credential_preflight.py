"""Fail-closed, read-only Core AGENT credential deployment preflight."""

from __future__ import annotations

import asyncio
import json

import httpx

from app.auth.core_agent import CoreAgentCredentialReadiness
from app.config import get_settings
from app.tools.core_client import HttpCoreClient


async def _run() -> int:
    settings = get_settings()
    async with httpx.AsyncClient(timeout=settings.core_timeout_seconds) as http:
        core = HttpCoreClient(
            settings.core_base_url,
            http,
            actor_token=settings.core_actor_token,
            timeout_seconds=settings.core_timeout_seconds,
            max_retries=0,
        )
        readiness = CoreAgentCredentialReadiness(
            token=settings.core_actor_token,
            core_client=core,
            expected_agent_id=settings.core_credential_agent_id,
            required_scopes=settings.required_core_scopes,
            forbidden_scopes=settings.forbidden_core_scopes,
            warning_seconds=settings.core_credential_expiry_warning_seconds,
            validation_interval_seconds=settings.core_credential_validation_interval_seconds,
        )
        snapshot = await readiness.validate_now()
    print(json.dumps(snapshot.public_dict(), separators=(",", ":")))
    return 0 if snapshot.ready else 1


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
