"""真链路只读段（B 段搬进 pytest）—— 打真 staging 的 balances/positions/orders。

安全：只读，无副作用。无 AGENT_CORE_ACTOR_TOKEN 自动 skip（CI 默认跳过）。
写操作不在此测（团队拍板：写只做契约 Mock，见 test_agentos_client.py）。

手动跑：
  AGENT_CORE_ACTOR_TOKEN=xxx .venv/bin/python -m pytest tests/test_agentos_live.py -m live -v
"""

from __future__ import annotations

import os

import httpx
import pytest

from app.tools.agentos.client import AgentOsStagingClient

STAGING_BASE = "https://api-staging.agentos-app.app"


def _token() -> str:
    token = os.environ.get("AGENT_CORE_ACTOR_TOKEN", "")
    if not token:
        try:
            from app.config import get_settings

            token = get_settings().core_actor_token
        except Exception:  # pragma: no cover - 配置缺失时视作无 token
            token = ""
    return token


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not _token(), reason="未设置 AGENT_CORE_ACTOR_TOKEN，跳过真链路测试"),
]


async def test_live_positions_readonly():
    async with httpx.AsyncClient(timeout=15.0) as http:
        client = AgentOsStagingClient(STAGING_BASE, token=_token(), client=http)
        data = await client.get_positions()
        assert isinstance(data, dict)


async def test_live_orders_readonly():
    async with httpx.AsyncClient(timeout=15.0) as http:
        client = AgentOsStagingClient(STAGING_BASE, token=_token(), client=http)
        data = await client.get_orders()
        assert isinstance(data, dict)


async def test_live_agent_trading_accounts_readonly():
    """账户发现：按 token 返回可用账户（含 unified/venue account id）。"""
    async with httpx.AsyncClient(timeout=15.0) as http:
        client = AgentOsStagingClient(STAGING_BASE, token=_token(), client=http)
        data = await client.get_agent_trading_accounts()
        assert isinstance(data, dict)
        assert isinstance(data.get("data"), list)


async def test_live_account_summary_readonly():
    unified = os.environ.get("AGENTOS_UNIFIED_ACCOUNT_ID", "")
    if not unified:
        pytest.skip("未设置 AGENTOS_UNIFIED_ACCOUNT_ID，跳过 account-summary")
    async with httpx.AsyncClient(timeout=15.0) as http:
        client = AgentOsStagingClient(STAGING_BASE, token=_token(), client=http)
        data = await client.get_account_summary(unified)
        assert isinstance(data, dict)


async def test_live_balances_readonly():
    # 契约：balances 参数是 unified_account_id（旧 trading_account_id 已废弃）
    unified = os.environ.get("AGENTOS_UNIFIED_ACCOUNT_ID", "")
    if not unified:
        pytest.skip("未设置 AGENTOS_UNIFIED_ACCOUNT_ID，跳过 balances")
    async with httpx.AsyncClient(timeout=15.0) as http:
        client = AgentOsStagingClient(STAGING_BASE, token=_token(), client=http)
        data = await client.get_balances(unified)
        assert isinstance(data, dict)
