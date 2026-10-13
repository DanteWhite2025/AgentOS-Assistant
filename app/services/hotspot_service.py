"""每日热点早报生成器。

摘要走内部统一 LLM 网关（app.agents.llm.LLMProvider）直接生成。用户点击
推荐卡片/深度追问时，走另一条独立路径——以 Agent 工具形式调用
app.tools.investment_expert_tools.ask_investment_expert（同样是内部 LLM，只是换了一个
"投研分析师"system prompt，见 app.services.investment_expert_client），职责分开：
早报是"读新闻 + 内部模型提炼"，深度问答是"内部模型的投研分析人格"。

LLM 输出解析失败时降级返回一个只有 headline/summary、无推荐卡片的兜底
digest，不让早报接口因为模型偶尔不吐合法 JSON 而整体报错。

缓存策略（这次凌晨 quota 打满刷出几百次 503 的根因就在这里）：
- 成功结果按 TTL 复用；早报不需要秒级新鲜度。
- 并发 miss 单飞，同一时刻只允许一次真实 LLM 调用。
- TTL 过期后刷新失败（quota / 网关超时等）继续返回上一份成功结果，
  不能把过期缓存扔掉后再让每个请求各自去撞同一块额度墙。
- 冷启动且没有可返回的旧结果时，失败进入短冷却，冷却期内不再打 LLM。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ValidationError

from app.agents.llm import LLMMessage, LLMProvider
from app.news.models import NewsQuery
from app.news.store import NewsStore

logger = logging.getLogger(__name__)

_LOOKBACK_HOURS = 12
_NEWS_LIMIT = 30
_FALLBACK_HEADLINE = "今日市场动态"
# 端点公开无鉴权，每次未命中缓存都会触发一次真实付费 LLM 调用。
# 早报全国一份、不需要分钟级新鲜度；默认 30 分钟，减少 LLM 刷新次数。
_DEFAULT_CACHE_TTL_SECONDS = 1800.0
# 无旧缓存可降级时，避免 insufficient_user_quota 这类持续失败被每个请求重放。
_DEFAULT_ERROR_COOLDOWN_SECONDS = 60.0

_SYSTEM_PROMPT = (
    "你是 YesUP 的市场早报编辑。基于给定的近期新闻列表，提炼一份简明的每日市场热点早报。"
    "必须只输出一个 JSON 对象，不要有任何 JSON 之外的文字，格式为："
    '{"headline": "今日核心行情速递", "summary": "1. ...\\n2. ...", '
    '"suggested_actions": [{"label": "🔥 ...", "prompt": "..."}]}。'
    "summary 用编号列出 2~4 条当前最重要的市场要闻/大盘动态；"
    "suggested_actions 给出 6~9 个用户可能想深入追问的议题，数量必须充足，供前端按 3 个"
    "一组分页切组展示，不得因凑不够而重复或改写同一个议题；label 简短带一个 emoji，"
    "prompt 是替用户写好的、可直接发给分析引擎的完整提问；"
    "prompt 必须自带完整主语——币种、公司、机构、标的一律写全名，"
    "严禁使用代词或指代早报正文的说法（「该项目」「这只币」「上述」「它」），"
    "因为用户点开时分析引擎收到的只有这一句话，看不到早报内容。"
)


class HotspotUnavailable(Exception):
    """LLM 生成失败，且当前没有可返回的过期缓存。"""


class SuggestedAction(BaseModel):
    label: str
    prompt: str


class HotspotDigest(BaseModel):
    headline: str
    summary: str
    suggested_actions: list[SuggestedAction] = []


def _fallback_digest(summary: str) -> HotspotDigest:
    return HotspotDigest(headline=_FALLBACK_HEADLINE, summary=summary, suggested_actions=[])


def _parse_digest(raw_text: str) -> HotspotDigest:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        logger.warning("热点早报 LLM 输出不是合法 JSON，降级为兜底摘要")
        return _fallback_digest(raw_text.strip()[:500])
    if not isinstance(payload, dict) or not payload.get("headline"):
        logger.warning("热点早报 LLM 输出缺少必要字段，降级为兜底摘要")
        return _fallback_digest(str(payload.get("summary", "")) if isinstance(payload, dict) else "")
    try:
        return HotspotDigest.model_validate(payload)
    except ValidationError:
        # JSON 语法合法但字段形状不对（如某条 suggested_actions 项缺 prompt）时
        # 同样要降级，不能让 ValidationError 把 GET /hotspots/daily 拖成裸 500。
        logger.warning("热点早报 LLM 输出字段形状不对，降级为兜底摘要")
        return _fallback_digest(str(payload.get("summary", "")))


class HotspotService:
    """进程内单实例持有：TTL 缓存要跨请求生效，必须由 Container 装配一次并复用

    （而不是每个请求各自 new 一份——那样缓存形同虚设）。
    """

    def __init__(
        self,
        news_store: NewsStore,
        llm: LLMProvider,
        *,
        cache_ttl_seconds: float = _DEFAULT_CACHE_TTL_SECONDS,
        error_cooldown_seconds: float = _DEFAULT_ERROR_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._news_store = news_store
        self._llm = llm
        self._cache_ttl_seconds = cache_ttl_seconds
        self._error_cooldown_seconds = error_cooldown_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._cached_digest: HotspotDigest | None = None
        self._cached_at: float | None = None
        self._failed_at: float | None = None

    def _fresh(self, now: float) -> bool:
        return (
            self._cached_digest is not None
            and self._cached_at is not None
            and now - self._cached_at < self._cache_ttl_seconds
        )

    def _in_cooldown(self, now: float) -> bool:
        return (
            self._failed_at is not None
            and now - self._failed_at < self._error_cooldown_seconds
        )

    def peek_cached(self) -> HotspotDigest | None:
        """进程里有过成功结果就返回（含过期）。接口层用它决定要不要限流。"""
        return self._cached_digest

    async def build_daily_digest(self) -> HotspotDigest:
        now = self._clock()
        if self._fresh(now) and self._cached_digest is not None:
            return self._cached_digest

        # 刷新进行中：有过期结果就立刻返回，首页不要卡在 LLM 上。
        if self._lock.locked() and self._cached_digest is not None:
            return self._cached_digest

        async with self._lock:
            now = self._clock()
            if self._fresh(now) and self._cached_digest is not None:
                return self._cached_digest
            if self._in_cooldown(now) and self._cached_digest is not None:
                return self._cached_digest
            if self._in_cooldown(now):
                raise HotspotUnavailable("热点早报暂不可用")

            try:
                digest = await self._generate()
            except Exception as exc:
                self._failed_at = now
                if self._cached_digest is not None:
                    logger.warning("热点早报刷新失败，返回过期缓存", exc_info=True)
                    return self._cached_digest
                logger.exception("热点早报生成失败")
                raise HotspotUnavailable("热点早报生成失败") from exc

            self._cached_digest = digest
            self._cached_at = now
            self._failed_at = None
            return digest

    async def _generate(self) -> HotspotDigest:
        query = NewsQuery(since=datetime.now(UTC) - timedelta(hours=_LOOKBACK_HOURS), limit=_NEWS_LIMIT)
        items, _ = await self._news_store.query(query)

        if not items:
            digest_input = "（近 12 小时暂无采集到的新闻）"
        else:
            digest_input = "\n".join(
                f"- [{item.published_at.isoformat()}] {item.title}：{item.content[:200]}"
                for item in items
            )

        result = await self._llm.complete(
            system=_SYSTEM_PROMPT,
            messages=(LLMMessage(role="user", content=digest_input),),
            tools=(),
        )
        return _parse_digest(result.text)
