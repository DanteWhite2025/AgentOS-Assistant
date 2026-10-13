"""AgentOsStagingClient 冒烟测试 —— 验证「API 调用构造正确性」+ 可选真链路。

两段式（团队 2026-08-03 拍板）：
  A. MockTransport 段：无需 token，断言每个请求的 URL / header / body 完全符合 OpenAPI 契约。
  B. 真链路段：仅当设置 AGENT_CORE_ACTOR_TOKEN 时执行，打 staging 查询三件套。

用法：
  .venv/bin/python scripts/smoke_agentos_staging.py                 # 只跑 A（离线，必过）
  AGENT_CORE_ACTOR_TOKEN=xxx AGENTOS_TRADING_ACCOUNT_ID=acc_1 \\
    .venv/bin/python scripts/smoke_agentos_staging.py               # A + B
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools.agentos.client import AgentOsStagingClient
from app.tools.agentos.models import CreateTradeDraftRequest

STAGING_BASE = "https://api-staging.agentos-app.app"


def _check(cond: bool, msg: str) -> None:
    mark = "✓" if cond else "✗"
    print(f"  {mark} {msg}")
    if not cond:
        raise AssertionError(msg)


async def run_mock_segment() -> None:
    """A 段：用 MockTransport 拦截请求，逐一断言构造正确。"""
    print("[A] MockTransport 请求构造断言")
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"data": {}, "request_id": "req_test"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = AgentOsStagingClient(STAGING_BASE, token="TESTTOKEN", client=http)

        # 1) 查询余额：GET + query 参数 + Bearer
        await client.get_balances("acc_42")
        r = captured[-1]
        _check(r.method == "GET", "get_balances 用 GET")
        _check(r.url.path == "/internal/v1/balances", "路径 /internal/v1/balances")
        _check(r.url.params.get("trading_account_id") == "acc_42", "带 trading_account_id 查询参数")
        _check(r.headers.get("Authorization") == "Bearer TESTTOKEN", "带 Bearer token")

        # 2) 仓位 / 订单：无参 GET
        await client.get_positions()
        _check(captured[-1].url.path == "/internal/v1/positions", "positions 路径正确")
        await client.get_orders()
        _check(captured[-1].url.path == "/internal/v1/orders", "orders 路径正确")

        # 3) 构造草稿：POST + Idempotency-Key + 契约 body
        req = CreateTradeDraftRequest(
            unified_account_id="uacc_1",
            venue_account_id="vacc_1",
            symbol="ETH-USDC",
            side="BUY",
            quantity="1.5",
            limit_price="3000.00",
            leverage="2",
            max_slippage="0.005",
            reduce_only=False,
        )
        await client.create_trade_draft(req.to_body(), idempotency_key="idem_fixed")
        r = captured[-1]
        body = json.loads(r.content)
        _check(r.method == "POST", "create_trade_draft 用 POST")
        _check(r.url.path == "/internal/v1/trade-drafts", "草稿路径正确")
        _check(r.headers.get("Idempotency-Key") == "idem_fixed", "带 Idempotency-Key")
        _check(body["order_type"] == "LIMIT" and body["side"] == "BUY", "body 枚举字段正确")
        _check(body["quantity"] == "1.5" and body["limit_price"] == "3000.00", "Decimal 归一为字符串")
        _check("mandate_id" not in body, "未提供的 mandate_id 已剔除")

        # 4) 用户确认：POST + draft_version + X-Step-Up-Token
        await client.confirm_trade_draft("draft_9", 3, step_up_token="stepup_abc")
        r = captured[-1]
        _check(r.url.path == "/api/v1/trade-drafts/draft_9/confirm", "确认路径正确")
        _check(json.loads(r.content) == {"draft_version": 3}, "确认体只含 draft_version")
        _check(r.headers.get("X-Step-Up-Token") == "stepup_abc", "带 X-Step-Up-Token")

    # 5) 边界校验：非法枚举 / 非法 Decimal 必须抛
    for bad in (
        lambda: CreateTradeDraftRequest("u", "v", "S", "LONG", "1", "1", "1", "0"),
        lambda: CreateTradeDraftRequest("u", "v", "S", "BUY", "abc", "1", "1", "0"),
    ):
        try:
            bad()
        except ValueError:
            _check(True, "非法入参被拒绝")
        else:
            _check(False, "非法入参未被拒绝")
    print("[A] 通过\n")


async def run_live_segment() -> None:
    """B 段：真链路查询三件套（只读，安全）。"""
    token = os.environ.get("AGENT_CORE_ACTOR_TOKEN")
    if not token:
        print("[B] 跳过（未设置 AGENT_CORE_ACTOR_TOKEN）")
        return
    account_id = os.environ.get("AGENTOS_TRADING_ACCOUNT_ID", "")
    print("[B] 真链路只读查询")
    async with httpx.AsyncClient(timeout=15.0) as http:
        client = AgentOsStagingClient(STAGING_BASE, token=token, client=http)
        try:
            if account_id:
                bal = await client.get_balances(account_id)
                print("  balances:", json.dumps(bal, ensure_ascii=False)[:200])
            else:
                print("  (未设置 AGENTOS_TRADING_ACCOUNT_ID，跳过 balances)")
            print("  positions:", json.dumps(await client.get_positions(), ensure_ascii=False)[:200])
            print("  orders:", json.dumps(await client.get_orders(), ensure_ascii=False)[:200])
            print("[B] 通过\n")
        except httpx.HTTPStatusError as exc:
            print(f"  ✗ HTTP {exc.response.status_code}: {exc.response.text[:200]}")
            raise


async def main() -> None:
    await run_mock_segment()
    await run_live_segment()
    print("全部冒烟检查完成 ✅")


if __name__ == "__main__":
    asyncio.run(main())
