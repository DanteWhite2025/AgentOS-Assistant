"""文本 WS 的多轮历史。

WS 是文本主路径，而它长连接期间用的是握手那一刻的 session 快照
（app/api/ws.py 外层闭包变量，_handle_turn 里没有 nonlocal）。Redis 的 update
是整体覆盖、无 CAS，所以拿旧快照写回去会把 runner 这一轮刚写进去的历史静默
冲掉——第二轮之后就永远只剩最后一轮，而且不报错。

这个测试卡住的就是它：一条连接连聊三轮，历史必须一轮不丢。
见 docs/plans/2026-09-04-text-turn-conversation-history/TECH_DESIGN.md §7。
"""

from __future__ import annotations

import asyncio

from starlette.testclient import TestClient

from app.main import app

USER_HEADERS = {"Authorization": "Bearer test-user:u1"}


def _drain_turn(ws, text: str, limit: int = 60) -> list[dict]:
    """收完一轮。error 也当终止条件，否则出错时会挂死在 receive_json 上。"""

    ws.send_json({"type": "user.message", "data": {"text": text}})
    events = []
    for _ in range(limit):
        evt = ws.receive_json()
        events.append(evt)
        if evt["type"] in ("response.text.done", "error"):
            break
    return events


def test_three_turns_on_one_connection_all_land_in_history() -> None:
    with TestClient(app) as client:
        body = client.post(
            "/v1/agent/sessions", json={"modality": "text"}, headers=USER_HEADERS
        ).json()
        session_id, rt_token = body["session_id"], body["rt_token"]
        container = client.app.state.container

        with client.websocket_connect(
            f"/v1/agent/stream?session_id={session_id}&token={rt_token}"
        ) as ws:
            assert ws.receive_json()["type"] == "session.state"
            for text in ("第一问", "第二问", "第三问"):
                events = _drain_turn(ws, text)
                assert events[-1]["type"] == "response.text.done", events[-1]

        saved = asyncio.run(container.session_store.get(session_id))

    assert saved is not None
    assert [t.user_text for t in saved.turns] == ["第一问", "第二问", "第三问"]


def test_a_later_turn_sees_what_the_earlier_one_said() -> None:
    """历史不只是存下来，还要真的回到下一轮的 prompt 里。"""

    seen_messages: list[list[tuple[str, str]]] = []

    with TestClient(app) as client:
        container = client.app.state.container
        original_llm = container.runner._llm

        class _RecordingLLM:
            async def complete(self, system, messages, tools):
                seen_messages.append([(m.role, m.content) for m in messages])
                return await original_llm.complete(system, messages, tools)

            async def stream(self, system, messages, tools):
                seen_messages.append([(m.role, m.content) for m in messages])
                async for event in original_llm.stream(system, messages, tools):
                    yield event

        container.runner._llm = _RecordingLLM()
        try:
            body = client.post(
                "/v1/agent/sessions", json={"modality": "text"}, headers=USER_HEADERS
            ).json()
            with client.websocket_connect(
                f"/v1/agent/stream?session_id={body['session_id']}&token={body['rt_token']}"
            ) as ws:
                assert ws.receive_json()["type"] == "session.state"
                _drain_turn(ws, "leopold的基金现在怎么样了")
                _drain_turn(ws, "他一季度具体做空了哪些股票？")
        finally:
            container.runner._llm = original_llm

    second_turn = seen_messages[-1]
    assert second_turn[0] == ("user", "leopold的基金现在怎么样了")
    assert second_turn[-1] == ("user", "他一季度具体做空了哪些股票？")
