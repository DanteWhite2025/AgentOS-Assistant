"""容器接线回归。

这一层是单测覆盖不到的最后一块：store 选型、titler 该不该装、清理循环
起不起，全靠 build_container 里的几个条件表达式，写错了单测一个都不会红。
REAL 配置照 tests/test_investment_expert_wiring.py 的范式。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.config import Settings
from app.container import build_container
from app.history.pg_store import PostgresConversationStore
from app.history.store import InMemoryConversationStore
from app.history.titler import ConversationTitler

REAL: dict[str, object] = {
    "provider_mode": "real",
    "env": "staging",
    "realtime_voice_provider": "openai",
    "openai_api_key": "sk-real-not-logged",
    "llm_base_url": "https://newapi.example.com/v1",
    "llm_api_key": "sk-newapi-not-logged",
    "llm_model": "claude-opus-4-8-low",
    "followin_api_key": "followin-key-for-test",
    "core_actor_token": "agent-staging-token",
    "core_base_url": "https://core-staging.example.com",
    "rt_ws_base": "wss://runtime-staging.example.com",
    "redis_url": "",
}

FAKE: dict[str, object] = {"provider_mode": "fake", "env": "local"}


@asynccontextmanager
async def _container(**overrides: object) -> AsyncIterator:
    """建容器并保证关掉。

    必须关：真实模式的容器持有 httpx 客户端，配了 database_url 时还持有一个
    asyncpg 引擎（Container._engine）。不 aclose 的话这些连接池会泄漏到后续
    测试——实测表现为全量套件直接挂住，而单独跑这个文件 2 秒就过，极难定位。
    """

    container = build_container(Settings(**overrides))
    try:
        yield container
    finally:
        await container.aclose()


async def test_real_mode_with_database_uses_postgres_store() -> None:
    async with _container(**(REAL | {"database_url": "postgresql+asyncpg://u:p@h/db"})) as c:
        assert isinstance(c.conversation_store, PostgresConversationStore)


async def test_real_mode_without_database_falls_back_to_memory() -> None:
    """库没配不能让服务起不来——退化成进程内存储，接口照常可用。"""
    async with _container(**(REAL | {"database_url": ""})) as c:
        assert isinstance(c.conversation_store, InMemoryConversationStore)


async def test_fake_mode_uses_memory_store() -> None:
    async with _container(**FAKE) as c:
        assert isinstance(c.conversation_store, InMemoryConversationStore)


async def test_titler_is_wired_in_real_mode() -> None:
    async with _container(**REAL) as c:
        assert isinstance(c.conversation_titler, ConversationTitler)


async def test_titler_is_not_wired_in_fake_mode() -> None:
    """FakeLLM 回不出 {"title": ...}，装了只是白跑一次无用调用。"""
    async with _container(**FAKE) as c:
        assert c.conversation_titler is None


async def test_titler_and_purger_are_off_when_history_disabled() -> None:
    async with _container(**(REAL | {"history_enabled": False})) as c:
        assert c.conversation_titler is None
        assert c.conversation_purger is None


async def test_runner_receives_both_conversation_store_and_titler() -> None:
    """漏传任一个都不会报错，只是历史静默不落库 / 标题静默不生成。"""
    async with _container(**REAL) as c:
        assert c.runner._conversation_store is c.conversation_store
        assert c.runner._conversation_titler is c.conversation_titler


async def test_purger_uses_its_own_round_lock() -> None:
    """与新闻采集共用锁会让两者互相抢，谁抢到谁跑、另一个整轮跳过。"""
    async with _container(**REAL) as c:
        assert c.conversation_purger is not None
        assert c.conversation_purger._lock is not c.news_collector.round_lock


def test_history_settings_all_have_defaults() -> None:
    """一个必填项都不能有：push main 即部署 real，漏配 set_env 会让整个
    服务起不来（已发生过的事故模式）。"""
    settings = Settings(provider_mode="fake", env="local")
    assert settings.history_enabled is True
    assert settings.session_retention_days == 30
    # 刻意是小时级而非 86400，见 config.py 的注释
    assert settings.session_purge_interval_seconds == 3600
