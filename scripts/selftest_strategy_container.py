"""策略容器综合自测 —— 真实 app 全链路 + 边界风控（可复跑）。

跑真实 FastAPI app（httpx.ASGITransport，真路由/真容器/真策略存储与编排），覆盖：
  一、正向生命周期：创建 → 查询 → 暂停 → 恢复 → 建单草稿 → 终止
  二、边界风控：超额(400) / 越权(404) / 状态拦截(PAUSED·TERMINATED 建单 → 409)

策略 CRUD/生命周期/边界为纯 runtime 逻辑（离线确定性）。ACTIVE 下建单草稿：
  - 若设置 AGENT_CORE_ACTOR_TOKEN（或 .env 有）→ 挂真 AgentOsStagingClient 真打 staging，断言 201；
  - 否则 → 断言 503（真链路未配置）。意图解析用 StubLLM（LLM 非本脚本被测对象）。

用法：
  .venv/bin/python scripts/selftest_strategy_container.py            # 无 token：建单断言 503
  AGENT_CORE_ACTOR_TOKEN=xxx .venv/bin/python scripts/selftest_strategy_container.py  # 真打 staging
"""

from __future__ import annotations

import os

# 测试隔离（须在任何 app 导入前）：强制 fake + local，屏蔽开发者 .env 的 real/staging。
os.environ["AGENT_PROVIDER_MODE"] = "fake"
os.environ["AGENT_ENV"] = "local"

import asyncio
import sys

import httpx

from app.agents.llm import LLMResult
from app.api.deps import get_container
from app.config import get_settings
from app.container import build_container
from app.main import create_app
from app.tools.agentos.client import AgentOsStagingClient

STAGING_BASE = "https://api-staging.agentos-app.app"
A = {"X-User-Id": "user_A", "X-Agent-Id": "agent_A"}
B = {"X-User-Id": "user_B", "X-Agent-Id": "agent_B"}

_passed = 0
_failed = 0


def check(cond: bool, label: str, detail: str = "") -> None:
    global _passed, _failed
    _passed += cond
    _failed += not cond
    print(f"  {'✓' if cond else '✗'} {label}" + (f"  [{detail}]" if detail else ""))


class _StubLLM:
    async def complete(self, system, messages, tools) -> LLMResult:
        return LLMResult(
            text='{"symbol":"BTC","side":"BUY","quantity":"0.0002","limit_price":"50000","leverage":"1"}'
        )


def _token() -> str:
    tok = os.environ.get("AGENT_CORE_ACTOR_TOKEN", "")
    if not tok:
        try:
            tok = get_settings().core_actor_token
        except Exception:
            tok = ""
    return tok


def _body(**over):
    b = {
        "name": "趋势策略A", "template_id": "tpl_trend", "runtime_env": "paper",
        "allocation": "100000", "capital_capacity": "100000", "strategy_capacity": "200000",
        "max_leverage": "3",
    }
    b.update(over)
    return b


async def main() -> int:
    token = _token()
    have_token = bool(token)
    container = build_container(get_settings())
    container.llm = _StubLLM()
    if have_token:
        container.agentos_client = AgentOsStagingClient(
            STAGING_BASE, token=token, client=httpx.AsyncClient(timeout=20.0)
        )
    app = create_app()
    app.state.container = container
    app.dependency_overrides[get_container] = lambda: container

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30.0) as c:
        print(f"\n=== 一、正向端到端生命周期（token: {'有→真打 staging' if have_token else '无→断言 503'}）===")
        r = await c.post("/v1/agent/strategies", json=_body(), headers=A)
        check(r.status_code == 201 and r.json().get("state") == "ACTIVE", "创建策略 → 201 ACTIVE", str(r.status_code))
        sid = r.json().get("strategy_id", "")

        r = await c.get("/v1/agent/strategies", headers=A)
        check(r.status_code == 200 and any(s["strategy_id"] == sid for s in r.json()["items"]), "查询列表含该策略")
        check((await c.get(f"/v1/agent/strategies/{sid}", headers=A)).status_code == 200, "查询单个 → 200")

        r = await c.post(f"/v1/agent/strategies/{sid}/pause", headers=A)
        check(r.status_code == 200 and r.json()["state"] == "PAUSED", "暂停 → PAUSED")

        r = await c.post(f"/v1/agent/strategies/{sid}/trade-draft", json={"text": "买 0.0002 BTC 限价 50000"}, headers=A)
        check(r.status_code == 409, "【状态拦截】PAUSED 建单 → 409", f"{r.status_code}/{r.json().get('code')}")

        r = await c.post(f"/v1/agent/strategies/{sid}/resume", headers=A)
        check(r.status_code == 200 and r.json()["state"] == "ACTIVE", "恢复 → ACTIVE")

        r = await c.post(f"/v1/agent/strategies/{sid}/trade-draft", json={"text": "买 0.0002 BTC 限价 50000"}, headers=A)
        if have_token:
            draft = r.json().get("draft_id") if r.status_code == 201 else None
            check(r.status_code == 201, "ACTIVE 建单 → 201（真打 staging，裸币名 BTC）", f"{r.status_code} draft={draft or str(r.text)[:120]}")
        else:
            check(r.status_code == 503, "ACTIVE 建单 → 503（真链路未配置）", str(r.status_code))

        r = await c.post(f"/v1/agent/strategies/{sid}/terminate", headers=A)
        check(r.status_code == 200 and r.json()["state"] == "TERMINATED", "终止 → TERMINATED")

        r = await c.post(f"/v1/agent/strategies/{sid}/trade-draft", json={"text": "买"}, headers=A)
        check(r.status_code == 409, "【状态拦截】TERMINATED 建单 → 409", f"{r.status_code}/{r.json().get('code')}")

        print("\n=== 二、边界与风控拦截 ===")
        r = await c.post("/v1/agent/strategies", json=_body(allocation="600", capital_capacity="500"), headers=A)
        check(r.status_code == 400 and r.json()["code"] == "INVALID_REQUEST", "【超额】allocation>capital_capacity → 400", f"{r.status_code}/{r.json().get('code')}")

        sid_b = (await c.post("/v1/agent/strategies", json=_body(name="B的策略"), headers=B)).json()["strategy_id"]
        check((await c.get(f"/v1/agent/strategies/{sid_b}", headers=A)).status_code == 404, "【越权】A 查 B 的策略 → 404")
        check((await c.post(f"/v1/agent/strategies/{sid_b}/pause", headers=A)).status_code == 404, "【越权】A 暂停 B 的策略 → 404")
        check((await c.post(f"/v1/agent/strategies/{sid_b}/trade-draft", json={"text": "买"}, headers=A)).status_code == 404, "【越权】A 在 B 策略内建单 → 404")

    print(f"\n=== 自测结果：{_passed} 通过 / {_failed} 失败 ===")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
