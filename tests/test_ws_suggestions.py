"""追问以 response.suggestions 下发，且不重复留在正文里。

约束来自 PR #78：摘要与正文必须继续随 response.text.delta 流式吐出，不能为了
拿到结构化追问就等整段生成完——那等于把刚打磨好的首字延迟赔回去。所以追问是
边流边从尾部剥的，正文该什么时候到还是什么时候到。
"""

from __future__ import annotations

from starlette.testclient import TestClient

from app.main import app
from app.runtime.suggestion_split import MARKER

USER_HEADERS = {"Authorization": "Bearer test-user:u1"}


class _ScriptedRunner:
    def __init__(self, chunks: tuple[str, ...]) -> None:
        self._chunks = chunks

    async def run_turn(self, *args, **kwargs):
        raise AssertionError("WS 应该走 run_turn_stream")

    async def run_turn_stream(self, session, text, trace_id, now, **kwargs):
        from app.agents.runner import RunResult, TurnDone, TurnTextDelta

        for chunk in self._chunks:
            yield TurnTextDelta(delta=chunk)
        yield TurnDone(result=RunResult(text="".join(self._chunks), executed_tools=()))


def _run(chunks: tuple[str, ...]) -> list[dict]:
    runner = _ScriptedRunner(chunks)
    with TestClient(app) as client:
        container = client.app.state.container
        original = container.runner
        container.runner = runner
        try:
            body = client.post(
                "/v1/agent/sessions", json={"modality": "text"}, headers=USER_HEADERS
            ).json()
            url = f"/v1/agent/stream?session_id={body['session_id']}&token={body['rt_token']}"
            with client.websocket_connect(url) as ws:
                assert ws.receive_json()["type"] == "session.state"
                ws.send_json({"type": "user.message", "data": {"text": "BTC 怎么样"}})
                events: list[dict] = []
                for _ in range(60):
                    evt = ws.receive_json()
                    events.append(evt)
                    if evt["type"] in ("response.text.done", "error"):
                        break
                return events
        finally:
            container.runner = original


def _text(events: list[dict]) -> str:
    return "".join(
        e["data"]["delta"] for e in events if e["type"] == "response.text.delta"
    )


def test_suggestions_are_pushed_as_their_own_event() -> None:
    events = _run((f"BTC 走弱。{MARKER}\n1. 资金费率呢？\n2. 要看持仓吗？\n3. 平仓价位？",))

    payload = next(e for e in events if e["type"] == "response.suggestions")["data"]
    assert payload["questions"] == ["资金费率呢？", "要看持仓吗？", "平仓价位？"]
    assert payload["response_id"]


def test_suggestions_do_not_also_appear_in_the_body() -> None:
    """否则用户会看到同样三句话出现两遍：一遍在正文尾巴，一遍在 chips。"""
    events = _run((f"BTC 走弱。{MARKER}\n1. 资金费率呢？\n2. 要看持仓吗？\n3. 平仓价位？",))

    body = _text(events)
    assert body == "BTC 走弱。"
    assert MARKER not in body
    assert "资金费率呢？" not in body


def test_done_text_still_equals_the_delta_sum() -> None:
    """ws-events.md 承诺 done.text == 全部 delta 拼接，客户端拿它做校验。

    剥离追问不能破坏这条契约——剥掉的部分两边都不能出现。
    """
    events = _run((f"结论。{MARKER}\n1. 甲\n2. 乙\n3. 丙",))

    done = next(e for e in events if e["type"] == "response.text.done")
    assert done["data"]["text"] == _text(events)


def test_suggestions_land_before_the_message_is_closed() -> None:
    """chips 要跟着那条消息一起渲染，所以必须在 done 之前到。"""
    events = _run((f"结论。{MARKER}\n1. 甲\n2. 乙\n3. 丙",))

    types = [e["type"] for e in events]
    assert types.index("response.suggestions") < types.index("response.text.done")


def test_no_marker_means_no_suggestions_event() -> None:
    """模型没给追问就不发空事件——前端少一个「questions 是空数组」的分支。"""
    events = _run(("BTC 走弱，当前 92,400。",))

    assert not [e for e in events if e["type"] == "response.suggestions"]
    assert _text(events) == "BTC 走弱，当前 92,400。"


def test_marker_split_across_deltas_still_works_end_to_end() -> None:
    """真实 token 流会把标记切碎，这是常态。"""
    events = _run(("结论。\n\n【你可能", "还想", "问】\n1. 甲\n2. 乙\n3. 丙"))

    payload = next(e for e in events if e["type"] == "response.suggestions")["data"]
    assert payload["questions"] == ["甲", "乙", "丙"]
    assert _text(events) == "结论。"


def test_unparsable_tail_degrades_into_the_body_without_breaking_the_turn() -> None:
    """解析失败时不发事件，内容回正文，主流程照常收尾——不能中断这一轮。"""
    prose = (
        "这不是列表，是模型没有遵守格式、直接把正文最后一段接着写在了标记后面，"
        "所以它整段都应该回到正文里，绝不能被吞掉或者渲染成一个巨大的 chip。"
    )
    events = _run((f"结论。{MARKER}{prose}",))

    assert not [e for e in events if e["type"] == "response.suggestions"]
    assert prose in _text(events)
    assert any(e["type"] == "response.text.done" for e in events)


def test_streaming_is_not_delayed_until_the_end() -> None:
    """正文要边流边到，不能被攒到最后一起发。

    这是本次改造最容易踩坏的地方：为了拿到完整追问而缓冲整段，PR #78 的首字
    延迟就白做了。
    """
    events = _run(("第一段。", "第二段。", f"{MARKER}\n1. 甲\n2. 乙\n3. 丙"))

    deltas = [e["data"]["delta"] for e in events if e["type"] == "response.text.delta"]
    assert deltas == ["第一段。", "第二段。"], "两段要分两条到，不能合并成一条"


def test_thinking_done_still_lands_before_the_first_delta() -> None:
    """PR #78 的时序不能被这次改动挪位。"""
    events = _run((f"结论。{MARKER}\n1. 甲",))

    types = [e["type"] for e in events]
    assert types.index("response.thinking.done") < types.index("response.text.delta")
