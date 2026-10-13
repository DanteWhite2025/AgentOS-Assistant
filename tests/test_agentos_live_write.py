"""真链路【写】端到端 —— 真打 staging 建一张交易草稿到 PENDING（副作用可控）。

与只读段（test_agentos_live.py）区别：这里真的在测试网**建草稿**，有真实副作用；
但只到 PENDING_CONFIRMATION（不 confirm 就不成交）。Runtime 不提供 confirm、执行或撤单能力。

甲方（AgentOS Core owner）已确认（2026-08-04）：token 有写权限、agent 已授权、建草稿需 mandate。

必需环境变量（缺任一即 skip）：
  AGENT_CORE_ACTOR_TOKEN      带写权限的 Agent Bearer JWT
  AGENTOS_UNIFIED_ACCOUNT_ID  统一账户 id（已知 uac_ff5a5ff0f4fb674d079f8e5f1e2fb5d7）
  AGENTOS_VENUE_ACCOUNT_ID    场所账户 id（★ 待甲方提供）
可选：
  AGENTOS_MANDATE_ID          用户自设额度授权 id（★ 若建草稿必须带，则填；否则留空试）
  AGENTOS_SYMBOL              交易对，默认 ETH-USDC（★ 以 staging 认可的为准）
  AGENTOS_QTY / AGENTOS_PRICE 下单量 / 限价，默认极小量试探约束
  AGENTOS_LEVERAGE / AGENTOS_MAX_SLIPPAGE

手动跑：
  AGENT_CORE_ACTOR_TOKEN=xxx AGENTOS_UNIFIED_ACCOUNT_ID=uac_... AGENTOS_VENUE_ACCOUNT_ID=vac_... \\
    .venv/bin/python -m pytest tests/test_agentos_live_write.py -m live -v -s
"""

from __future__ import annotations

import os

import httpx
import pytest

from app.tools.agentos.client import AgentOsStagingClient
from app.tools.agentos.models import CreateTradeDraftRequest

STAGING_BASE = "https://api-staging.agentos-app.app"


def _resolve_token() -> str:
    token = os.environ.get("AGENT_CORE_ACTOR_TOKEN", "")
    if not token:
        try:
            from app.config import get_settings

            token = get_settings().core_actor_token
        except Exception:  # pragma: no cover - 配置缺失视作无 token
            token = ""
    return token


_TOKEN = _resolve_token()
_UNIFIED = os.environ.get("AGENTOS_UNIFIED_ACCOUNT_ID", "")
_VENUE = os.environ.get("AGENTOS_VENUE_ACCOUNT_ID", "")

# 缺写链路必需的三要素之一即跳过（token / 统一账户 / 场所账户）
_READY = bool(_TOKEN and _UNIFIED and _VENUE)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _READY,
        reason="写链路真测需 AGENT_CORE_ACTOR_TOKEN + AGENTOS_UNIFIED_ACCOUNT_ID + AGENTOS_VENUE_ACCOUNT_ID",
    ),
]


async def test_live_create_trade_draft_to_pending():
    """真打 staging 建一张草稿，断言进入待确认态（不 confirm，不成交）。"""
    req = CreateTradeDraftRequest(
        unified_account_id=_UNIFIED,
        venue_account_id=_VENUE,
        instrument_id=os.environ.get(
            "AGENTOS_INSTRUMENT_ID", "perp_hyperliquid_testnet_eth"
        ),
        symbol=os.environ.get("AGENTOS_SYMBOL", "ETH"),
        side="BUY",
        quantity=os.environ.get("AGENTOS_QTY", "0.001"),  # 极小量，先探约束
        limit_price=os.environ.get("AGENTOS_PRICE", "10000"),  # 远离市价的低限价，避免误成交
        leverage=os.environ.get("AGENTOS_LEVERAGE", "1"),
        max_slippage=os.environ.get("AGENTOS_MAX_SLIPPAGE", "0.005"),
        mandate_id=os.environ.get("AGENTOS_MANDATE_ID") or None,
    )
    async with httpx.AsyncClient(timeout=15.0) as http:
        client = AgentOsStagingClient(STAGING_BASE, token=_TOKEN, client=http)
        try:
            draft = await client.create_trade_draft(
                req.to_body(), idempotency_key="live-explicit-user-intent"
            )
        except httpx.HTTPStatusError as exc:
            pytest.fail(f"建草稿被上游拒绝 HTTP {exc.response.status_code}")

    data = draft.get("data") or {}
    assert data.get("id"), f"未返回 draft id：{draft}"
    # 状态应为待确认（容忍不同命名）
    status = str(data.get("status", "")).upper()
    assert "PENDING" in status or "CONFIRM" in status, f"预期待确认态，实得 {status!r}"
