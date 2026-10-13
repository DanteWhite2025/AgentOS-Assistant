"""TavilySearchClient 单测 —— 连接池复用 + Redis 短时缓存，不改变 tavily_search 本身行为。"""

from __future__ import annotations

import httpx
import respx

from app.tools.tavily_client import TavilySearchClient, TavilySearchConfig

_ANSWER_PAYLOAD = {"answer": "BTC hit $100k.", "results": []}


def test_default_timeout_is_5_seconds_hard_limit():
    """团队定的目标是硬超时 5 秒：联网搜索慢不如不等，不能悄悄retreat回旧的15秒。"""
    assert TavilySearchConfig(api_key="tvly-test").timeout_seconds == 5.0


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int]] = []

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int) -> None:
        self.store[key] = value
        self.set_calls.append((key, value, ex))


async def test_search_reuses_shared_http_client_without_creating_new_one():
    shared_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=_ANSWER_PAYLOAD)
    ))
    try:
        client = TavilySearchClient(
            TavilySearchConfig(api_key="tvly-test"), http_client=shared_client
        )
        result = await client.search("BTC news")
    finally:
        await shared_client.aclose()

    assert result["source"] == "TAVILY_SEARCH"
    assert result["answer"] == "BTC hit $100k."


async def test_search_without_redis_hits_tavily_every_time():
    call_count = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_ANSWER_PAYLOAD)

    shared_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    try:
        client = TavilySearchClient(
            TavilySearchConfig(api_key="tvly-test"), http_client=shared_client
        )
        await client.search("BTC news")
        await client.search("BTC news")
    finally:
        await shared_client.aclose()

    assert call_count == 2


async def test_search_caches_result_and_skips_second_tavily_call():
    redis = _FakeRedis()
    with respx.mock:
        route = respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json=_ANSWER_PAYLOAD)
        )
        client = TavilySearchClient(
            TavilySearchConfig(api_key="tvly-test", cache_ttl_seconds=90),
            redis_client=redis,
        )
        first = await client.search("BTC news", topic="news")
        second = await client.search("BTC news", topic="news")

    assert route.call_count == 1
    assert first.get("cached") is not True
    assert second["cached"] is True
    assert second["answer"] == "BTC hit $100k."


async def test_search_does_not_cache_error_results():
    redis = _FakeRedis()
    with respx.mock:
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(500, json={"error": "boom"})
        )
        client = TavilySearchClient(
            TavilySearchConfig(api_key="tvly-test", cache_ttl_seconds=90),
            redis_client=redis,
        )
        result = await client.search("BTC news")

    assert "error" in result
    assert redis.set_calls == []


async def test_search_cache_key_isolates_by_topic_and_time_range():
    redis = _FakeRedis()
    with respx.mock:
        route = respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json=_ANSWER_PAYLOAD)
        )
        client = TavilySearchClient(
            TavilySearchConfig(api_key="tvly-test", cache_ttl_seconds=90),
            redis_client=redis,
        )
        await client.search("BTC news", topic="general")
        await client.search("BTC news", topic="news", time_range="week")

    assert route.call_count == 2


async def test_zero_ttl_disables_caching():
    redis = _FakeRedis()
    with respx.mock:
        route = respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json=_ANSWER_PAYLOAD)
        )
        client = TavilySearchClient(
            TavilySearchConfig(api_key="tvly-test", cache_ttl_seconds=0),
            redis_client=redis,
        )
        await client.search("BTC news")
        await client.search("BTC news")

    assert route.call_count == 2
    assert redis.set_calls == []


async def test_redis_failure_degrades_to_direct_tavily_call():
    class _BrokenRedis:
        async def get(self, key: str) -> str | None:
            raise RuntimeError("redis down")

        async def set(self, key: str, value: str, ex: int) -> None:
            raise RuntimeError("redis down")

    with respx.mock:
        respx.post("https://api.tavily.com/search").mock(
            return_value=httpx.Response(200, json=_ANSWER_PAYLOAD)
        )
        client = TavilySearchClient(
            TavilySearchConfig(api_key="tvly-test", cache_ttl_seconds=90),
            redis_client=_BrokenRedis(),
        )
        result = await client.search("BTC news")

    assert result["answer"] == "BTC hit $100k."


# ---- 退避重试。搜索超时是全链路最常见的失败（5s 是最紧的预算），产品要求
# 「搜索超时可以尝试重试一下」，且对用户完全无感——重试必须封装在 client 层，
# 因为 runner 在一次 no_data 之后就把 web_search 移出候选了，模型没机会重调。 ----


def _recording_sleep() -> tuple[list[float], object]:
    slept: list[float] = []

    async def _sleep(delay: float) -> None:
        slept.append(delay)

    return slept, _sleep


def _no_jitter(_low: float, _high: float) -> float:
    return 0.0


async def test_timeout_is_retried_and_second_attempt_wins():
    attempts = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.TimeoutException("too slow")
        return httpx.Response(200, json=_ANSWER_PAYLOAD)

    slept, sleep = _recording_sleep()
    shared_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    try:
        client = TavilySearchClient(
            TavilySearchConfig(api_key="tvly-test"),
            http_client=shared_client,
            sleep=sleep,
            jitter=_no_jitter,
        )
        result = await client.search("BTC news")
    finally:
        await shared_client.aclose()

    assert attempts == 2
    assert result["answer"] == "BTC hit $100k."
    assert "error" not in result, "重试成功后不能留下失败痕迹"
    assert slept == [0.25], "第一次退避 0.25s"


async def test_non_retryable_error_is_not_retried():
    """401 是密钥配置问题，重试多少次都一样，只会白等。"""
    attempts = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={"error": "unauthorized"})

    slept, sleep = _recording_sleep()
    shared_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    try:
        client = TavilySearchClient(
            TavilySearchConfig(api_key="tvly-test"),
            http_client=shared_client,
            sleep=sleep,
            jitter=_no_jitter,
        )
        result = await client.search("BTC news")
    finally:
        await shared_client.aclose()

    assert attempts == 1
    assert result["error_code"] == "SEARCH_UNAUTHORIZED"
    assert slept == []


async def test_retry_budget_is_exhausted_and_last_error_returned():
    attempts = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"error": "unavailable"})

    slept, sleep = _recording_sleep()
    shared_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    try:
        client = TavilySearchClient(
            TavilySearchConfig(api_key="tvly-test", max_retries=2),
            http_client=shared_client,
            sleep=sleep,
            jitter=_no_jitter,
        )
        result = await client.search("BTC news")
    finally:
        await shared_client.aclose()

    assert attempts == 3, "max_retries=2 意味着首次 + 2 次重试"
    assert result["error_code"] == "SEARCH_UPSTREAM_ERROR"
    assert slept == [0.25, 0.5], "指数退避"


async def test_default_retry_budget_is_one():
    """用户是在等回答，不能为了重试把一次搜索拖成十几秒。默认只重试一次。"""
    assert TavilySearchConfig(api_key="tvly-test").max_retries == 1


async def test_retry_after_header_is_honored_on_rate_limit():
    attempts = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "slow down"})
        return httpx.Response(200, json=_ANSWER_PAYLOAD)

    slept, sleep = _recording_sleep()
    shared_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    try:
        client = TavilySearchClient(
            TavilySearchConfig(api_key="tvly-test"),
            http_client=shared_client,
            sleep=sleep,
            jitter=_no_jitter,
        )
        result = await client.search("BTC news")
    finally:
        await shared_client.aclose()

    assert attempts == 2
    assert slept == [2.0], "Retry-After 比默认退避长时以它为准"
    assert result["answer"] == "BTC hit $100k."


async def test_cache_hit_never_triggers_a_request_or_retry():
    redis = _FakeRedis()
    calls = 0

    def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_ANSWER_PAYLOAD)

    shared_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    try:
        client = TavilySearchClient(
            TavilySearchConfig(api_key="tvly-test"),
            http_client=shared_client,
            redis_client=redis,
        )
        await client.search("BTC news")
        second = await client.search("BTC news")
    finally:
        await shared_client.aclose()

    assert calls == 1
    assert second["cached"] is True


async def test_failed_retries_do_not_poison_the_cache():
    redis = _FakeRedis()

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    _slept, sleep = _recording_sleep()
    shared_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    try:
        client = TavilySearchClient(
            TavilySearchConfig(api_key="tvly-test"),
            http_client=shared_client,
            redis_client=redis,
            sleep=sleep,
            jitter=_no_jitter,
        )
        await client.search("BTC news")
    finally:
        await shared_client.aclose()

    assert redis.set_calls == []
