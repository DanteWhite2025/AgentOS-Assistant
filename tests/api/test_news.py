"""GET /v1/agent/news 接口测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_container
from app.api.news import router
from app.news.models import NewsItem
from app.news.store import InMemoryNewsStore

_NOW = datetime.now(UTC)


class _Settings:
    news_default_time_range_days = 30
    news_query_rate_limit = 60
    news_query_rate_window_seconds = 60


class _Ctn:
    def __init__(self, store, rate_limiter=None) -> None:
        self.settings = _Settings()
        self.news_store = store
        self.rate_limiter = rate_limiter


def _item(news_id: str, *, minutes: int = 0, title: str = "t", **kwargs) -> NewsItem:
    defaults = {
        "news_id": news_id,
        "dedup_key": title.casefold(),
        "source": "media",
        "title": title,
        "content": "正文",
        "published_at": _NOW - timedelta(minutes=minutes),
    }
    return NewsItem(**{**defaults, **kwargs})


@pytest.fixture
async def client_and_store():
    store = InMemoryNewsStore()
    app = FastAPI()
    app.include_router(router, prefix="/v1/agent")
    app.dependency_overrides[get_container] = lambda: _Ctn(store)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, store


async def test_returns_items_newest_first(client_and_store) -> None:
    client, store = client_and_store
    await store.upsert_many([
        _item("a", minutes=30, title="a"), _item("b", minutes=1, title="b")
    ])
    body = (await client.get("/v1/agent/news")).json()
    assert [item["news_id"] for item in body["items"]] == ["b", "a"]
    assert body["next_cursor"] is None


async def test_response_never_leaks_raw(client_and_store) -> None:
    client, store = client_and_store
    await store.upsert_many([_item("a", raw={"secret": "leak", "content": "<b>x</b>"})])
    body = (await client.get("/v1/agent/news")).json()
    assert "raw" not in body["items"][0]
    assert "leak" not in str(body)


async def test_filters_by_tag(client_and_store) -> None:
    client, store = client_and_store
    await store.upsert_many([
        _item("a", title="a", tags=("crypto",)), _item("b", title="b", tags=("macro",))
    ])
    body = (await client.get("/v1/agent/news?tag=macro")).json()
    assert [item["news_id"] for item in body["items"]] == ["b"]


async def test_unknown_tag_returns_400(client_and_store) -> None:
    client, _ = client_and_store
    resp = await client.get("/v1/agent/news?tag=nonsense")
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_REQUEST"


async def test_keyword_search(client_and_store) -> None:
    client, store = client_and_store
    await store.upsert_many([
        _item("a", title="Bitcoin ETF"), _item("b", title="英伟达芯片")
    ])
    body = (await client.get("/v1/agent/news?q=芯片")).json()
    assert [item["news_id"] for item in body["items"]] == ["b"]


async def test_default_time_window_excludes_ancient_rows(client_and_store) -> None:
    client, store = client_and_store
    await store.upsert_many([
        _item("ancient", minutes=60 * 24 * 400, title="ancient"),
        _item("fresh", minutes=5, title="fresh"),
    ])
    body = (await client.get("/v1/agent/news")).json()
    assert [item["news_id"] for item in body["items"]] == ["fresh"]


async def test_cursor_pagination(client_and_store) -> None:
    client, store = client_and_store
    await store.upsert_many([_item(f"n{i}", minutes=i, title=f"t{i}") for i in range(5)])
    first = (await client.get("/v1/agent/news?limit=2")).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = (
        await client.get(f"/v1/agent/news?limit=2&cursor={first['next_cursor']}")
    ).json()
    ids = [item["news_id"] for item in (*first["items"], *second["items"])]
    assert ids == ["n0", "n1", "n2", "n3"]


async def test_bad_cursor_returns_400(client_and_store) -> None:
    client, _ = client_and_store
    resp = await client.get("/v1/agent/news?cursor=!!!not-base64!!!")
    assert resp.status_code == 400
    assert resp.json()["code"] == "INVALID_REQUEST"


async def test_limit_out_of_range_returns_422(client_and_store) -> None:
    client, _ = client_and_store
    assert (await client.get("/v1/agent/news?limit=999")).status_code == 422
    assert (await client.get("/v1/agent/news?limit=0")).status_code == 422


async def test_returns_503_when_store_unconfigured() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/v1/agent")
    app.dependency_overrides[get_container] = lambda: _Ctn(None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/agent/news")
    assert resp.status_code == 503


async def test_rate_limited_returns_429() -> None:
    class _AlwaysDeny:
        async def allow(self, key: str, limit: int, window_seconds: int) -> bool:
            return False

    app = FastAPI()
    app.include_router(router, prefix="/v1/agent")
    app.dependency_overrides[get_container] = lambda: _Ctn(
        InMemoryNewsStore(), _AlwaysDeny()
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/agent/news")
    assert resp.status_code == 429
    assert resp.json()["code"] == "RATE_LIMITED"


async def test_caller_key_ignores_forgeable_user_id_header() -> None:
    """限流身份不应受可伪造的 X-User-Id 影响：换个 header 值不能换到新配额桶。

    本端点无鉴权，X-User-Id 由调用方任意指定；若被用于限流键，攻击者只需
    每次换一个值即可绕开配额（尤其是 q= 命中 pg_trgm 退化场景的顺序扫描）。
    """
    seen_keys: list[str] = []

    class _RecordingLimiter:
        async def allow(self, key: str, limit: int, window_seconds: int) -> bool:
            seen_keys.append(key)
            return True

    app = FastAPI()
    app.include_router(router, prefix="/v1/agent")
    app.dependency_overrides[get_container] = lambda: _Ctn(
        InMemoryNewsStore(), _RecordingLimiter()
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/v1/agent/news", headers={"X-User-Id": "attacker-1"})
        await client.get("/v1/agent/news", headers={"X-User-Id": "attacker-2"})

    assert len(seen_keys) == 2
    assert seen_keys[0] == seen_keys[1]
    assert "attacker-1" not in seen_keys[0]
    assert "attacker-2" not in seen_keys[1]


async def test_caller_key_unaffected_by_missing_user_id_header() -> None:
    """未带 X-User-Id 时行为不变：仍按对端 IP 计。"""
    seen_keys: list[str] = []

    class _RecordingLimiter:
        async def allow(self, key: str, limit: int, window_seconds: int) -> bool:
            seen_keys.append(key)
            return True

    app = FastAPI()
    app.include_router(router, prefix="/v1/agent")
    app.dependency_overrides[get_container] = lambda: _Ctn(
        InMemoryNewsStore(), _RecordingLimiter()
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get("/v1/agent/news")
        await client.get("/v1/agent/news", headers={"X-User-Id": "someone"})

    assert len(seen_keys) == 2
    assert seen_keys[0] == seen_keys[1]
    assert seen_keys[0].startswith("news:query:ip:")


# ---------------------------------------------------------------------------
# lang 参数 = 标题展示语言（不是数据过滤条件）
# ---------------------------------------------------------------------------


def _bilingual(news_id: str = "zh1", **kwargs) -> NewsItem:
    """已翻译过的条目：title 中文，title_en 上游英文原文。"""
    return _item(
        news_id,
        title="比特币突破 71500",
        title_en="BTC breaks 71500",
        content="发生前后的中文摘要",
        **kwargs,
    )


async def test_default_response_uses_chinese_title(client_and_store) -> None:
    """前端不传参数就该拿到中文，无需改版发包。"""
    client, store = client_and_store
    await store.upsert_many([_bilingual()])

    resp = await client.get("/v1/agent/news")

    assert resp.status_code == 200
    assert resp.json()["items"][0]["title"] == "比特币突破 71500"


@pytest.mark.parametrize("lang", ["cn", "zh-cn", "zh", "ZH-CN", ""])
async def test_chinese_aliases_all_return_chinese_title(client_and_store, lang) -> None:
    client, store = client_and_store
    await store.upsert_many([_bilingual()])

    resp = await client.get("/v1/agent/news", params={"lang": lang})

    assert resp.json()["items"][0]["title"] == "比特币突破 71500"


@pytest.mark.parametrize("lang", ["en", "EN", "en-US"])
async def test_english_lang_returns_original_english_title(client_and_store, lang) -> None:
    client, store = client_and_store
    await store.upsert_many([_bilingual()])

    resp = await client.get("/v1/agent/news", params={"lang": lang})

    assert resp.json()["items"][0]["title"] == "BTC breaks 71500"


async def test_lang_does_not_filter_out_items(client_and_store) -> None:
    """回归：lang 曾是 `WHERE lang = ?` 过滤条件，而 trending 条目的 lang 字段
    恒为空 —— 推荐 tab 传 lang=zh-cn 会返回 0 条（staging 实测）。现在
    lang 只选标题语言，一条都不该少。"""
    client, store = client_and_store
    await store.upsert_many([
        _bilingual("a", lang=""),          # trending：上游不给 source_lang
        _item("b", title="Fed holds", lang="en"),
    ])

    for lang in ("", "zh-cn", "en"):
        resp = await client.get("/v1/agent/news", params={"lang": lang})
        assert len(resp.json()["items"]) == 2, f"lang={lang!r} 不该过滤掉任何条目"


async def test_untranslated_item_keeps_its_title_in_both_languages(client_and_store) -> None:
    """英文源直出的条目没有 title_en，两种语言下都返回它自己的标题。"""
    client, store = client_and_store
    await store.upsert_many([_item("x", title="Fed holds rates steady")])

    for lang in ("", "en"):
        resp = await client.get("/v1/agent/news", params={"lang": lang})
        assert resp.json()["items"][0]["title"] == "Fed holds rates steady"
