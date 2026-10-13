"""每日热点早报服务单测。

摘要生成走内部统一 LLM 网关（不调用投研分析工具——ask_investment_expert 只在用户深度追问时
以工具形式被调用），故这里只需 mock NewsStore 与 LLMProvider。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.agents.llm import LLMResult
from app.news.models import NewsItem
from app.news.store import InMemoryNewsStore
from app.services.hotspot_service import HotspotService, HotspotUnavailable


def _news_item(news_id: str, title: str) -> NewsItem:
    return NewsItem(
        news_id=news_id,
        dedup_key=title,
        source="media",
        title=title,
        content=f"{title} 正文内容",
        published_at=datetime.now(UTC),
    )


class _StubLLM:
    def __init__(self, text: str) -> None:
        self._text = text
        self.received_messages: tuple | None = None
        self.call_count = 0

    async def complete(self, system, messages, tools):
        self.call_count += 1
        self.received_messages = messages
        return LLMResult(text=self._text)


async def test_build_daily_digest_parses_structured_response() -> None:
    store = InMemoryNewsStore()
    await store.upsert_many([_news_item("1", "美联储议息决定"), _news_item("2", "BTC 突破 7 万美元")])

    llm_output = {
        "headline": "今日核心行情速递",
        "summary": "1. 美联储维持利率不变\n2. BTC 突破关键阻力位",
        "suggested_actions": [
            {"label": "🔥 深度分析英伟达财报", "prompt": "结合最新财报深度分析英伟达后续走势"},
            {"label": "📈 BTC 突破阻力位了吗？", "prompt": "结合当前宏观流动性分析大饼走势"},
        ],
    }
    llm = _StubLLM(json.dumps(llm_output, ensure_ascii=False))
    service = HotspotService(news_store=store, llm=llm)

    digest = await service.build_daily_digest()

    assert digest.headline == "今日核心行情速递"
    assert len(digest.suggested_actions) == 2
    assert digest.suggested_actions[0].label == "🔥 深度分析英伟达财报"
    # 新闻内容确实被喂给了 LLM
    assert "美联储议息决定" in llm.received_messages[0].content


async def test_build_daily_digest_falls_back_when_llm_output_is_not_json() -> None:
    store = InMemoryNewsStore()
    await store.upsert_many([_news_item("1", "美联储议息决定")])
    llm = _StubLLM("这不是合法 JSON")
    service = HotspotService(news_store=store, llm=llm)

    digest = await service.build_daily_digest()

    assert digest.headline
    assert digest.suggested_actions == []


async def test_build_daily_digest_falls_back_when_llm_output_has_invalid_schema() -> None:
    """JSON 语法合法但字段形状不对（如 suggested_actions 项缺 prompt）时也必须降级，

    不能让 pydantic.ValidationError 原样冒泡把 GET /v1/agent/hotspots/daily 拖成裸 500。
    """
    store = InMemoryNewsStore()
    await store.upsert_many([_news_item("1", "美联储议息决定")])
    llm = _StubLLM(
        json.dumps({"headline": "今日速递", "summary": "x", "suggested_actions": [{"label": "foo"}]})
    )
    service = HotspotService(news_store=store, llm=llm)

    digest = await service.build_daily_digest()

    assert digest.headline
    assert digest.suggested_actions == []


async def test_build_daily_digest_handles_no_recent_news() -> None:
    store = InMemoryNewsStore()
    llm = _StubLLM(json.dumps({"headline": "暂无热点", "summary": "", "suggested_actions": []}))
    service = HotspotService(news_store=store, llm=llm)

    digest = await service.build_daily_digest()

    assert digest.headline == "暂无热点"


# ---- TTL 缓存：公开端点无鉴权，每次都触发付费 LLM 调用是明显的成本放大点 ----


async def test_build_daily_digest_uses_cache_within_ttl() -> None:
    store = InMemoryNewsStore()
    llm = _StubLLM(json.dumps({"headline": "今日速递", "summary": "x", "suggested_actions": []}))
    clock = [0.0]
    service = HotspotService(news_store=store, llm=llm, cache_ttl_seconds=300, clock=lambda: clock[0])

    first = await service.build_daily_digest()
    clock[0] = 100.0  # 仍在 300s TTL 内
    second = await service.build_daily_digest()

    assert llm.call_count == 1
    assert first == second


async def test_peek_cached_keeps_stale_after_ttl() -> None:
    store = InMemoryNewsStore()
    llm = _StubLLM(json.dumps({"headline": "今日速递", "summary": "x", "suggested_actions": []}))
    clock = [0.0]
    service = HotspotService(news_store=store, llm=llm, cache_ttl_seconds=300, clock=lambda: clock[0])

    assert service.peek_cached() is None
    digest = await service.build_daily_digest()
    assert service.peek_cached() == digest

    clock[0] = 301.0
    assert service.peek_cached() == digest


async def test_build_daily_digest_refetches_after_ttl_expires() -> None:
    store = InMemoryNewsStore()
    llm = _StubLLM(json.dumps({"headline": "今日速递", "summary": "x", "suggested_actions": []}))
    clock = [0.0]
    service = HotspotService(news_store=store, llm=llm, cache_ttl_seconds=300, clock=lambda: clock[0])

    await service.build_daily_digest()
    clock[0] = 301.0  # 超过 300s TTL
    await service.build_daily_digest()

    assert llm.call_count == 2


class _FlakyLLM:
    def __init__(self, text: str) -> None:
        self._text = text
        self.call_count = 0

    async def complete(self, system, messages, tools):
        self.call_count += 1
        if self.call_count == 1:
            return LLMResult(text=self._text)
        raise RuntimeError("insufficient_user_quota")


class _SlowLLM:
    def __init__(self, text: str) -> None:
        self._text = text
        self.call_count = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, system, messages, tools):
        self.call_count += 1
        self.started.set()
        await self.release.wait()
        return LLMResult(text=self._text)


async def test_build_daily_digest_serves_stale_when_refresh_fails() -> None:
    """TTL 过期后 LLM 额度不够，必须继续返回上一份成功结果，不能把每个首页请求打成 503。"""
    store = InMemoryNewsStore()
    llm = _FlakyLLM(json.dumps({"headline": "今日速递", "summary": "x", "suggested_actions": []}))
    clock = [0.0]
    service = HotspotService(
        news_store=store,
        llm=llm,
        cache_ttl_seconds=300,
        error_cooldown_seconds=60,
        clock=lambda: clock[0],
    )

    first = await service.build_daily_digest()
    clock[0] = 301.0
    second = await service.build_daily_digest()
    third = await service.build_daily_digest()

    assert first.headline == "今日速递"
    assert second == first
    assert third == first
    assert llm.call_count == 2  # 成功一次 + 过期后失败一次，冷却期内不再打


async def test_build_daily_digest_cools_down_after_cold_start_failure() -> None:
    store = InMemoryNewsStore()

    class _FailingLLM:
        def __init__(self) -> None:
            self.call_count = 0

        async def complete(self, system, messages, tools):
            self.call_count += 1
            raise RuntimeError("insufficient_user_quota")

    clock = [0.0]
    llm = _FailingLLM()
    service = HotspotService(
        news_store=store,
        llm=llm,
        cache_ttl_seconds=300,
        error_cooldown_seconds=60,
        clock=lambda: clock[0],
    )

    with pytest.raises(HotspotUnavailable):
        await service.build_daily_digest()
    with pytest.raises(HotspotUnavailable):
        await service.build_daily_digest()
    assert llm.call_count == 1

    clock[0] = 61.0
    with pytest.raises(HotspotUnavailable):
        await service.build_daily_digest()
    assert llm.call_count == 2


async def test_build_daily_digest_single_flight_coalesces_concurrent_misses() -> None:
    store = InMemoryNewsStore()
    llm = _SlowLLM(json.dumps({"headline": "今日速递", "summary": "x", "suggested_actions": []}))
    service = HotspotService(news_store=store, llm=llm, cache_ttl_seconds=300)

    first_task = asyncio.create_task(service.build_daily_digest())
    await llm.started.wait()
    second_task = asyncio.create_task(service.build_daily_digest())
    await asyncio.sleep(0)
    llm.release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert llm.call_count == 1
    assert first == second


def test_hotspot_prompts_must_be_self_contained() -> None:
    """早报的 suggested_actions.prompt 点开就是全新一轮的第一句话。

    和答案末尾的追问 chips 是同一类东西（见 app/container.py 的 _FOLLOWUP_GUIDE）：
    文本回合无状态，代词到了后端没有指代对象，模型会自己补一个。早报这里更没有
    上文可依——它是会话的第一句，「该项目」「这只币」连本轮都没有。
    """
    from app.services.hotspot_service import _SYSTEM_PROMPT

    assert "代词" in _SYSTEM_PROMPT
    assert "全名" in _SYSTEM_PROMPT or "完整主语" in _SYSTEM_PROMPT


def test_hotspot_prompt_asks_for_a_six_to_nine_item_pool() -> None:
    """议题池要够前端首页「换一换」轮换 + 二级列表页按 3 个一组分页切组，2~3 条不够用。"""
    from app.services.hotspot_service import _SYSTEM_PROMPT

    assert "6~9" in _SYSTEM_PROMPT


async def test_build_daily_digest_parses_a_six_to_nine_item_pool() -> None:
    store = InMemoryNewsStore()
    await store.upsert_many([_news_item("1", "美联储议息决定")])

    actions = [
        {"label": f"🔥 议题{i}", "prompt": f"深入分析议题{i}的后续走势"} for i in range(1, 8)
    ]
    llm_output = {"headline": "今日核心行情速递", "summary": "1. 美联储维持利率不变", "suggested_actions": actions}
    llm = _StubLLM(json.dumps(llm_output, ensure_ascii=False))
    service = HotspotService(news_store=store, llm=llm)

    digest = await service.build_daily_digest()

    assert len(digest.suggested_actions) == 7
