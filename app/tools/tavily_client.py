"""Tavily 搜索的连接池与短时缓存封装。

`tavily_search()`（见 search_tools.py）本身只是一次裸 HTTP 调用；容器启动时
高频命中的重复查询（同一天内多个用户问同一件热点事）如果每次都新建 TCP 连接、
现打 Tavily，既浪费配额也拖慢响应。这层只做两件事：复用长连接 httpx client、
按「日期+主题+关键词」做 60~120 秒的短时 Redis 缓存——不改变 tavily_search 本身
的行为和返回结构。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.tools.retry import Jitter, Sleep, backoff_sleep
from app.tools.search_tools import tavily_search

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TavilySearchConfig:
    api_key: str
    timeout_seconds: float = 5.0
    cache_ttl_seconds: int = 90
    # 默认只重试一次：用户正在等回答，5s 超时 × 3 次尝试会把一次搜索拖到
    # 十几秒，比"没搜到"更难接受。
    max_retries: int = 1


class TavilySearchClient:
    """给 ToolExecutor 用的 Tavily 门面：连接池复用 + 可选 Redis 短时缓存 + 退避重试。"""

    def __init__(
        self,
        config: TavilySearchConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        redis_client: Any = None,
        sleep: Sleep = asyncio.sleep,
        jitter: Jitter = random.uniform,
    ) -> None:
        self._config = config
        self._http_client = http_client
        self._redis = redis_client
        self._sleep = sleep
        self._jitter = jitter

    def _cache_key(self, query: str, topic: str, time_range: str | None) -> str:
        # 缓存 key 带当天日期，避免"最新"类查询跨天复用过期结果。
        today = datetime.now(UTC).date().isoformat()
        normalized = " ".join(query.strip().lower().split())
        raw = f"{today}|{topic}|{time_range or ''}|{normalized}"
        digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return f"cache:web_search:{digest}"

    async def search(
        self,
        query: str,
        *,
        topic: str = "general",
        time_range: str | None = None,
    ) -> dict[str, Any]:
        cache_key = (
            self._cache_key(query, topic, time_range)
            if self._redis is not None and self._config.cache_ttl_seconds > 0
            else None
        )

        if cache_key is not None:
            try:
                cached = await self._redis.get(cache_key)
                if cached:
                    data = json.loads(cached)
                    data["cached"] = True
                    return data
            except Exception as exc:
                logger.warning("读取联网搜索 Redis 缓存异常，降级穿透到 Tavily: %s", exc)

        result = await self._search_with_retry(query, topic, time_range)

        if cache_key is not None and not result.get("error"):
            try:
                await self._redis.set(
                    cache_key,
                    json.dumps(result, ensure_ascii=False),
                    ex=self._config.cache_ttl_seconds,
                )
            except Exception as exc:
                logger.warning("写入联网搜索 Redis 缓存异常: %s", exc)

        return result

    async def _search_with_retry(
        self, query: str, topic: str, time_range: str | None
    ) -> dict[str, Any]:
        """按 tavily_search 返回的 retryable 标志做退避重试。

        重试必须封装在这一层：runner 一旦把某次搜索判成 no_data，就会把
        web_search 移出本轮候选工具，模型没有机会自己重调。对上层来说，
        一次 `search()` 调用要么拿到结果，要么是已经尽力过的失败。
        """

        attempt = 0
        while True:
            result = await tavily_search(
                query,
                api_key=self._config.api_key,
                topic=topic,
                time_range=time_range,
                timeout=self._config.timeout_seconds,
                client=self._http_client,
            )
            if not result.get("error"):
                return result
            if not result.get("retryable") or attempt >= self._config.max_retries:
                return result
            logger.info(
                "联网搜索失败将重试 error_code=%s attempt=%d/%d",
                result.get("error_code"),
                attempt + 1,
                self._config.max_retries,
            )
            await backoff_sleep(
                attempt,
                result.get("retry_after"),
                # 退避不超过一次搜索本身的超时预算。Tavily 限流时可能给出
                # 几十上百秒的 Retry-After，照单全收等于让用户干等——超过这个
                # 上限就宁可放弃重试，让上层如实说没搜到。
                max_delay=self._config.timeout_seconds,
                sleep=self._sleep,
                jitter=self._jitter,
            )
            attempt += 1
