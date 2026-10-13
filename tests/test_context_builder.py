"""ContextBuilder 上下文与记忆组装单元测试。"""

from __future__ import annotations

import pytest

from app.memory.store import InMemoryMemoryStore
from app.runtime.context_builder import ContextBuilder


@pytest.mark.asyncio
async def test_context_builder_prompt_injection() -> None:
    store = InMemoryMemoryStore()
    user_id = "user_prompt_test"

    # 写入画像与情景
    await store.remember_profile(
        user_id,
        category="preference",
        key="tokens",
        content=["BTC", "ETH"],
    )
    await store.remember_episode(
        user_id,
        summary="用户上次咨询了BTC减半行情",
    )

    builder = ContextBuilder(store)

    # 1. 组装 Prompt
    base_prompt = "你是 YesUP 数字人助手。"
    full_prompt = await builder.build_system_prompt(
        base_prompt,
        user_id=user_id,
        query="关于BTC减半行情怎么看？",
    )

    assert "你是 YesUP 数字人助手。" in full_prompt
    assert "<user_profile>" in full_prompt
    assert "[preference] tokens:" in full_prompt
    assert "<recalled_memory>" in full_prompt
    assert "用户上次咨询了BTC减半行情" in full_prompt


@pytest.mark.asyncio
async def test_context_builder_graceful_fallback() -> None:
    # 1. store 为 None 时原样返回
    builder_none = ContextBuilder(None)
    base_prompt = "基础提示词"
    result1 = await builder_none.build_system_prompt(base_prompt, user_id="user_1")
    assert result1 == base_prompt

    # 2. user_id 为 None 时原样返回
    store = InMemoryMemoryStore()
    builder = ContextBuilder(store)
    result2 = await builder.build_system_prompt(base_prompt, user_id=None)
    assert result2 == base_prompt

    # 3. 无匹配记忆时仅包含基础 prompt
    result3 = await builder.build_system_prompt(base_prompt, user_id="empty_user")
    assert result3 == base_prompt


async def test_voice_context_includes_the_current_session_text_turns() -> None:
    """刚打字聊完就打电话，语音那边不能什么都不知道。

    build_voice_context 原来只读 long-term episode，而文字聊天从不写 episode
    （只有语音挂断才写，见 app/api/voice.py），所以文字→语音一直是断片的。
    """

    from datetime import UTC, datetime

    from app.runtime.session import ConversationTurn

    turns = (
        ConversationTurn(
            "leopold的基金现在怎么样了",
            "Leopold Aschenbrenner 的基金…",
            datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC),
        ),
    )

    rendered = await ContextBuilder(None).build_voice_context("u1", turns=turns)

    assert "leopold的基金现在怎么样了" in rendered
    assert "Leopold Aschenbrenner 的基金" in rendered
    assert rendered.startswith("<recent_context>")


async def test_voice_context_without_turns_is_unchanged() -> None:
    """向后兼容：不传 turns 时行为与改造前完全一致（无记忆则空串）。"""

    assert await ContextBuilder(None).build_voice_context("u1") == ""
