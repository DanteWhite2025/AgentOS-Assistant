"""思考进度事件：阶段文案 + 首字耗时。

前端要显示「正在思考 → 正在整理结果 → 已思考 8 秒」这三段，其中：
- 阶段名（在干什么）只有服务端知道，前端造不出来，必须下发；
- 「已思考 N 秒」前端理论上能自己掐表，但流式首块可能是空的，
  客户端一收到就停表会算出 0.5 秒——比不显示更糟，所以耗时也由服务端定。
"""

from __future__ import annotations

from starlette.testclient import TestClient

from app.main import app

USER_HEADERS = {"Authorization": "Bearer test-user:u1"}


def _open_turn(client):
    body = client.post(
        "/v1/agent/sessions", json={"modality": "text"}, headers=USER_HEADERS
    ).json()
    sid, rt = body["session_id"], body["rt_token"]
    return client.websocket_connect(f"/v1/agent/stream?session_id={sid}&token={rt}")


def _drain(ws, text: str, limit: int = 60) -> list[dict]:
    ws.send_json({"type": "user.message", "data": {"text": text}})
    events: list[dict] = []
    for _ in range(limit):
        evt = ws.receive_json()
        events.append(evt)
        if evt["type"] in ("response.text.done", "error"):
            break
    return events


class _ScriptedRunner:
    """按脚本吐事件的 runner 替身。脚本元素是 runner 的事件对象工厂。"""

    def __init__(self, script) -> None:
        self._script = script

    async def run_turn(self, *args, **kwargs):
        raise AssertionError("WS 应该走 run_turn_stream")

    async def run_turn_stream(self, session, text, trace_id, now, **kwargs):
        for make in self._script:
            yield make()


def _with_runner(client, runner):
    container = client.app.state.container
    original = container.runner
    container.runner = runner
    return container, original


def _run(script, text: str = "BTC 多少钱") -> list[dict]:
    runner = _ScriptedRunner(script)
    with TestClient(app) as client:
        container, original = _with_runner(client, runner)
        try:
            with _open_turn(client) as ws:
                assert ws.receive_json()["type"] == "session.state"
                return _drain(ws, text)
        finally:
            container.runner = original


def _delta(text: str):
    from app.agents.runner import TurnTextDelta

    return lambda: TurnTextDelta(delta=text)


def _stage(name: str):
    from app.agents.runner import TurnStage

    return lambda: TurnStage(stage=name)


def _done(text: str):
    from app.agents.runner import RunResult, TurnDone

    return lambda: TurnDone(result=RunResult(text=text, executed_tools=()))


def test_stage_event_carries_server_rendered_copy_and_i18n_key() -> None:
    """阶段事件要带可直接显示的中文和 i18n key，沿用 tool.invoked 那套约定。"""
    events = _run([_stage("thinking"), _delta("BTC 92,400。"), _done("BTC 92,400。")])

    stages = [e for e in events if e["type"] == "response.stage"]
    assert stages, "必须下发 response.stage"
    data = stages[0]["data"]
    assert data["stage"] == "thinking"
    assert data["title"] == "正在思考"
    assert data["title_key"] == "stage.thinking"
    assert data["response_id"], "阶段要绑定到具体那一轮回复"


def test_composing_stage_fills_the_gap_between_tools_and_the_answer() -> None:
    """工具跑完到答案出来之间是一段空白，要有「正在整理结果」占位。"""
    events = _run(
        [
            _stage("thinking"),
            _stage("composing"),
            _delta("综合来看…"),
            _done("综合来看…"),
        ]
    )

    stages = [e["data"]["stage"] for e in events if e["type"] == "response.stage"]
    assert stages == ["thinking", "composing"]
    titles = {
        e["data"]["stage"]: e["data"]["title"]
        for e in events
        if e["type"] == "response.stage"
    }
    assert titles["composing"] == "正在整理结果"


def test_unknown_stage_degrades_to_a_generic_title_instead_of_breaking() -> None:
    """runner 将来新增阶段而文案表没跟上时，宁可显示一句通用文案，也不能让整轮挂掉。"""
    events = _run([_stage("brand_new_stage"), _delta("好"), _done("好")])

    stage = next(e for e in events if e["type"] == "response.stage")["data"]
    assert stage["stage"] == "brand_new_stage"
    assert stage["title"], "未知阶段也要有可显示的文案"
    assert stage["title_key"] == "stage.brand_new_stage"


def test_thinking_done_lands_before_the_first_delta() -> None:
    """「已思考 N 秒」要在第一个字之前定格——币安那个 UI 就是这个顺序。"""
    events = _run([_stage("thinking"), _delta("BTC"), _delta(" 92,400。"), _done("BTC 92,400。")])

    types = [e["type"] for e in events]
    assert "response.thinking.done" in types
    assert types.index("response.thinking.done") < types.index("response.text.delta")


def test_thinking_done_carries_elapsed_ms() -> None:
    events = _run([_delta("好"), _done("好")])

    payload = next(e for e in events if e["type"] == "response.thinking.done")["data"]
    assert isinstance(payload["elapsed_ms"], int)
    assert payload["elapsed_ms"] >= 0
    assert payload["response_id"]


def test_thinking_done_is_emitted_once_per_turn() -> None:
    """它是「首字延迟」的定格，不是每个 delta 都报一次。"""
    events = _run([_delta("一"), _delta("二"), _delta("三"), _done("一二三")])

    assert len([e for e in events if e["type"] == "response.thinking.done"]) == 1


def test_empty_delta_does_not_stop_the_thinking_clock() -> None:
    """空增量不算首字。

    provider 会发空块，客户端一收到就停表会算出「已思考 0 秒」；空块也不该
    渲染成一条文本增量。
    """
    events = _run([_delta(""), _delta(""), _delta("真正的第一个字"), _done("真正的第一个字")])

    types = [e["type"] for e in events]
    deltas = [e["data"]["delta"] for e in events if e["type"] == "response.text.delta"]
    assert deltas == ["真正的第一个字"], "空增量不该下发"
    assert types.index("response.thinking.done") < types.index("response.text.delta")


def test_turn_without_any_text_still_reports_thinking_done() -> None:
    """一个字都没吐出来的轮次（纯工具失败等）也要给个终态，
    否则前端那行「正在思考」会一直转。"""
    events = _run([_stage("thinking"), _done("")])

    assert any(e["type"] == "response.thinking.done" for e in events)


def test_speaking_switch_still_waits_for_a_real_character() -> None:
    """数字人切 Speaking 的判定同样不能被空块提前触发。"""
    events = _run([_delta(""), _delta("有字了"), _done("有字了")])

    avatar = [
        e["data"]["value"] for e in events if e["type"] == "avatar.event"
    ]
    types = [e["type"] for e in events]
    assert "Speaking" in avatar
    speaking_at = next(
        i for i, e in enumerate(events)
        if e["type"] == "avatar.event" and e["data"]["value"] == "Speaking"
    )
    assert speaking_at == types.index("response.text.delta") - 1 or speaking_at < types.index(
        "response.text.delta"
    )
