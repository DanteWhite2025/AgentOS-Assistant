"""GET /v1/agent/hotspots/daily 接口测试。"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.agents.llm import LLMResult
from app.api.deps import get_container
from app.api.hotspots import router
from app.news.store import InMemoryNewsStore
from app.services.hotspot_service import HotspotService
from app.services.rate_limit import InMemoryRateLimiter


class _StubLLM:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, system, messages, tools):
        return LLMResult(text=self._text)


class _Settings:
    hotspots_rate_limit = 60
    hotspots_rate_window_seconds = 60


class _Ctn:
    def __init__(self, *, hotspot_service=None, rate_limiter=None, settings=None) -> None:
        self.settings = settings or _Settings()
        self.hotspot_service = hotspot_service
        self.rate_limiter = rate_limiter


@pytest.fixture
def app_with():
    def _build(hotspot_service, *, rate_limiter=None, settings=None):
        app = FastAPI()
        app.include_router(router, prefix="/v1/agent")
        ctn = _Ctn(hotspot_service=hotspot_service, rate_limiter=rate_limiter, settings=settings)
        app.dependency_overrides[get_container] = lambda: ctn
        return app

    return _build


def _service(llm_text: str) -> HotspotService:
    return HotspotService(news_store=InMemoryNewsStore(), llm=_StubLLM(llm_text))


async def test_returns_digest_structure(app_with) -> None:
    service = _service(
        json.dumps(
            {
                "headline": "今日核心行情速递",
                "summary": "1. 美联储维持利率不变",
                "suggested_actions": [
                    {"label": "🔥 深度分析英伟达财报", "prompt": "结合最新财报深度分析英伟达后续走势"}
                ],
            }
        )
    )
    app = app_with(service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/agent/hotspots/daily")

    assert resp.status_code == 200
    body = resp.json()
    assert body["headline"] == "今日核心行情速递"
    assert body["suggested_actions"][0]["label"] == "🔥 深度分析英伟达财报"
    assert resp.headers["Cache-Control"] == "no-store"


async def test_returns_a_six_to_nine_item_suggested_action_pool(app_with) -> None:
    """议题池扩容到 6~9 条，接口层不做数量截断，原样透传给前端分页切组。"""
    actions = [
        {"label": f"🔥 议题{i}", "prompt": f"深入分析议题{i}的后续走势"} for i in range(1, 8)
    ]
    service = _service(
        json.dumps(
            {"headline": "今日核心行情速递", "summary": "1. 美联储维持利率不变", "suggested_actions": actions},
            ensure_ascii=False,
        )
    )
    app = app_with(service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/agent/hotspots/daily")

    assert resp.status_code == 200
    assert len(resp.json()["suggested_actions"]) == 7


async def test_returns_503_when_hotspot_service_unavailable(app_with) -> None:
    app = app_with(None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/agent/hotspots/daily")

    assert resp.status_code == 503


async def test_returns_error_envelope_when_digest_generation_fails(app_with) -> None:
    """LLM 调用本身抛异常（上游挂了/鉴权失效）时也要走统一错误信封，

    不能让裸异常冒泡成 FastAPI 默认的非结构化 500。
    """

    class _FailingLLM:
        async def complete(self, system, messages, tools):
            raise RuntimeError("upstream LLM gateway unavailable")

    service = HotspotService(news_store=InMemoryNewsStore(), llm=_FailingLLM())
    app = app_with(service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/agent/hotspots/daily")

    assert resp.status_code == 503
    assert resp.json()["code"] == "INTERNAL"


async def test_cache_hits_do_not_consume_rate_limit(app_with) -> None:
    """早报全国一份。缓存命中若也占 IP 配额，nginx 后面的 127.0.0.1 会把首页打成 429。"""
    service = _service(json.dumps({"headline": "h", "summary": "s", "suggested_actions": []}))
    limiter = InMemoryRateLimiter()
    custom_settings = _Settings()
    custom_settings.hotspots_rate_limit = 1
    custom_settings.hotspots_rate_window_seconds = 3600
    app = app_with(service, rate_limiter=limiter, settings=custom_settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/v1/agent/hotspots/daily")
        second = await client.get("/v1/agent/hotspots/daily")
        third = await client.get("/v1/agent/hotspots/daily")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 200


async def test_returns_429_on_cold_start_when_rate_limited(app_with) -> None:
    """冷启动没有缓存时，限流仍然挡住打 LLM 的 miss。"""

    class _FailingLLM:
        async def complete(self, system, messages, tools):
            raise RuntimeError("insufficient_user_quota")

    service = HotspotService(news_store=InMemoryNewsStore(), llm=_FailingLLM())
    limiter = InMemoryRateLimiter()
    custom_settings = _Settings()
    custom_settings.hotspots_rate_limit = 1
    custom_settings.hotspots_rate_window_seconds = 3600
    app = app_with(service, rate_limiter=limiter, settings=custom_settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/v1/agent/hotspots/daily")
        blocked = await client.get("/v1/agent/hotspots/daily")

    assert first.status_code == 503
    assert blocked.status_code == 429


async def test_existing_cache_skips_rate_limit_after_ttl_expires(app_with) -> None:
    """成功过一次之后，TTL 过期也不再挤限流桶，由单飞刷新。"""
    clock = [0.0]
    service = HotspotService(
        news_store=InMemoryNewsStore(),
        llm=_StubLLM(json.dumps({"headline": "今日速递", "summary": "x", "suggested_actions": []})),
        cache_ttl_seconds=10,
        clock=lambda: clock[0],
    )
    limiter = InMemoryRateLimiter()
    custom_settings = _Settings()
    custom_settings.hotspots_rate_limit = 1
    custom_settings.hotspots_rate_window_seconds = 3600
    app = app_with(service, rate_limiter=limiter, settings=custom_settings)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/v1/agent/hotspots/daily")
        clock[0] = 11.0
        second = await client.get("/v1/agent/hotspots/daily")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["headline"] == "今日速递"


async def test_returns_stale_digest_when_refresh_hits_quota(app_with) -> None:
    """成功过一次之后，TTL 过期碰上 LLM 额度不够仍应 200，而不是把首页打成 503。"""

    class _FlakyLLM:
        def __init__(self) -> None:
            self.call_count = 0

        async def complete(self, system, messages, tools):
            self.call_count += 1
            if self.call_count == 1:
                return LLMResult(
                    text=json.dumps({"headline": "今日速递", "summary": "x", "suggested_actions": []})
                )
            raise RuntimeError("insufficient_user_quota")

    clock = [0.0]
    service = HotspotService(
        news_store=InMemoryNewsStore(),
        llm=_FlakyLLM(),
        cache_ttl_seconds=10,
        error_cooldown_seconds=60,
        clock=lambda: clock[0],
    )
    app = app_with(service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get("/v1/agent/hotspots/daily")
        clock[0] = 11.0
        second = await client.get("/v1/agent/hotspots/daily")

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["headline"] == "今日速递"
