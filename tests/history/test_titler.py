"""会话标题生成。

契约是「尽力而为」：生成不出来就什么都不做，行里已经有写时初始化的首句
截断值。绝不为了写标题而抛错——它挂在回合的 fire-and-forget 任务里，
抛出去只会变成一条无人接收的 task exception。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app.history.store import InMemoryConversationStore
from app.history.titler import ConversationTitler

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
# 建行首句。必须与 generate() 的入参是同一个值 —— titler 用
# fallback_title(user_text) 作为守卫，传不一样的值会让守卫不匹配、
# 整个用例静默空转（本次实测踩到：4 个断言「标题未变」的用例因此
# 变成永远为真）。
FIRST = "帮我看看比特币最近怎么样"


class _LLM:
    """按脚本回话的 LLM 替身。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0
        self.systems: list[str] = []

    async def complete(self, system, messages, tools):
        self.calls += 1
        self.systems.append(system)
        return type("R", (), {"text": self.text})()


class _Boom:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def complete(self, system, messages, tools):
        raise self.exc


async def _seeded() -> InMemoryConversationStore:
    store = InMemoryConversationStore()
    await store.record_turn("sess_1", "u1", FIRST, "答", NOW)
    return store


async def test_generates_and_writes_title() -> None:
    store = await _seeded()
    llm = _LLM('{"title": "BTC行情分析"}')

    await ConversationTitler(llm, store).generate("sess_1", FIRST, "答")

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.title == "BTC行情分析"


async def test_tolerates_code_fence_and_pleasantries() -> None:
    """模型爱把 JSON 包在 ```json 围栏或客套话里，只取最外层花括号块。"""
    store = await _seeded()
    llm = _LLM('好的，这是结果：\n```json\n{"title": "ETH质押收益"}\n```\n希望有帮助！')

    await ConversationTitler(llm, store).generate("sess_1", FIRST, "答")

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.title == "ETH质押收益"


@pytest.mark.parametrize(
    "reply",
    [
        "",  # max_tokens 被隐藏推理吃光时网关会安静地回空串
        "这不是 JSON",
        "{不是合法 JSON}",
        '{"title": ""}',  # 空标题会让列表项没有主文案
        '{"title": "   "}',
        '{"other": "x"}',
        "[1, 2, 3]",  # 合法 JSON 但不是对象
    ],
)
async def test_unusable_reply_keeps_fallback_title(reply: str) -> None:
    store = await _seeded()
    original = (await store.get_conversation("sess_1")).title

    await ConversationTitler(_LLM(reply), store).generate("sess_1", FIRST, "答")

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.title == original, "不可用的回复不该改动已有标题"


async def test_llm_failure_keeps_fallback_title() -> None:
    store = await _seeded()
    original = (await store.get_conversation("sess_1")).title

    await ConversationTitler(_Boom(RuntimeError("网关 502")), store).generate("sess_1", FIRST, "答")

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.title == original


async def test_cancellation_propagates() -> None:
    """容器关停走的是这条路，不能被当成普通失败咽掉。"""
    store = await _seeded()

    with pytest.raises(asyncio.CancelledError):
        await ConversationTitler(_Boom(asyncio.CancelledError()), store).generate(
            "sess_1", FIRST, "答"
        )


async def test_overlong_title_is_truncated() -> None:
    """title 列是 String(200)，模型不听话写一整段会直接写库失败。"""
    store = await _seeded()
    llm = _LLM('{"title": "' + "长" * 500 + '"}')

    await ConversationTitler(llm, store).generate("sess_1", FIRST, "答")

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert len(convo.title) <= 200


async def test_store_failure_is_swallowed() -> None:
    """回写失败不该冒泡——它是个无人接收的后台任务。"""

    class _BadStore(InMemoryConversationStore):
        async def set_title(self, session_id, title, *, only_if=None) -> None:
            raise RuntimeError("pg 挂了")

    store = _BadStore()
    await store.record_turn("sess_1", "u1", FIRST, "答", NOW)

    await ConversationTitler(_LLM('{"title": "标题"}'), store).generate("sess_1", FIRST, "答")


async def test_prompt_carries_both_sides_of_the_turn() -> None:
    """只给问题的话，「帮我看看」这种开场白生不出有信息量的标题。"""
    store = await _seeded()
    llm = _LLM('{"title": "T"}')

    await ConversationTitler(llm, store).generate("sess_1", "比特币怎么样", "六万二，涨了 2%")

    assert llm.calls == 1


# ---------------------------------------------------------------- 不覆盖用户改名


async def test_generated_title_does_not_overwrite_user_rename() -> None:
    """真实竞态：titler 异步跑 1.5~3 分钟，用户可能在它返回前就改了名。

    二期加入重命名后才有这个问题——一期没有重命名，无条件覆盖是安全的。
    """
    store = await _seeded()  # 首句是「帮我看看比特币最近怎么样」
    await store.set_title("sess_1", "用户改的名字")

    await ConversationTitler(_LLM('{"title": "BTC行情分析"}'), store).generate(
        "sess_1", "帮我看看比特币最近怎么样", "答"
    )

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.title == "用户改的名字"


async def test_generated_title_upgrades_untouched_fallback() -> None:
    """没被改过时照常升级，守卫不能把正常路径也挡掉。"""
    store = await _seeded()

    await ConversationTitler(_LLM('{"title": "BTC行情分析"}'), store).generate(
        "sess_1", "帮我看看比特币最近怎么样", "答"
    )

    convo = await store.get_conversation("sess_1")
    assert convo is not None
    assert convo.title == "BTC行情分析"


async def test_guard_uses_the_same_truncation_as_the_store() -> None:
    """守卫值必须与建行时写入的降级值逐字一致。

    两处各算一次截断、规则稍有出入的话，守卫永远不匹配 —— 表现是标题永远
    升级不了，而且完全静默。所以 titler 与 store 共用 fallback_title。
    """
    store = await _seeded()
    long_text = "帮我详细分析一下比特币最近三个月的走势以及后续可能的支撑位和压力位"
    await store.record_turn("sess_2", "u1", long_text, "答", NOW)

    await ConversationTitler(_LLM('{"title": "BTC走势分析"}'), store).generate(
        "sess_2", long_text, "答"
    )

    convo = await store.get_conversation("sess_2")
    assert convo is not None
    assert convo.title == "BTC走势分析", "守卫值与建行降级值不一致，标题永远升级不了"
