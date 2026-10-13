"""模型测试。重点：NewsCard 绝不能泄漏 raw。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.news.models import NewsCard, NewsItem


def _item() -> NewsItem:
    return NewsItem(
        news_id="abc",
        dedup_key="btc breaks 71500",
        source="media",
        title="BTC breaks 71500",
        content="纯文本正文",
        published_at=datetime(2026, 8, 21, tzinfo=UTC),
        source_name="cointelegraph",
        source_url="https://cointelegraph.com/x",
        source_quality="media",
        lang="zh-cn",
        asset_type="crypto",
        upstream_category="majorcoin",
        tags=("crypto", "recommend"),
        raw={"content": "<p><b>看涨观点</b></p>", "secret": "leak"},
    )


def test_card_omits_raw_and_internal_fields() -> None:
    dumped = NewsCard.of(_item()).model_dump()
    for leaked in ("raw", "dedup_key", "source_quality", "upstream_category"):
        assert leaked not in dumped


def test_card_preserves_public_fields() -> None:
    card = NewsCard.of(_item())
    assert card.news_id == "abc"
    assert card.source_url == "https://cointelegraph.com/x"
    assert card.tags == ["crypto", "recommend"]


def test_item_defaults_are_empty_not_none() -> None:
    item = NewsItem(
        news_id="i", dedup_key="d", source="media", title="t", content="c",
        published_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    assert item.source_url == "" and item.tags == () and item.raw == {}


# ---------------------------------------------------------------------------
# 标题多语言投影：title 存中文，title_en 存上游英文原文
# ---------------------------------------------------------------------------


def _translated_item() -> NewsItem:
    return _item().model_copy(
        update={"title": "比特币突破 71500", "title_en": "BTC breaks 71500"}
    )


def test_card_defaults_to_chinese_title() -> None:
    """不传语言 = 前端不改版：直接拿到中文标题。"""
    assert NewsCard.of(_translated_item()).title == "比特币突破 71500"


def test_card_returns_english_title_when_requested() -> None:
    card = NewsCard.of(_translated_item(), title_lang="en")
    assert card.title == "BTC breaks 71500"


def test_card_falls_back_to_stored_title_when_no_english_original() -> None:
    """未经翻译的条目（title 本身就是原文）在 lang=en 下不能返回空标题。"""
    card = NewsCard.of(_item(), title_lang="en")
    assert card.title == "BTC breaks 71500"


def test_card_never_exposes_title_en_field() -> None:
    """title_en 是内部存储列，对外只通过 title 投影暴露。"""
    assert "title_en" not in NewsCard.of(_translated_item()).model_dump()


def test_item_title_en_defaults_to_empty() -> None:
    item = NewsItem(
        news_id="i", dedup_key="d", source="media", title="t", content="c",
        published_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    assert item.title_en == ""
