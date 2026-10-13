"""Deployment gate for Gemini Live v1beta ephemeral-token connectivity."""

from __future__ import annotations

import asyncio
import json

import httpx

from app.config import Settings
from app.voice.gemini_live import GeminiLiveVoiceProvider
from app.voice.gemini_preflight import GeminiLiveConnectivityPreflight


async def _run() -> int:
    settings = Settings()
    if "gemini_live" not in settings.enabled_voice_provider_names:
        print('gemini_live_preflight={"status":"not_selected"}')
        return 0
    async with httpx.AsyncClient(timeout=20.0) as client:
        preflight = GeminiLiveConnectivityPreflight(
            provider=GeminiLiveVoiceProvider(
                settings.gemini_api_key,
                client,
                instructions="必须始终使用简体中文回答。",
                model=settings.gemini_live_model,
                base_url=settings.gemini_base_url,
                token_ttl_seconds=settings.gemini_token_ttl_seconds,
                session_start_window_seconds=(settings.gemini_session_start_window_seconds),
            ),
            api_key_configured=bool(settings.gemini_api_key.strip()),
            model=settings.gemini_live_model,
        )
        snapshot = await preflight.check()
    print(
        "gemini_live_preflight="
        + json.dumps(snapshot.public_dict(), separators=(",", ":"), default=str)
    )
    return 0 if snapshot.connectivity_verified else 1


def main() -> int:
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
