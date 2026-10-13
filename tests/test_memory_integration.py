"""分层记忆系统集成测试（Integration Tests）。

验证：
1. MemoryStore + ContextBuilder + AgentRunner 跨组件协同工作。
2. 短期记忆 (Session) 与长期记忆 (Profile & Episodes) 的分层流转。
3. 记忆更新、遗忘与多用户数据隔离。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.agents.llm import LLMMessage, LLMResult
from app.agents.runner import AgentProfile, AgentRunner
from app.audit.store import InMemoryAuditStore
from app.memory.pg_store import PostgresMemoryStore
from app.memory.store import InMemoryMemoryStore
from app.runtime.context_builder import ContextBuilder
from app.runtime.session import Scope, Session
from app.tools.builtin import build_default_registry
from app.tools.core_client import FakeCoreClient
from app.tools.executor import ToolExecutor


class CapturingLLM:
    """用于捕获实际传入 System Prompt 的 Mock LLM。"""

    def __init__(self, response_text: str = "回复内容") -> None:
        self.last_system_prompt: str | None = None
        self.last_messages: tuple[LLMMessage, ...] = ()
        self.response_text = response_text

    async def complete(
        self,
        system_prompt: str,
        messages: tuple[LLMMessage, ...],
        tools: tuple[str, ...],
    ) -> LLMResult:
        self.last_system_prompt = system_prompt
        self.last_messages = messages
        return LLMResult(text=self.response_text)


def _create_session(user_id: str, session_id: str = "sess_001") -> Session:
    now = datetime.now(UTC)
    return Session(
        session_id=session_id,
        user_id=user_id,
        modality="text",
        scope=Scope(agents=("avatar",), tools=("get_positions",)),
        state="text_active",
        created_at=now,
        last_activity_at=now,
    )


@pytest.mark.asyncio
async def test_memory_integration_full_flow() -> None:
    """集成测试：长期记忆在 AgentRunner 对话全链路中的注入与生效。"""
    # 1. 初始化持久化层（使用 SQLite 内存引擎模拟 PostgreSQL）
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    memory_store = PostgresMemoryStore(engine)
    await memory_store.create_schema()

    user_id = "user_trader_alpha"

    # 2. 预置长期画像记忆与情景决策记忆
    await memory_store.remember_profile(
        user_id,
        category="preferences",
        key="trading_style",
        content={"style": "网格套利", "risk_level": "conservative"},
    )
    await memory_store.remember_profile(
        user_id,
        category="assets",
        key="watchlist",
        content=["BTC", "ETH", "SOL"],
    )
    await memory_store.remember_episode(
        user_id,
        summary="用户于2026-08配置了BTC现货对冲策略",
        details={"pair": "BTC/USDC", "hedging": True},
    )

    # 3. 组装 ContextBuilder 与 AgentRunner
    context_builder = ContextBuilder(memory_store)
    capturing_llm = CapturingLLM(response_text="已为您结合网格偏好完成分析。")
    audit = InMemoryAuditStore()
    executor = ToolExecutor(build_default_registry(), FakeCoreClient())

    profile = AgentProfile(
        name="avatar",
        model="fake",
        system_prompt="你是 YesUP 智能投顾数字人。",
        tools=("get_positions",),
    )

    runner = AgentRunner(
        profile=profile,
        executor=executor,
        llm=capturing_llm,
        audit=audit,
        context_builder=context_builder,
    )

    # 4. 执行一轮回话
    session = _create_session(user_id)
    now = datetime.now(UTC)
    result = await runner.run_turn(
        session,
        user_text="我想了解下我当前的BTC对冲策略表现如何？",
        trace_id="trace_test_01",
        now=now,
    )

    assert result.text == "已为您结合网格偏好完成分析。"

    # 5. 断言 LLM 收到的 System Prompt 准确注入了分层记忆
    sys_prompt = capturing_llm.last_system_prompt
    assert sys_prompt is not None
    assert "你是 YesUP 智能投顾数字人。" in sys_prompt
    assert "<user_profile>" in sys_prompt
    assert "[preferences] trading_style:" in sys_prompt
    assert "网格套利" in sys_prompt
    assert "[assets] watchlist:" in sys_prompt
    assert "<recalled_memory>" in sys_prompt
    assert "BTC现货对冲策略" in sys_prompt


@pytest.mark.asyncio
async def test_memory_multi_user_isolation() -> None:
    """集成测试：多用户记忆严格隔离，A 用户的偏好绝不泄露给 B 用户。"""
    store = InMemoryMemoryStore()
    context_builder = ContextBuilder(store)
    capturing_llm = CapturingLLM()
    audit = InMemoryAuditStore()
    executor = ToolExecutor(build_default_registry(), FakeCoreClient())

    profile = AgentProfile(name="avatar", model="fake", system_prompt="助手", tools=())
    runner = AgentRunner(
        profile=profile,
        executor=executor,
        llm=capturing_llm,
        audit=audit,
        context_builder=context_builder,
    )

    # 存储 User A 的专属记忆
    await store.remember_profile("user_A", "secret", "api_pref", {"tier": "VIP_WHALE"})
    await store.remember_episode("user_A", "User A 买了 100 个 BTC")

    # User B 发起请求
    session_b = _create_session("user_B")
    now = datetime.now(UTC)
    await runner.run_turn(session_b, "请问我有什么特别设置？", "trace_b", now)

    prompt_b = capturing_llm.last_system_prompt
    assert prompt_b is not None
    assert "VIP_WHALE" not in prompt_b
    assert "100 个 BTC" not in prompt_b
    assert "<user_profile>" not in prompt_b


@pytest.mark.asyncio
async def test_memory_update_and_forget_lifecycle() -> None:
    """集成测试：用户偏好更新与遗忘后，下一轮推理实时体现变更。"""
    store = InMemoryMemoryStore()
    context_builder = ContextBuilder(store)
    capturing_llm = CapturingLLM()
    audit = InMemoryAuditStore()
    executor = ToolExecutor(build_default_registry(), FakeCoreClient())

    profile = AgentProfile(name="avatar", model="fake", system_prompt="助手", tools=())
    runner = AgentRunner(
        profile=profile,
        executor=executor,
        llm=capturing_llm,
        audit=audit,
        context_builder=context_builder,
    )

    user_id = "user_update_test"
    session = _create_session(user_id)
    now = datetime.now(UTC)

    # 初始偏好
    await store.remember_profile(user_id, "preference", "leverage", {"max": 3})
    await runner.run_turn(session, "第一轮", "trace_1", now)
    assert '"max": 3' in (capturing_llm.last_system_prompt or "")

    # 更新偏好
    await store.remember_profile(user_id, "preference", "leverage", {"max": 10})
    await runner.run_turn(session, "第二轮", "trace_2", now)
    assert '"max": 10' in (capturing_llm.last_system_prompt or "")

    # 遗忘删除
    await store.forget_profile(user_id, "preference", "leverage")
    await runner.run_turn(session, "第三轮", "trace_3", now)
    assert "<user_profile>" not in (capturing_llm.last_system_prompt or "")


@pytest.mark.asyncio
async def test_container_startup_calls_memory_store_create_schema() -> None:
    """回归测试：验证 container.startup() 自动触发 memory_store.create_schema()。"""
    from app.config import Settings
    from app.container import build_container

    container = build_container(Settings(provider_mode="fake"))

    schema_created = False

    class MockMemoryStore:
        async def create_schema(self) -> None:
            nonlocal schema_created
            schema_created = True

    container.memory_store = MockMemoryStore()  # type: ignore[assignment]
    await container.startup()

    assert schema_created is True
