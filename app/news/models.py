"""新闻领域模型。

NewsItem 是内部完整记录（含 raw / dedup_key，供采集与存储用）；
NewsCard 是对外投影 —— raw 里有上游原始 HTML，绝不能进响应体。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, Field

NewsTag = Literal["recommend", "crypto", "stocks", "ai_tech", "macro"]
NEWS_TAGS: Final[tuple[NewsTag, ...]] = ("recommend", "crypto", "stocks", "ai_tech", "macro")


class NewsItem(BaseModel):
    """一条入库新闻的完整记录。"""

    news_id: str = Field(description="sha256(source_url)；无 url 时取 source_name+content 摘要")
    dedup_key: str = Field(description="规范化标题，用于跨 route 合并")
    source: str = Field(description="media | research | trending")
    title: str = Field(description="展示标题。英文标题翻译成功后这里是中文")
    title_en: str = Field(
        default="",
        description="上游英文原标题，仅在标题被翻译过时非空；未翻译的条目留空",
    )
    content: str = Field(description="剥 HTML 后的纯文本，已去掉上游摘要的固定开场句")
    content_en: str = Field(
        default="",
        description="上游英文原正文，仅在正文被翻译/生成中文摘要时非空；未翻译的条目留空",
    )
    published_at: datetime
    source_name: str = ""
    source_url: str = ""
    source_quality: str = ""
    lang: str = ""
    asset_type: str = ""
    upstream_category: str = ""
    tags: tuple[NewsTag, ...] = ()
    raw: dict[str, Any] = Field(default_factory=dict, description="上游原始行，含原始 HTML")


# 展示语言。除 en 外一律给中文——前端历史上传过 cn / zh / zh-cn 三种写法，
# 与其挑一个当唯一合法值再对其余报 400，不如只识别 en、其余落到中文默认：
# 这个参数只决定标题取哪一列，猜错的代价是一行标题的语言，不是一次失败请求。
_EN: Final = "en"


def normalize_title_lang(value: str) -> str:
    """展示语言归一化：en 系（en / EN / en-US）→ en，其余（含空值）→ zh-cn。"""
    return _EN if (value or "").strip().lower().startswith(_EN) else "zh-cn"


class NewsCard(BaseModel):
    """对外投影。不含 raw / dedup_key / source_quality / upstream_category。

    ``title_en`` 也不出现在这里：标题的语言由请求的 lang 决定，客户端拿到的
    永远是一个 title 字段，不需要自己在两列之间挑。
    """

    news_id: str
    title: str
    content: str
    source_name: str
    source_url: str
    published_at: datetime
    tags: list[NewsTag]
    asset_type: str
    lang: str

    @classmethod
    def of(cls, item: NewsItem, *, title_lang: str = "") -> NewsCard:
        """按展示语言投影标题与正文。

        lang=en 且该条没有英文原文（英文源直出，title/content 本身就是原文）时回落
        到 title/content —— 绝不能返回空。
        """
        english = normalize_title_lang(title_lang) == _EN
        return cls(
            news_id=item.news_id,
            title=(item.title_en or item.title) if english else item.title,
            content=(item.content_en or item.content) if english else item.content,
            source_name=item.source_name,
            source_url=item.source_url,
            published_at=item.published_at,
            tags=list(item.tags),
            asset_type=item.asset_type,
            lang=item.lang,
        )


class NewsPage(BaseModel):
    items: list[NewsCard]
    next_cursor: str | None = Field(default=None, description="null 表示已到底")


class NewsQuery(BaseModel):
    """一次查询的全部条件。

    没有 lang：它是展示语言而不是筛选条件，在 NewsCard 投影层处理。曾经
    这里的 lang 会翻成 `WHERE lang = ?`，而 trending route 不返回
    source_lang、那批条目的 lang 恒为空——推荐 tab 传 lang=zh-cn 会返回
    0 条（staging 实测），恰好滤掉最需要中文标题的那批。
    """

    tag: NewsTag | None = None
    q: str = ""
    since: datetime | None = None
    limit: int = 20
    cursor: str | None = None
