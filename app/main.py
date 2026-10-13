"""FastAPI 应用入口。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.agent import router as agent_router
from app.api.errors import error_response
from app.api.health import router as health_router
from app.api.hotspots import router as hotspots_router
from app.api.news import router as news_router
from app.api.strategy import router as strategy_router
from app.api.usage import router as usage_router
from app.api.voice import router as voice_gateway_router
from app.api.ws import router as ws_router
from app.auth.core_user import UserAuthenticationError
from app.config import get_settings
from app.container import build_container
from app.telemetry.logging import setup_logging
from app.telemetry.otel import setup_otel

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging()
    container = build_container(settings)
    await container.startup()
    app.state.container = container
    logger.info(
        "started %s env=%s provider_mode=%s", settings.app_name, settings.env, settings.provider_mode
    )
    try:
        yield
    finally:
        await container.aclose()
        logger.info("shutting down %s", settings.app_name)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    # ponytail: 开放 CORS 支持，方便 Vercel / 外部 Web 看板跨域调 API；由于不基于 Cookie 鉴权，使能 allow_credentials=False 消除安全隐患
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(UserAuthenticationError)
    async def user_authentication_error_handler(
        _request: Request, exc: UserAuthenticationError
    ):
        return error_response(exc.code, exc.message, exc.status_code)

    app.include_router(health_router)
    app.include_router(agent_router, prefix="/v1/agent")
    app.include_router(strategy_router, prefix="/v1/agent")
    app.include_router(news_router, prefix="/v1/agent")
    app.include_router(hotspots_router, prefix="/v1/agent")
    # WebRTC 实时语音网关控制面：数据面直连大模型，控制面（风控下单 / 结账）过这里。
    app.include_router(voice_gateway_router, prefix="/api/voice")
    app.include_router(ws_router)
    # 内部用量与成本核算查询。只对内，不在对外 API 前缀下。
    app.include_router(usage_router, prefix="/internal/usage")
    setup_otel(app, settings)  # otel_enabled=False 时 no-op
    return app


app = create_app()
