"""健康检查。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.api.deps import get_container
from app.config import get_settings
from app.container import Container

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "env": settings.env,
        "meaning": "liveness_only",
    }


@router.get("/readyz")
async def readyz(
    container: Annotated[Container, Depends(get_container)],
) -> JSONResponse:
    """Cached dependency readiness; it is not a real-device voice-loop claim."""

    real = container.settings.provider_mode == "real"
    credential = container.core_credential_readiness.snapshot
    preflight = container.llm_model_preflight
    llm_snapshot = preflight.snapshot if preflight is not None else None
    # 只有配置错误（模型不存在 / key 被拒）才拦 readiness；站点抖动不拦，
    # 否则启动瞬间的一次超时会让服务永久 not_ready。
    llm_blocks = llm_snapshot is not None and llm_snapshot.blocks_readiness
    enabled_voice = set(container.settings.enabled_voice_provider_names)
    volc = real and container.settings.realtime_voice_provider == "volc_rtc"
    gemini = real and container.settings.realtime_voice_provider == "gemini_live"
    pipeline = real and container.settings.realtime_voice_provider == "pipeline"
    pipeline_enabled = real and "pipeline" in enabled_voice
    gemini_enabled = real and "gemini_live" in enabled_voice
    gemini_preflight = container.gemini_live_preflight
    gemini_snapshot = gemini_preflight.snapshot if gemini_preflight is not None else None
    gemini_connectivity_verified = bool(
        gemini_snapshot is not None and gemini_snapshot.connectivity_verified
    )
    ready = not real or (
        credential.ready and not llm_blocks and (not gemini or gemini_connectivity_verified)
    )
    if volc:
        voice_configuration = {
            "runtime_contract_valid": True,
            "provider": "volc_rtc",
            "rtc_application_configured": bool(
                container.settings.rtc_app_id and container.settings.rtc_app_key
            ),
            "s2s_credentials_configured": bool(
                container.settings.huochan_s2s_app_id and container.settings.huochan_s2s_token
            ),
            "ark_configured": bool(
                container.settings.volc_ark_model and container.settings.volc_ark_endpoint_id
            ),
            "state_callback_configured": bool(
                container.settings.volc_callback_url and container.settings.volc_callback_secret
            ),
            "function_callback_configured": bool(
                container.settings.volc_function_callback_url
                and container.settings.volc_function_callback_secret
            ),
            "provider_account_verified": False,
            "bot_presence_verified": False,
        }
    elif gemini:
        voice_configuration = {
            "runtime_contract_valid": True,
            "provider": "gemini_live",
            "gemini_credentials_configured": bool(container.settings.gemini_api_key.strip()),
            "gemini_contract_version": (
                gemini_snapshot.contract_version if gemini_snapshot is not None else "v1beta"
            ),
            "gemini_model": container.settings.gemini_live_model,
            "gemini_connectivity_verified": gemini_connectivity_verified,
            "preflight_status": (
                gemini_snapshot.status if gemini_snapshot is not None else "not_configured"
            ),
        }
    elif pipeline:
        # 02 方案流水线：音频由 Runtime 合成后经 WS 下发，既不签票也没有 Bot
        # 进房，所以这里没有 rtc_* / bot_presence_* 那两组字段。音色和语速要
        # 报出来 —— 听感投诉排查的第一步就是确认线上到底在用哪个音色。
        voice_configuration = {
            "runtime_contract_valid": True,
            "provider": "pipeline",
            "tts_credentials_configured": bool(container.settings.huochan_api_key.strip()),
            "tts_voice": container.settings.huochan_voice_type,
            "tts_speed_ratio": container.settings.huochan_speed_ratio,
            "tts_resource_id": container.settings.huochan_resource_id,
            "issues_direct_link_credentials": False,
        }
    else:
        voice_configuration = {
            "runtime_contract_valid": False,
            "provider": "demo_fake",
        }
    content = {
        "status": "ready" if ready else "not_ready",
        "service": container.settings.app_name,
        "env": container.settings.env,
        "provider_mode": container.settings.provider_mode,
        "default_voice_mode": (container.settings.realtime_voice_provider if real else "demo_fake"),
        "realtime_voice_provider": (
            container.settings.realtime_voice_provider if real else "demo_fake"
        ),
        "gemini_credentials_configured": bool(
            gemini_enabled and container.settings.gemini_api_key.strip()
        ),
        "gemini_contract_version": (
            gemini_snapshot.contract_version if gemini_snapshot is not None else None
        ),
        "gemini_model": container.settings.gemini_live_model if gemini_enabled else None,
        "gemini_connectivity_verified": gemini_connectivity_verified,
        "voice_modes": {
            "pipeline": {
                "enabled": pipeline_enabled,
                "available": bool(
                    pipeline_enabled
                    and container.voice_pipeline is not None
                    and container.settings.huochan_api_key.strip()
                ),
                "tts_configured": bool(
                    pipeline_enabled and container.settings.huochan_api_key.strip()
                ),
            },
            "gemini_live": {
                "enabled": gemini_enabled,
                "available": bool(
                    gemini_enabled
                    and container.settings.gemini_api_key.strip()
                    and gemini_connectivity_verified
                ),
                "credentials_configured": bool(
                    gemini_enabled and container.settings.gemini_api_key.strip()
                ),
                "connectivity_verified": gemini_connectivity_verified,
                "preflight_status": (
                    gemini_snapshot.status if gemini_snapshot is not None else "not_configured"
                ),
            },
        },
        "voice_function_calling_configured": bool(
            volc
            and container.settings.volc_function_callback_url
            and container.settings.volc_ark_model
            and container.settings.volc_ark_endpoint_id
        ),
        "voice_configuration": voice_configuration,
        "core_market_data_client_configured": bool(
            real and container.settings.core_base_url and container.settings.core_actor_token
        ),
        "core_credential": credential.public_dict(),
        "llm_model": llm_snapshot.public_dict() if llm_snapshot is not None else None,
        "voice_e2e_verified": False,
    }
    return JSONResponse(status_code=200 if ready else 503, content=content)
