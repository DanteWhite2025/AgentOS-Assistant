"""Validated, secret-safe configuration for the Volc VoiceChat pipeline."""

from __future__ import annotations

import json
from typing import Any


def default_volc_voice_config(
    *,
    s2s_app_id: str,
    s2s_token: str,
    end_smooth_window_ms: int = 800,
) -> dict[str, Any]:
    return {
        "S2SConfig": {
            "Provider": "volcano",
            "OutputMode": 1,
            "ProviderParams": {
                "app": {"appid": s2s_app_id, "token": s2s_token},
                "dialog": {"extra": {"model": "O"}},
                "asr": {"extra": {"end_smooth_window_ms": end_smooth_window_ms}},
                "tts": {
                    "speaker": "zh_female_vv_jupiter_bigtts",
                    "audio_config": {
                        "channel": 1,
                        "format": "pcm_s16le",
                        "sample_rate": 24000,
                    },
                },
            },
        },
        "SubtitleConfig": {"DisableRTSSubtitle": False, "SubtitleMode": 1},
        "InterruptMode": 0,
    }


def load_volc_voice_config(
    *,
    override_json: str,
    s2s_app_id: str,
    s2s_token: str,
    end_smooth_window_ms: int = 800,
) -> dict[str, Any]:
    if not override_json:
        return default_volc_voice_config(
            s2s_app_id=s2s_app_id,
            s2s_token=s2s_token,
            end_smooth_window_ms=end_smooth_window_ms,
        )
    try:
        config = json.loads(override_json)
    except json.JSONDecodeError as exc:
        raise ValueError("AGENT_VOLC_VOICE_CHAT_CONFIG_JSON must be valid JSON") from exc
    if not isinstance(config, dict):
        raise ValueError("AGENT_VOLC_VOICE_CHAT_CONFIG_JSON must contain a JSON object")
    # Endpointing is an operational latency control, not a secret embedded in
    # the provider JSON. Keep it authoritative even when deployment supplies a
    # complete voice override so staging cannot silently retain an older value.
    try:
        provider_params = config["S2SConfig"]["ProviderParams"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "AGENT_VOLC_VOICE_CHAT_CONFIG_JSON requires S2SConfig.ProviderParams"
        ) from exc
    if not isinstance(provider_params, dict):
        raise ValueError("AGENT_VOLC_VOICE_CHAT_CONFIG_JSON requires S2SConfig.ProviderParams")
    asr = provider_params.setdefault("asr", {})
    if not isinstance(asr, dict):
        raise ValueError("AGENT_VOLC_VOICE_CHAT_CONFIG_JSON requires ASR configuration")
    extra = asr.setdefault("extra", {})
    if not isinstance(extra, dict):
        raise ValueError("AGENT_VOLC_VOICE_CHAT_CONFIG_JSON requires ASR extra configuration")
    extra["end_smooth_window_ms"] = end_smooth_window_ms
    return config


def validate_volc_voice_config(config: dict[str, Any]) -> None:
    """Fail closed on the S2S contract without returning configured values."""

    s2s = config.get("S2SConfig")
    if not isinstance(s2s, dict):
        raise ValueError("Volc voice config requires S2SConfig")
    if s2s.get("Provider") != "volcano":
        raise ValueError("Volc voice config requires S2SConfig.Provider=volcano")
    if s2s.get("OutputMode") != 1:
        raise ValueError("Volc voice config requires S2SConfig.OutputMode=1")

    params = s2s.get("ProviderParams")
    if not isinstance(params, dict):
        raise ValueError("Volc voice config requires S2SConfig.ProviderParams")
    app = params.get("app")
    if not isinstance(app, dict) or not all(
        isinstance(app.get(name), str) and app[name].strip() for name in ("appid", "token")
    ):
        raise ValueError("Volc voice config requires S2S app credentials")
    dialog = params.get("dialog")
    extra = dialog.get("extra") if isinstance(dialog, dict) else None
    if not isinstance(extra, dict) or extra.get("model") != "O":
        raise ValueError("Volc voice config requires dialog.extra.model=O")
    if not isinstance(params.get("asr"), dict) or not params["asr"]:
        raise ValueError("Volc voice config requires S2S ASR configuration")
    tts = params.get("tts")
    if (
        not isinstance(tts, dict)
        or not isinstance(tts.get("speaker"), str)
        or not tts["speaker"].strip()
    ):
        raise ValueError("Volc voice config requires S2S TTS speaker")
    audio = tts.get("audio_config")
    if not isinstance(audio, dict) or not all(
        name in audio for name in ("channel", "format", "sample_rate")
    ):
        raise ValueError("Volc voice config requires S2S TTS audio_config")

    subtitle = config.get("SubtitleConfig")
    if not isinstance(subtitle, dict) or subtitle.get("DisableRTSSubtitle") is not False:
        raise ValueError("Volc voice config must enable RTS subtitles")
    if config.get("InterruptMode") not in {0, 1}:
        raise ValueError("Volc voice config InterruptMode must be 0 or 1")
