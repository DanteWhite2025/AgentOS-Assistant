"""文本对话 WebSocket 测试（starlette TestClient）。"""

from __future__ import annotations

from datetime import UTC, datetime

from starlette.testclient import TestClient

from app.api.ws import _turn_idempotency_key
from app.main import app

USER_HEADERS = {"Authorization": "Bearer test-user:u1"}
OTHER_USER_HEADERS = {"Authorization": "Bearer test-user:u2"}


class _RecordingRunner:
    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self.user_bearer_tokens: list[str | None] = []

    async def run_turn(self, *args, user_bearer_token=None, **kwargs):
        self.user_bearer_tokens.append(user_bearer_token)
        return await self._delegate.run_turn(
            *args,
            user_bearer_token=user_bearer_token,
            **kwargs,
        )

    async def run_turn_stream(self, *args, user_bearer_token=None, **kwargs):
        # WS 走的是流式入口，包装器两个都要转发，否则这层记录会把 WS 打断。
        self.user_bearer_tokens.append(user_bearer_token)
        async for event in self._delegate.run_turn_stream(
            *args,
            user_bearer_token=user_bearer_token,
            **kwargs,
        ):
            yield event


def test_ws_text_flow_with_tool() -> None:
    with TestClient(app) as client:
        body = client.post(
            "/v1/agent/sessions", json={"modality": "text"}, headers=USER_HEADERS
        ).json()
        sid, rt = body["session_id"], body["rt_token"]

        with client.websocket_connect(f"/v1/agent/stream?session_id={sid}&token={rt}") as ws:
            first = ws.receive_json()
            assert first["type"] == "session.state"

            ws.send_json({"type": "user.message", "data": {"text": "帮我看下持仓"}})
            events = []
            for _ in range(40):
                evt = ws.receive_json()
                events.append(evt)
                if evt["type"] == "response.text.done":
                    break
            seen = [e["type"] for e in events]
            assert "response.started" in seen
            assert "tool.invoked" in seen
            assert "response.text.delta" in seen
            assert "response.text.done" in seen

            # tool.invoked 现在是执行前实时推送的（见 AgentRunner.on_tool_start），
            # 不能再和 run_turn 跑完后的批量补发重复：每个工具恰好一条 invoked。
            invoked = [e for e in events if e["type"] == "tool.invoked"]
            assert len(invoked) == 1
            assert invoked[0]["data"]["name"] == "get_positions"
            assert invoked[0]["data"]["call_id"]

            results = [e for e in events if e["type"] == "tool.result"]
            assert len(results) == 1
            assert results[0]["data"]["call_id"] == invoked[0]["data"]["call_id"]

            # tool.invoked 必须先于 tool.result 到达（先声明"正在调用"，再报完成）。
            assert events.index(invoked[0]) < events.index(results[0])


def test_ws_forwards_matching_mobile_user_token_to_runner() -> None:
    with TestClient(app) as client:
        body = client.post(
            "/v1/agent/sessions", json={"modality": "text"}, headers=USER_HEADERS
        ).json()
        sid, rt = body["session_id"], body["rt_token"]
        recording = _RecordingRunner(client.app.state.container.runner)
        client.app.state.container.runner = recording

        with client.websocket_connect(
            f"/v1/agent/stream?session_id={sid}&token={rt}",
            headers=USER_HEADERS,
        ) as ws:
            assert ws.receive_json()["type"] == "session.state"
            ws.send_json({"type": "user.message", "data": {"text": "帮我看下持仓"}})
            for _ in range(40):
                if ws.receive_json()["type"] == "response.text.done":
                    break

        assert recording.user_bearer_tokens == ["test-user:u1"]


def test_ws_rejects_mobile_user_token_for_another_session_owner() -> None:
    with TestClient(app) as client:
        body = client.post(
            "/v1/agent/sessions", json={"modality": "text"}, headers=USER_HEADERS
        ).json()
        with client.websocket_connect(
            f"/v1/agent/stream?session_id={body['session_id']}&token={body['rt_token']}",
            headers=OTHER_USER_HEADERS,
        ) as ws:
            event = ws.receive_json()
            assert event["type"] == "error"
            assert event["data"]["code"] == "UNAUTHORIZED"


def test_ws_tool_invoked_push_failure_does_not_crash_turn(monkeypatch) -> None:
    """tool.invoked 实时推送失败（客户端已断线等）不能把异常炸穿 run_turn，

    否则会命中 run_turn 外层 except 分支里未加保护的二次 _send，
    重演 fix(ws) 修过的"异常处理路径本身再抛一次"的坑。
    """
    from fastapi import WebSocket

    original_send_json = WebSocket.send_json

    async def flaky_send_json(self, data):
        if data.get("type") == "tool.invoked":
            raise RuntimeError("simulated broken pipe")
        return await original_send_json(self, data)

    monkeypatch.setattr(WebSocket, "send_json", flaky_send_json)

    with TestClient(app) as client:
        body = client.post(
            "/v1/agent/sessions", json={"modality": "text"}, headers=USER_HEADERS
        ).json()
        sid, rt = body["session_id"], body["rt_token"]

        with client.websocket_connect(f"/v1/agent/stream?session_id={sid}&token={rt}") as ws:
            ws.receive_json()  # session.state
            ws.send_json({"type": "user.message", "data": {"text": "帮我看下持仓"}})
            seen = []
            for _ in range(40):
                evt = ws.receive_json()
                seen.append(evt["type"])
                if evt["type"] == "response.text.done":
                    break

            # tool.invoked 被吞掉了（模拟推送失败），但 turn 仍然正常跑完到底，
            # 连接没有被异常打断。
            assert "tool.invoked" not in seen
            assert "tool.result" in seen
            assert "response.text.done" in seen


def test_ws_ping_pong() -> None:
    with TestClient(app) as client:
        body = client.post(
            "/v1/agent/sessions", json={"modality": "text"}, headers=USER_HEADERS
        ).json()
        sid, rt = body["session_id"], body["rt_token"]
        with client.websocket_connect(f"/v1/agent/stream?session_id={sid}&token={rt}") as ws:
            ws.receive_json()  # session.state
            ws.send_json({"type": "ping", "data": {}})
            assert ws.receive_json()["type"] == "pong"


def test_ws_bad_token_rejected() -> None:
    with (
        TestClient(app) as client,
        client.websocket_connect("/v1/agent/stream?session_id=nope&token=bad") as ws,
    ):
        evt = ws.receive_json()
        assert evt["type"] == "error"
        assert evt["data"]["code"] == "UNAUTHORIZED"


def test_ws_rejects_rt_token_with_wrong_canonical_user() -> None:
    with TestClient(app) as client:
        body = client.post(
            "/v1/agent/sessions", json={"modality": "text"}, headers=USER_HEADERS
        ).json()
        # The in-memory store is async; issue a correctly signed token directly
        # against the known session id but with a different canonical USER.
        wrong, _ = client.app.state.container.token_service.issue_rt_token(
            body["session_id"], "u2", (), datetime.now(UTC)
        )
        with client.websocket_connect(
            f"/v1/agent/stream?session_id={body['session_id']}&token={wrong}"
        ) as ws:
            evt = ws.receive_json()
            assert evt["type"] == "error"
            assert evt["data"]["code"] == "UNAUTHORIZED"


# ---- 工具调用的实时展示。Dante 的需求：「思维链也展示下 Agent 正在调用的
# 工具，像 ChatGPT 那样」。tool.invoked 早就在实时推了，缺的是人话文案，
# 以及一条只在成功时才发、status 还硬编码成 "ok" 的 tool.result。 ----


def _drain_turn(ws, text: str, limit: int = 60) -> list[dict]:
    """收完一轮的事件。

    error 也当终止条件：这一轮出错时不会再有 response.text.done，只等它会让
    测试挂死在 receive_json 上，报出来的是超时而不是"这轮报错了"。
    """

    ws.send_json({"type": "user.message", "data": {"text": text}})
    events = []
    for _ in range(limit):
        evt = ws.receive_json()
        events.append(evt)
        if evt["type"] in ("response.text.done", "error"):
            break
    return events


def _open_turn(client):
    body = client.post(
        "/v1/agent/sessions", json={"modality": "text"}, headers=USER_HEADERS
    ).json()
    return client.websocket_connect(
        f"/v1/agent/stream?session_id={body['session_id']}&token={body['rt_token']}"
    )


def test_tool_invoked_carries_human_readable_title_and_params() -> None:
    """前端不该拿到 get_positions 这种函数名去自己猜中文。"""
    with TestClient(app) as client, _open_turn(client) as ws:
        assert ws.receive_json()["type"] == "session.state"
        events = _drain_turn(ws, "帮我看下持仓")

    invoked = next(e for e in events if e["type"] == "tool.invoked")
    assert invoked["data"]["title"] == "查询持仓"
    assert invoked["data"]["title_key"] == "tool.get_positions.running"
    assert "params" in invoked["data"]
    assert invoked["data"]["name"] == "get_positions", "原始工具名保留，便于埋点"


def test_tool_result_reports_real_status_not_a_hardcoded_ok() -> None:
    with TestClient(app) as client, _open_turn(client) as ws:
        assert ws.receive_json()["type"] == "session.state"
        events = _drain_turn(ws, "帮我看下持仓")

    result = next(e for e in events if e["type"] == "tool.result")
    assert result["data"]["status"] == "ok"
    assert result["data"]["name"] == "get_positions"


def test_tool_result_arrives_before_the_answer_text() -> None:
    """实时下发的意义就在这：过去 tool.result 是整轮跑完后批量补的，
    和答案文本几乎同时到达，前端根本没机会显示中间过程。"""
    with TestClient(app) as client, _open_turn(client) as ws:
        assert ws.receive_json()["type"] == "session.state"
        events = _drain_turn(ws, "帮我看下持仓")

    types = [e["type"] for e in events]
    assert types.index("tool.result") < types.index("response.text.delta")


def _isolated_app(executor):
    """建一条独立的 app + container。

    app.main.app 的 container 是模块级共享的，别的测试文件替换过它的 runner
    且不还原；靠 getattr 探测包装层去穿透是侥幸——一旦拿错对象，executor 就没
    被替换，工具秒回，这条用例会挂死在等一条永远不来的消息上。
    """

    from fastapi import FastAPI

    from app.api.agent import router as agent_router
    from app.api.ws import router as ws_router
    from app.config import Settings
    from app.container import build_container

    container = build_container(Settings(redis_url=""))
    container.runner._executor = executor
    isolated = FastAPI()
    isolated.state.container = container
    isolated.include_router(agent_router, prefix="/v1/agent")
    isolated.include_router(ws_router)
    return isolated


def test_interrupt_closes_out_in_flight_tools() -> None:
    """用户中断时，已经发过 tool.invoked 的调用必须补一条终态——
    否则 App 上那几行「正在查询…」会一直转到会话结束。"""
    import asyncio

    class _HangingExecutor:
        async def execute(self, tool_name, user_id, args, scope_tools, **kwargs):
            # 睡得刚好够长，让 interrupt 能在工具执行中途到达；又足够短，
            # 万一 cancel 没生效也只是这条用例断言失败，不会拖垮整个测试套件。
            await asyncio.sleep(2)
            return {}

    with TestClient(_isolated_app(_HangingExecutor())) as client:
        body = client.post(
            "/v1/agent/sessions", json={"modality": "text"}, headers=USER_HEADERS
        ).json()
        with client.websocket_connect(
            f"/v1/agent/stream?session_id={body['session_id']}&token={body['rt_token']}"
        ) as ws:
            assert ws.receive_json()["type"] == "session.state"
            ws.send_json({"type": "user.message", "data": {"text": "帮我看下持仓"}})

            # 等到工具已经开始执行（invoked 已发），此时它卡在 executor 里
            invoked = None
            for _ in range(10):
                evt = ws.receive_json()
                if evt["type"] == "tool.invoked":
                    invoked = evt
                    break
            assert invoked is not None

            ws.send_json({"type": "user.interrupt", "data": {}})
            cancelled = None
            for _ in range(10):
                evt = ws.receive_json()
                if evt["type"] == "tool.result":
                    cancelled = evt
                    break

    assert cancelled is not None, "中断后必须给在途工具补终态"
    assert cancelled["data"]["status"] == "cancelled"
    assert cancelled["data"]["call_id"] == invoked["data"]["call_id"]


# ---- 真流式。改造前 ws 把完整答案切成 8 字一段假装流式（循环里连 sleep 都
# 没有），首字延迟等于整轮耗时。现在 delta 来自模型的真实 token 流。 ----


class _StreamingRunner:
    """按脚本吐增量的 runner 替身，可选在中途抛异常。"""

    def __init__(self, chunks, *, raise_after: int | None = None, exc=None) -> None:
        self._chunks = chunks
        self._raise_after = raise_after
        self._exc = exc
        self.closed = False
        self.calls = []

    async def run_turn(self, *args, **kwargs):
        raise AssertionError("WS 应该走 run_turn_stream")

    async def run_turn_stream(self, session, text, trace_id, now, **kwargs):
        from app.agents.runner import RunResult, TurnDone, TurnTextDelta

        self.calls.append(kwargs)
        try:
            for index, chunk in enumerate(self._chunks):
                if self._raise_after is not None and index == self._raise_after:
                    raise self._exc
                yield TurnTextDelta(delta=chunk)
            yield TurnDone(
                result=RunResult(text="".join(self._chunks), executed_tools=())
            )
        finally:
            self.closed = True


def test_ws_derives_stable_turn_idempotency_key_from_mobile_message_id() -> None:
    runner = _StreamingRunner(("交易意图已生成",))
    with TestClient(app) as client:
        container, original = _with_runner(client, runner)
        try:
            body = client.post(
                "/v1/agent/sessions", json={"modality": "text"}, headers=USER_HEADERS
            ).json()
            session_id = body["session_id"]
            with client.websocket_connect(
                f"/v1/agent/stream?session_id={session_id}&token={body['rt_token']}"
            ) as ws:
                assert ws.receive_json()["type"] == "session.state"
                ws.send_json(
                    {
                        "type": "user.message",
                        "data": {"text": "创建 BTC 交易", "client_msg_id": "mobile-msg-123"},
                    }
                )
                for _ in range(20):
                    if ws.receive_json()["type"] == "response.text.done":
                        break
        finally:
            container.runner = original

    assert len(runner.calls) == 1
    assert runner.calls[0]["idempotency_key"] == _turn_idempotency_key(
        "u1", session_id, "mobile-msg-123", "unused-fallback"
    )


def _with_runner(client, runner):
    """把 container 上的 runner 换掉，返回还原用的原值。"""
    container = client.app.state.container
    original = container.runner
    container.runner = runner
    return container, original


def test_deltas_come_from_the_model_not_from_slicing() -> None:
    """delta 边界应当是模型给的，而不是每 8 个字符切一刀。"""
    runner = _StreamingRunner(("你当前", "持有 1.2 ETH，", "较昨日上涨"))
    with TestClient(app) as client:
        container, original = _with_runner(client, runner)
        try:
            with _open_turn(client) as ws:
                assert ws.receive_json()["type"] == "session.state"
                events = _drain_turn(ws, "帮我看下持仓")
        finally:
            container.runner = original

    deltas = [e["data"]["delta"] for e in events if e["type"] == "response.text.delta"]
    assert deltas == ["你当前", "持有 1.2 ETH，", "较昨日上涨"]
    assert runner.closed, "一轮结束后必须关闭生成器"


def test_done_text_is_the_accumulated_deltas() -> None:
    """done 里的文本必须等于已经吐出去的 delta 拼接。

    模型在调工具前说的前言进 delta 流但不进 RunResult.text——直接用后者会让
    客户端拿最终答案覆盖掉已经显示的前言。
    """
    runner = _StreamingRunner(("我查一下行情。", "BTC 现在 92,400。"))
    with TestClient(app) as client:
        container, original = _with_runner(client, runner)
        try:
            with _open_turn(client) as ws:
                assert ws.receive_json()["type"] == "session.state"
                events = _drain_turn(ws, "BTC 多少钱")
        finally:
            container.runner = original

    deltas = [e["data"]["delta"] for e in events if e["type"] == "response.text.delta"]
    done = next(e for e in events if e["type"] == "response.text.done")
    assert done["data"]["text"] == "".join(deltas)


def test_mid_stream_failure_closes_the_message_before_erroring() -> None:
    """已经吐了半截 delta 才炸：先把这条消息收尾，再发 error。

    7 个 except 分支原先都假设「异常发生时一个字都没发出去」。流式下不补
    response.text.done 的话，客户端那条消息会永远停在「正在输入」状态。
    """
    runner = _StreamingRunner(
        ("已经吐出去的半句话", "永远到不了"),
        raise_after=1,
        exc=ValueError("上游炸了"),
    )
    with TestClient(app) as client:
        container, original = _with_runner(client, runner)
        try:
            with _open_turn(client) as ws:
                assert ws.receive_json()["type"] == "session.state"
                # 这一轮 done 和 error 都会到，_drain_turn 收到 done 就停，
                # 所以这里自己收到 error 为止。
                ws.send_json({"type": "user.message", "data": {"text": "随便聊聊"}})
                events = []
                for _ in range(20):
                    evt = ws.receive_json()
                    events.append(evt)
                    if evt["type"] == "error":
                        break
        finally:
            container.runner = original

    types = [e["type"] for e in events]
    assert "response.text.delta" in types, "中断前的内容确实发出去了"
    assert "response.text.done" in types, "必须补一条 done 把消息收尾"
    assert "error" in types
    assert types.index("response.text.done") < types.index("error"), (
        "先收尾再报错，否则客户端先看到错误、那条消息还挂在输入中"
    )
    done = next(e for e in events if e["type"] == "response.text.done")
    assert done["data"]["text"] == "已经吐出去的半句话", "收尾文本是已经吐出的部分"


def test_failure_before_any_delta_does_not_emit_an_empty_done() -> None:
    """一个字都没吐就失败时不该补 done——那会让客户端显示一条空消息。"""
    runner = _StreamingRunner(("永远到不了",), raise_after=0, exc=ValueError("上游炸了"))
    with TestClient(app) as client:
        container, original = _with_runner(client, runner)
        try:
            with _open_turn(client) as ws:
                assert ws.receive_json()["type"] == "session.state"
                events = _drain_turn(ws, "随便聊聊")
        finally:
            container.runner = original

    types = [e["type"] for e in events]
    assert "response.text.delta" not in types
    assert "response.text.done" not in types
    assert "error" in types


def test_unexpected_error_mid_stream_also_closes_the_message() -> None:
    """兜底 except 分支同样要先收尾再报错。

    回归：给 7 个分支批量插收尾时用的模式是「except X: 后面紧跟 await _send」，
    而 `except Exception:` 的第一行是 logger.exception，正好没被匹配到。更糟的是
    另外两个中断用例都抛 ValueError——那是已命名的分支，把这个缺口盖过去了。

    兜底分支恰恰是最需要它的：命名分支覆盖的是已知的上游错误，而任何没预料到的
    故障都落在这里，那条消息会永远停在「正在输入」。
    """
    runner = _StreamingRunner(
        ("已经吐出去的半句话", "永远到不了"),
        raise_after=1,
        exc=RuntimeError("没预料到的故障"),
    )
    with TestClient(app) as client:
        container, original = _with_runner(client, runner)
        try:
            with _open_turn(client) as ws:
                assert ws.receive_json()["type"] == "session.state"
                ws.send_json({"type": "user.message", "data": {"text": "随便聊聊"}})
                events = []
                for _ in range(20):
                    evt = ws.receive_json()
                    events.append(evt)
                    if evt["type"] == "error":
                        break
        finally:
            container.runner = original

    types = [e["type"] for e in events]
    assert "response.text.done" in types, "兜底分支同样要补 done"
    assert types.index("response.text.done") < types.index("error")
    done = next(e for e in events if e["type"] == "response.text.done")
    assert done["data"]["text"] == "已经吐出去的半句话"


def test_ws_audio_requires_explicit_read_aloud_opt_in() -> None:
    from app.voice.pipeline_provider import VoicePipeline

    class RecordingTTS:
        def __init__(self):
            self.texts = []

        async def stream_synthesize(self, text, **kwargs):
            self.texts.append(text)
            yield b"test-audio"

    # Absence, false, and a truthy string must all remain silent.
    for preference in (None, False, "true", True):
        with TestClient(app) as client:
            tts = RecordingTTS()
            client.app.state.container.voice_pipeline = VoicePipeline(tts)
            body = client.post(
                "/v1/agent/sessions", json={"modality": "text"}, headers=USER_HEADERS
            ).json()
            with client.websocket_connect(
                f"/v1/agent/stream?session_id={body['session_id']}&token={body['rt_token']}"
            ) as ws:
                assert ws.receive_json()["type"] == "session.state"
                data = {"text": "帮我看下持仓"}
                if preference is not None:
                    data["read_aloud"] = preference
                ws.send_json({"type": "user.message", "data": data})
                events = []
                for _ in range(80):
                    event = ws.receive_json()
                    events.append(event)
                    if event["type"] == "avatar.event" and event["data"].get("value") == "Idle":
                        break
                types = [event["type"] for event in events]
                assert "response.text.delta" in types
                assert "response.text.done" in types
                assert bool(tts.texts) is (preference is True)
                assert ("audio.chunk" in types) is (preference is True)
                assert ("audio.done" in types) is (preference is True)
