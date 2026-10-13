"""存储层测试。PG 实现走 sqlite+aiosqlite（照 tests/test_stores.py 的范式）。

dedup_key 合并是本层最容易写错的地方：trending route 无 source_url，
news_id 只能用内容摘要，同一条新闻会与 media route 的行撞出两个 id。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.news.models import NewsItem, NewsQuery
from app.news.store import decode_cursor, encode_cursor

_BASE = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _item(news_id: str, *, minutes: int = 0, title: str = "t", **kwargs) -> NewsItem:
    defaults = {
        "news_id": news_id,
        "dedup_key": title.casefold(),
        "source": "media",
        "title": title,
        "content": "正文",
        "published_at": _BASE + timedelta(minutes=minutes),
    }
    return NewsItem(**{**defaults, **kwargs})


async def _pg_store():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.news.pg_store import PostgresNewsStore

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    store = PostgresNewsStore(engine)
    await store.create_schema()
    return store


@pytest.fixture(params=["memory", "pg"])
async def store(request):
    if request.param == "memory":
        from app.news.store import InMemoryNewsStore

        return InMemoryNewsStore()
    return await _pg_store()


def test_cursor_round_trips() -> None:
    encoded = encode_cursor(_BASE, "abc")
    when, news_id = decode_cursor(encoded)
    assert when == _BASE and news_id == "abc"


def test_decode_cursor_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        decode_cursor("not-a-cursor")


async def test_upsert_then_query_returns_item(store) -> None:
    assert await store.upsert_many([_item("a", tags=("crypto",))]) == 1
    items, cursor = await store.query(NewsQuery(limit=10))
    assert [item.news_id for item in items] == ["a"]
    assert cursor is None


async def test_same_news_id_is_not_duplicated(store) -> None:
    await store.upsert_many([_item("a")])
    await store.upsert_many([_item("a")])
    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 1


async def test_dedup_key_merges_tags_instead_of_inserting_row(store) -> None:
    # media route 先入库（有 url，id=media-1），trending route 后到（无 url，id=trend-1）
    await store.upsert_many([_item("media-1", title="BTC breaks", tags=("crypto",))])
    await store.upsert_many([_item("trend-1", title="BTC breaks", tags=("recommend",))])
    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 1, "同 dedup_key 不该新建行"
    assert set(items[0].tags) == {"crypto", "recommend"}
    assert items[0].news_id == "media-1", "应合并到先入库的那行"


async def test_dedup_key_merges_within_24h_window(store) -> None:
    # 相差 1 小时，应视为同一事件，合并成 1 行
    await store.upsert_many([_item("a", minutes=0, title="美联储议息决定", tags=("macro",))])
    await store.upsert_many(
        [_item("b", minutes=60, title="美联储议息决定", tags=("recommend",))]
    )
    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 1, "24h 窗口内的同 dedup_key 应合并"
    assert set(items[0].tags) == {"macro", "recommend"}


async def test_dedup_key_outside_24h_window_keeps_rows_separate(store) -> None:
    # 相差 30 天，标题复用（如「美联储议息决定」）不该被误判为同一事件
    await store.upsert_many([_item("a", minutes=0, title="美联储议息决定", tags=("macro",))])
    await store.upsert_many(
        [_item("b", minutes=30 * 24 * 60, title="美联储议息决定", tags=("recommend",))]
    )
    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 2, "超出 24h 窗口不该合并"
    tags_by_id = {item.news_id: set(item.tags) for item in items}
    assert tags_by_id == {"a": {"macro"}, "b": {"recommend"}}


async def test_dedup_key_merges_just_under_24h_boundary(store) -> None:
    # 相差 23h59m，仍在窗口内，应合并
    await store.upsert_many([_item("a", minutes=0, title="边界事件", tags=("macro",))])
    await store.upsert_many(
        [_item("b", minutes=23 * 60 + 59, title="边界事件", tags=("recommend",))]
    )
    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 1, "23h59m 仍在 24h 窗口内，应合并"
    assert set(items[0].tags) == {"macro", "recommend"}


async def test_dedup_key_does_not_merge_just_over_24h_boundary(store) -> None:
    # 相差 24h01m，超出窗口，不应合并
    await store.upsert_many([_item("a", minutes=0, title="边界事件2", tags=("macro",))])
    await store.upsert_many(
        [_item("b", minutes=24 * 60 + 1, title="边界事件2", tags=("recommend",))]
    )
    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 2, "24h01m 已超出窗口，不该合并"


async def test_dedup_key_window_check_is_symmetric_for_insert_order(store) -> None:
    # 先插较新的一条，再插 30 天前的一条，仍应保持 2 行独立
    await store.upsert_many(
        [_item("new", minutes=30 * 24 * 60, title="美联储议息决定2", tags=("macro",))]
    )
    await store.upsert_many([_item("old", minutes=0, title="美联储议息决定2", tags=("recommend",))])
    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 2, "反向插入顺序下超出窗口仍不该合并"
    tags_by_id = {item.news_id: set(item.tags) for item in items}
    assert tags_by_id == {"new": {"macro"}, "old": {"recommend"}}


async def test_query_filters_by_tag(store) -> None:
    await store.upsert_many([
        _item("a", title="a", tags=("crypto",)),
        _item("b", title="b", tags=("macro",)),
    ])
    items, _ = await store.query(NewsQuery(tag="macro", limit=10))
    assert [item.news_id for item in items] == ["b"]


async def test_query_filters_by_keyword_case_insensitively(store) -> None:
    await store.upsert_many([
        _item("a", title="Bitcoin ETF inflow"),
        _item("b", title="英伟达发布新芯片"),
    ])
    items, _ = await store.query(NewsQuery(q="bitcoin", limit=10))
    assert [item.news_id for item in items] == ["a"]
    items, _ = await store.query(NewsQuery(q="芯片", limit=10))
    assert [item.news_id for item in items] == ["b"]


async def test_keyword_wildcards_are_escaped_not_interpreted(store) -> None:
    await store.upsert_many([_item("a", title="plain title")])
    # "%" 若未转义会变成通配符、把所有行都捞出来
    items, _ = await store.query(NewsQuery(q="%", limit=10))
    assert items == []


async def test_lang_is_no_longer_a_query_filter(store) -> None:
    """lang 已从「数据过滤条件」改为「标题展示语言」（在 API 投影层处理）。

    改动的由来：trending route 不返回 source_lang，那批条目的 lang 恒为空，
    旧的 `WHERE lang = ?` 让推荐 tab 传 lang=zh-cn 时返回 0 条（staging 实测）。
    存储层不该再认识这个字段。
    """
    await store.upsert_many([
        _item("a", title="a", lang="zh-cn"),
        _item("b", title="b", lang="en"),
        _item("c", title="c", lang=""),
    ])
    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 3
    assert not hasattr(NewsQuery(), "lang"), "NewsQuery 不该再有 lang 字段"


async def test_query_respects_since_window(store) -> None:
    await store.upsert_many([
        _item("old", minutes=0, title="old"),
        _item("new", minutes=60, title="new"),
    ])
    items, _ = await store.query(NewsQuery(since=_BASE + timedelta(minutes=30), limit=10))
    assert [item.news_id for item in items] == ["new"]


async def test_query_orders_newest_first(store) -> None:
    await store.upsert_many([
        _item("a", minutes=0, title="a"),
        _item("b", minutes=10, title="b"),
        _item("c", minutes=20, title="c"),
    ])
    items, _ = await store.query(NewsQuery(limit=10))
    assert [item.news_id for item in items] == ["c", "b", "a"]


async def test_cursor_pagination_has_no_gap_or_overlap(store) -> None:
    await store.upsert_many([_item(f"n{i}", minutes=i, title=f"t{i}") for i in range(5)])
    first, cursor = await store.query(NewsQuery(limit=2))
    assert cursor is not None
    second, cursor2 = await store.query(NewsQuery(limit=2, cursor=cursor))
    third, cursor3 = await store.query(NewsQuery(limit=2, cursor=cursor2))
    seen = [item.news_id for item in (*first, *second, *third)]
    assert seen == ["n4", "n3", "n2", "n1", "n0"]
    assert cursor3 is None


async def test_purge_before_removes_old_rows_and_their_tags(store) -> None:
    await store.upsert_many([
        _item("old", minutes=0, title="old", tags=("crypto",)),
        _item("new", minutes=60, title="new", tags=("macro",)),
    ])
    assert await store.purge_before(_BASE + timedelta(minutes=30)) == 1
    items, _ = await store.query(NewsQuery(limit=10))
    assert [item.news_id for item in items] == ["new"]
    # 标签过滤不该还能捞到已删的行（关联表须一并清掉）
    stale, _ = await store.query(NewsQuery(tag="crypto", limit=10))
    assert stale == []


# 修复 7：purge_before 先把所有过期 id 读进内存再 IN (...)，行数一旦超出
# PG/sqlite 单条语句的绑定参数上限（PG 65535）就会硬失败，被 collector.py 的
# except 吞成「清理失败，等待下一轮」——保留期从此静默失效，表无界增长。
async def test_purge_before_handles_batches_beyond_sql_variable_limit() -> None:
    """40k 行远超参数上限；直接批量 insert 绕开 upsert_many 逐行重建字典的慢路径，
    只验证 purge_before 本身的 SQL 形态。"""
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.news.pg_store import PostgresNewsStore, news_table, news_tags_table

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    store = PostgresNewsStore(engine)
    await store.create_schema()

    now = _BASE
    total = 40_000
    old_rows = [
        {
            "news_id": f"old-{i}", "dedup_key": f"old-{i}", "source": "media",
            "source_name": "", "source_url": "", "source_quality": "",
            "title": f"old {i}", "content": "c", "lang": "", "asset_type": "",
            "upstream_category": "", "published_at": now - timedelta(days=2),
            "raw": {}, "created_at": now,
        }
        for i in range(total)
    ]
    new_row = {
        "news_id": "keep", "dedup_key": "keep", "source": "media",
        "source_name": "", "source_url": "", "source_quality": "",
        "title": "keep", "content": "c", "lang": "", "asset_type": "",
        "upstream_category": "", "published_at": now, "raw": {}, "created_at": now,
    }
    async with engine.begin() as conn:
        await conn.execute(news_table.insert(), old_rows)
        await conn.execute(news_table.insert(), [new_row])
        await conn.execute(
            news_tags_table.insert(),
            [{"news_id": f"old-{i}", "tag": "crypto"} for i in range(total)],
        )

    removed = await store.purge_before(now - timedelta(days=1))
    assert removed == total

    items, _ = await store.query(NewsQuery(limit=10))
    assert [item.news_id for item in items] == ["keep"]

    async with engine.begin() as conn:
        orphan_count = await conn.execute(
            sa.select(sa.func.count()).select_from(news_tags_table)
        )
        assert orphan_count.scalar_one() == 0


# ---------------------------------------------------------------------------
# translated_ids：翻译前的成本闸门（已有译文的不再送 LLM）
# ---------------------------------------------------------------------------


async def test_translated_ids_returns_only_rows_with_english_original(store) -> None:
    await store.upsert_many([
        _item("done", title="比特币突破", title_en="BTC breaks"),
        _item("pending", title="BTC breaks 2", title_en=""),
    ])
    assert await store.translated_ids(["done", "pending", "missing"]) == {"done"}


async def test_translated_ids_on_empty_input(store) -> None:
    assert await store.translated_ids([]) == set()


async def test_translated_ids_on_empty_store(store) -> None:
    assert await store.translated_ids(["a"]) == set()


async def test_translated_ids_does_not_report_purged_rows(store) -> None:
    await store.upsert_many([
        _item("old", title="旧闻", title_en="old news", minutes=-60)
    ])
    await store.purge_before(_BASE - timedelta(minutes=30))
    assert await store.translated_ids(["old"]) == set()


# ---------------------------------------------------------------------------
# 已存在行的内容刷新：清洗规则与新译文要能落到存量数据上
# ---------------------------------------------------------------------------


async def test_existing_row_content_is_refreshed(store) -> None:
    """上线时表里躺着一批带「发生了什么」前缀的存量行。同 news_id 再来一轮
    必须把清洗后的正文写回去，否则改动对已入库的新闻等于没生效。"""
    await store.upsert_many([_item("a", title="t", content="发生了什么？ 旧正文")])
    await store.upsert_many([_item("a", title="t", content="旧正文")])

    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 1 and items[0].content == "旧正文"


async def test_existing_row_gets_translation_on_a_later_round(store) -> None:
    await store.upsert_many([_item("a", title="BTC breaks", title_en="")])
    await store.upsert_many([
        _item("a", title="比特币突破", title_en="BTC breaks")
    ])

    items, _ = await store.query(NewsQuery(limit=10))
    assert items[0].title == "比特币突破" and items[0].title_en == "BTC breaks"


async def test_refresh_never_overwrites_an_existing_translation(store) -> None:
    """关键回归：已翻译条目在后续轮次会跳过翻译，此时送进来的 title 又是
    上游英文原文。若无条件写回，译文会被冲掉、下一轮再翻一次——标题在
    中英之间反复抖动，还按轮烧 token。"""
    await store.upsert_many([
        _item("a", title="比特币突破", title_en="BTC breaks", content="正文")
    ])
    await store.upsert_many([
        _item("a", title="BTC breaks", title_en="", content="正文 v2")
    ])

    items, _ = await store.query(NewsQuery(limit=10))
    assert items[0].title == "比特币突破", "译文不该被英文原标题覆盖"
    assert items[0].title_en == "BTC breaks"
    assert items[0].content == "正文 v2", "正文仍应跟随最新一轮"


async def test_refresh_keeps_row_count_and_merges_tags(store) -> None:
    await store.upsert_many([_item("a", title="t", tags=("crypto",))])
    await store.upsert_many([_item("a", title="t", tags=("recommend",))])

    items, _ = await store.query(NewsQuery(limit=10))
    assert len(items) == 1
    assert set(items[0].tags) == {"crypto", "recommend"}


# ---------------------------------------------------------------------------
# title_en 需要真正落库、真正回读（新增列最容易漏掉的一环）
# ---------------------------------------------------------------------------


async def test_title_en_round_trips_through_storage(store) -> None:
    await store.upsert_many([
        _item("a", title="比特币突破 71500", title_en="BTC breaks 71500")
    ])
    items, _ = await store.query(NewsQuery(limit=10))
    assert items[0].title_en == "BTC breaks 71500"


async def test_keyword_search_matches_english_original_title(store) -> None:
    """标题翻成中文后，用英文关键词也要还能搜到——原文进了 title_en，
    检索必须把它一起算上，否则英文搜索能力会随翻译一起消失。"""
    await store.upsert_many([
        _item("a", title="比特币突破 71500", title_en="BTC breaks 71500", content="中文摘要")
    ])
    items, _ = await store.query(NewsQuery(q="BTC breaks", limit=10))
    assert [item.news_id for item in items] == ["a"]


async def test_content_en_round_trips_through_storage(store) -> None:
    await store.upsert_many([
        _item(
            "a",
            title="ARM 股价大涨",
            title_en="ARM surges",
            content="ARM 股价盘中大涨5%。",
            content_en="ARM stock surged 5% today.",
        )
    ])
    items, _ = await store.query(NewsQuery(limit=10))
    assert items[0].content_en == "ARM stock surged 5% today."


async def test_refresh_never_overwrites_existing_content_translation(store) -> None:
    """关键回归：已翻译正文条目在后续轮次若被未翻译的英文原文再次命中，
    中文摘要绝不能被冲回英文原文。"""
    await store.upsert_many([
        _item(
            "a",
            title="ARM 股价大涨",
            title_en="ARM surges",
            content="ARM 股价盘中大涨5%。",
            content_en="ARM stock surged 5% today.",
        )
    ])
    # 下一轮跳过翻译，送来的是英文原文
    await store.upsert_many([
        _item(
            "a",
            title="ARM surges",
            title_en="",
            content="ARM stock surged 5% today.",
            content_en="",
        )
    ])

    items, _ = await store.query(NewsQuery(limit=10))
    assert items[0].title == "ARM 股价大涨"
    assert items[0].content == "ARM 股价盘中大涨5%。", "中文正文不应被英文原文冲掉"
    assert items[0].content_en == "ARM stock surged 5% today."


async def test_keyword_search_matches_english_original_content(store) -> None:
    await store.upsert_many([
        _item(
            "a",
            title="英伟达财报",
            title_en="Nvidia earnings",
            content="英伟达最新财报表现亮眼。",
            content_en="Nvidia reported exceptional revenue figures.",
        )
    ])
    items, _ = await store.query(NewsQuery(q="exceptional revenue", limit=10))
    assert [item.news_id for item in items] == ["a"]

