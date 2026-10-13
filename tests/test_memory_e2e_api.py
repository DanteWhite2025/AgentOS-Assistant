"""端到端 E2E API 测试 —— 验证真实 HTTP 接口下记忆系统的完整流转与容灾降级。"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.agents.llm import LLMMessage, LLMResult
from app.config import Settings
from app.container import build_container
from app.main import create_app

AUTH_HEADER = {"Authorization": "Bearer test-user:u"}


class E2ECapturingLLM:
    """捕获 E2E API 链路中 LLM 接收到的提示词与历史消息。"""

    def __init__(self, response_text: str = "已为您生成分析。") -> None:
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


class BrokenMemoryStore:
    """模拟数据库连接断开/异常的记忆存储。"""

    async def get_profile(self, user_id: str, category: str | None = None) -> Any:
        raise RuntimeError("PostgreSQL connection timeout")

    async def recall_episodes(self, user_id: str, query: str | None = None, limit: int = 5) -> Any:
        raise RuntimeError("PostgreSQL query failed")


def _app_with(container):
    app = create_app()
    app.state.container = container
    return app


def _api(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_e2e_messages_endpoint_with_injected_memory() -> None:
    """E2E 测试：通过 HTTP /v1/agent/messages 端点发起对话，验证记忆系统端到端注入。"""
    container = build_container(Settings(provider_mode="fake"))
    capturing_llm = E2ECapturingLLM(response_text="已为您依据高风险偏好制定 SOL 交易方案。")
    container.llm = capturing_llm
    container.runner._llm = capturing_llm

    # 1. 预置长期画像与情景
    user_id = "u"  # 与 test-user:u 对应
    await container.memory_store.remember_profile(
        user_id=user_id,
        category="risk_preference",
        key="level",
        content={"tolerance": "high", "max_leverage": 5},
    )
    await container.memory_store.remember_episode(
        user_id=user_id,
        summary="用户上次在SOL突破高点时咨询了现货买入",
    )

    app = _app_with(container)

    async with _api(app) as client:
        # 2. 创建会话
        sess_resp = await client.post(
            "/v1/agent/sessions",
            json={"modality": "text"},
            headers=AUTH_HEADER,
        )
        assert sess_resp.status_code == 201
        session_id = sess_resp.json()["session_id"]

        # 3. 发送单轮消息
        msg_resp = await client.post(
            "/v1/agent/messages",
            json={"session_id": session_id, "text": "请帮我看下 SOL 的走势与建仓建议"},
            headers=AUTH_HEADER,
        )

        assert msg_resp.status_code == 200
        data = msg_resp.json()
        assert data["text"] == "已为您依据高风险偏好制定 SOL 交易方案。"
        assert data["trace_id"].startswith("trc_")

        # 4. 验证真实 Prompt 包含记忆
        prompt = capturing_llm.last_system_prompt
        assert prompt is not None
        assert "<user_profile>" in prompt
        assert "[risk_preference] level:" in prompt
        assert '"tolerance": "high"' in prompt
        assert "<recalled_memory>" in prompt
        assert "SOL突破高点" in prompt


@pytest.mark.asyncio
async def test_e2e_messages_memory_failure_graceful_degradation() -> None:
    """E2E 测试：当记忆库发生故障时，/messages 端点自动优雅降级，保障正常对话。"""
    container = build_container(Settings(provider_mode="fake"))
    capturing_llm = E2ECapturingLLM(response_text="常规回复（记忆服务降级）")
    container.llm = capturing_llm
    container.runner._llm = capturing_llm

    # 注入故障记忆库
    from app.runtime.context_builder import ContextBuilder

    broken_store: Any = BrokenMemoryStore()
    container.memory_store = broken_store
    container.context_builder = ContextBuilder(broken_store)
    container.runner._context_builder = container.context_builder

    app = _app_with(container)

    async with _api(app) as client:
        sess_resp = await client.post(
            "/v1/agent/sessions",
            json={"modality": "text"},
            headers=AUTH_HEADER,
        )
        assert sess_resp.status_code == 201
        session_id = sess_resp.json()["session_id"]

        # 请求不应该崩溃，仍返回 200
        msg_resp = await client.post(
            "/v1/agent/messages",
            json={"session_id": session_id, "text": "你好"},
            headers=AUTH_HEADER,
        )
        assert msg_resp.status_code == 200
        assert msg_resp.json()["text"] == "常规回复（记忆服务降级）"


@pytest.mark.asyncio
async def test_e2e_multi_turn_session_state_and_memory_coordination() -> None:
    """E2E 测试：多轮对话下短期会话历史状态与长期记忆协同工作。"""
    container = build_container(Settings(provider_mode="fake"))
    capturing_llm = E2ECapturingLLM(response_text="多轮回复")
    container.llm = capturing_llm
    container.runner._llm = capturing_llm

    await container.memory_store.remember_profile(
        user_id="u",
        category="preference",
        key="currency",
        content="USDC",
    )

    app = _app_with(container)

    async with _api(app) as client:
        sess_resp = await client.post(
            "/v1/agent/sessions",
            json={"modality": "text"},
            headers=AUTH_HEADER,
        )
        session_id = sess_resp.json()["session_id"]

        # 第一轮
        resp1 = await client.post(
            "/v1/agent/messages",
            json={"session_id": session_id, "text": "第一轮问询"},
            headers=AUTH_HEADER,
        )
        assert resp1.status_code == 200
        sess1 = await container.session_store.get(session_id)
        assert sess1 is not None
        time1 = sess1.last_activity_at

        # 第二轮
        resp2 = await client.post(
            "/v1/agent/messages",
            json={"session_id": session_id, "text": "第二轮问询"},
            headers=AUTH_HEADER,
        )
        assert resp2.status_code == 200
        sess2 = await container.session_store.get(session_id)
        assert sess2 is not None
        assert sess2.last_activity_at >= time1
        assert "USDC" in (capturing_llm.last_system_prompt or "")
        assert "currency" in (capturing_llm.last_system_prompt or "")
