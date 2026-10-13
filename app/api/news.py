"""新闻检索 API。

只读本地 news 表，不透传上游 —— 上游 limit 上限 50、无分页，直接代理
给不了下拉加载。数据由 app/news/collector.py 的后台循环持续写入。

默认时间窗是性能护栏而非产品选择：2 字中文关键词提取不出完整 trigram，
pg_trgm 索引用不上、退化为顺序扫描，必须钉住扫描范围。

`lang` 是**标题展示语言**，不是筛选条件。它曾经是 `WHERE lang = ?`：而
trending route 不返回 source_lang、那批条目的 lang 恒为空，推荐 tab 传
lang=zh-cn 会返回 0 条（staging 实测），恰好滤掉最需要中文标题的那一批。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, get_args

from fastapi import APIRouter, Depends, Query, Request

from app.api import schemas
from app.api.deps import get_container
from app.api.errors import error_response
from app.container import Container
from app.news.models import NewsCard, NewsPage, NewsQuery, NewsTag

router = APIRouter(tags=["news"])

_ERR: dict[str, Any] = {"model": schemas.ErrorEnvelope}
Ctn = Annotated[Container, Depends(get_container)]
_ALLOWED_TAGS: tuple[str, ...] = get_args(NewsTag)


def _caller_id(request: Request) -> str:
    """限流身份：只按对端 IP 计。

    不采用 X-User-Id：本端点无鉴权，该 header 由调用方任意指定，用它做限流
    键等于放开配额——攻击者每次换一个值就能落进新的配额桶，等于没有限流。
    这在 q= 命中 2 字中文关键词、pg_trgm 退化为顺序扫描的场景下尤其危险：
    无限配额 = 给了一个反复打 PG 顺序扫描的开关。
    """
    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


@router.get(
    "/news",
    response_model=NewsPage,
    summary="新闻检索（tag 过滤 / 关键词搜索 / 游标分页）",
    responses={400: _ERR, 429: _ERR, 503: _ERR},
)
async def list_news(
    container: Ctn,
    request: Request,
    tag: str = Query(default="", description=f"标签过滤，单值。可选：{'、'.join(_ALLOWED_TAGS)}"),
    q: str = Query(default="", max_length=100, description="关键词，匹配标题与正文"),
    lang: str = Query(
        default="",
        description=(
            "标题展示语言。默认（不传 / cn / zh / zh-cn）给中文标题；"
            "en 给上游英文原标题。不是数据过滤条件，不会减少返回条数。"
        ),
    ),
    days: int = Query(
        default=0, ge=0, le=365, description="时间窗（天）。0 = 用服务端默认（30 天）"
    ),
    limit: int = Query(default=20, ge=1, le=50, description="单页条数"),
    cursor: str = Query(default="", description="上一页返回的 next_cursor"),
):
    """按标签与关键词检索新闻，按发布时间倒序、游标分页。"""
    store = container.news_store
    if store is None:
        return error_response("INTERNAL", "新闻服务未配置或不可用", 503)

    if tag and tag not in _ALLOWED_TAGS:
        return error_response(
            "INVALID_REQUEST",
            f"标签 '{tag}' 不可用。可选：{'、'.join(_ALLOWED_TAGS)}",
            400,
            details={"tag": tag, "allowed": list(_ALLOWED_TAGS)},
        )

    limiter = container.rate_limiter
    if limiter is not None:
        window = container.settings.news_query_rate_window_seconds
        quota = container.settings.news_query_rate_limit
        if quota > 0 and not await limiter.allow(
            f"news:query:{_caller_id(request)}", quota, window
        ):
            return error_response(
                "RATE_LIMITED",
                f"新闻检索已达配额（{quota} 次 / {window}s），请稍后重试",
                429,
                details={"limit": quota, "window_seconds": window},
            )

    span = days or container.settings.news_default_time_range_days
    query = NewsQuery(
        tag=tag or None,  # type: ignore[arg-type]
        q=q.strip(),
        since=datetime.now(UTC) - timedelta(days=span),
        limit=limit,
        cursor=cursor or None,
    )
    try:
        items, next_cursor = await store.query(query)
    except ValueError as exc:
        # 只有游标解析会抛 ValueError
        return error_response("INVALID_REQUEST", f"游标不合法：{exc}", 400)

    return NewsPage(
        items=[NewsCard.of(item, title_lang=lang) for item in items],
        next_cursor=next_cursor,
    )
