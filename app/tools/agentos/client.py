"""AgentOsStagingClient —— 真测试网 Agent API 的薄 HTTP 封装。

职责边界：智能体只做「查询 + 构造草稿」。Runtime 不提供 confirm、
request-execution、订单撤单或 Mandate 写入方法，也不接受 Step-Up Token。
用户确认和后续订单生命周期只允许由 Mobile 直接调用 Core 正式 USER 接口。

不注入全局单例：base_url / token / httpx.AsyncClient 全部外部注入，便于
用 MockTransport 免 token 验证请求构造，用真 token 打真链路。
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any, Final
from urllib.parse import quote

import httpx

_AGENT_PREFIX: Final = "/internal/v1"
_PUBLIC_PREFIX: Final = "/api/v1"


def _new_request_id() -> str:
    """每次请求的追踪 id（契约要求写请求必带 X-Request-ID，1..128 字符）。"""
    return "req_" + secrets.token_hex(16)


class AgentOsStagingClient:
    """对接 https://api-staging.agentos-app.app 的 Agent 接口。"""

    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._client = client

    def _common_headers(self, token: str | None = None) -> dict[str, str]:
        """所有请求共有：Bearer 身份 + Accept + X-Request-ID（契约要求）。"""
        return {
            "Authorization": f"Bearer {token if token is not None else self._token}",
            "Accept": "application/json",
            "X-Request-ID": _new_request_id(),
        }

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        bearer_token: str | None = None,
    ) -> dict[str, Any]:
        resp = await self._client.get(
            f"{self._base}{path}",
            params=params,
            headers=self._common_headers(bearer_token),
        )
        resp.raise_for_status()
        return resp.json()

    async def _post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        idempotency_key: str,
        bearer_token: str | None = None,
    ) -> dict[str, Any]:
        headers = self._common_headers(bearer_token)
        headers["Idempotency-Key"] = idempotency_key
        resp = await self._client.post(f"{self._base}{path}", json=json, headers=headers)
        resp.raise_for_status()
        return resp.json()

    async def _post_idempotent(
        self,
        path: str,
        *,
        json: dict[str, Any],
        idempotency_key: str,
        bearer_token: str | None = None,
    ) -> dict[str, Any]:
        """Replay one ambiguous transport failure with the exact same key.

        Core owns the idempotency record, so a timeout after acceptance recovers
        the same Draft instead of creating another one.  HTTP failures are never
        blindly retried here.
        """

        if not idempotency_key.strip():
            raise ValueError("创建 Draft 必须提供稳定 Idempotency-Key")
        try:
            return await self._post(
                path,
                json=json,
                idempotency_key=idempotency_key,
                bearer_token=bearer_token,
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            await asyncio.sleep(0)
            return await self._post(
                path,
                json=json,
                idempotency_key=idempotency_key,
                bearer_token=bearer_token,
            )

    # ---- 信息查询（Agent tag，身份从 token 派生）----

    async def get_balances(self, unified_account_id: str) -> dict[str, Any]:
        """GET /internal/v1/balances —— 权威 Ledger 余额（契约参数 unified_account_id）。"""
        return await self._get(
            f"{_AGENT_PREFIX}/balances", params={"unified_account_id": unified_account_id}
        )

    async def get_user_balances(
        self, unified_account_id: str, user_bearer_token: str
    ) -> dict[str, Any]:
        """Read the current USER's balances without crossing Agent ownership.

        ``/internal/v1/balances`` requires an AGENT token whose ``user_id`` owns
        the requested Unified Account.  Runtime's configured AGENT credential
        must therefore not be combined with an account ID discovered from an
        arbitrary Mobile USER session.  Forward the transient USER credential
        only for this request and never assign, cache, or log it.
        """

        account_id = unified_account_id.strip()
        if not account_id:
            raise ValueError("Unified Account ID 不能为空")
        if not user_bearer_token.strip():
            raise ValueError("USER Bearer Token 不能为空")
        return await self._get(
            f"{_PUBLIC_PREFIX}/trading-accounts/{quote(account_id, safe='')}/balances",
            bearer_token=user_bearer_token,
        )

    async def get_positions(self) -> dict[str, Any]:
        """GET /internal/v1/positions —— 当前仓位。"""
        return await self._get(f"{_AGENT_PREFIX}/positions")

    async def get_orders(self) -> dict[str, Any]:
        """GET /internal/v1/orders —— 智能体可见订单。"""
        return await self._get(f"{_AGENT_PREFIX}/orders")

    async def get_account_summary(self, unified_account_id: str) -> dict[str, Any]:
        """GET /internal/v1/account-summary —— 余额 + Agent 可见仓位/订单汇总。"""
        return await self._get(
            f"{_AGENT_PREFIX}/account-summary", params={"unified_account_id": unified_account_id}
        )

    async def get_user_account_summary(
        self, unified_account_id: str, user_bearer_token: str
    ) -> dict[str, Any]:
        """Read the current USER's account summary without crossing Agent ownership.

        Mirrors ``get_user_balances``: ``/internal/v1/account-summary`` requires
        an AGENT token whose ``user_id`` owns the requested Unified Account, so
        it must never be combined with an account ID discovered from an
        arbitrary Mobile USER session. Forward the transient USER credential
        only for this request and never assign, cache, or log it.
        """

        account_id = unified_account_id.strip()
        if not account_id:
            raise ValueError("Unified Account ID 不能为空")
        if not user_bearer_token.strip():
            raise ValueError("USER Bearer Token 不能为空")
        return await self._get(
            f"{_PUBLIC_PREFIX}/me/account-summary",
            params={"unified_account_id": account_id},
            bearer_token=user_bearer_token,
        )

    async def get_user_asset_snapshot(self, user_bearer_token: str) -> dict[str, Any]:
        """GET /api/v1/me/asset-snapshot —— 钱包直签用户的实时链上与场所资产投影。

        仅接受短期 Mobile USER Token，身份从 Token 派生。
        不依赖 unified_account_id、Trading Readiness、Core Ledger 或 AGENT Token。
        """
        token = user_bearer_token.strip()
        if not token:
            raise ValueError("USER Bearer Token 不能为空")
        return await self._get(
            f"{_PUBLIC_PREFIX}/me/asset-snapshot",
            bearer_token=token,
        )

    async def get_agent_trading_accounts(self) -> dict[str, Any]:
        """GET /internal/v1/agent-trading-accounts —— 按 token(user_id+agent_id) 发现可用账户。

        会话起始用它拿 unified_account_id / venue_account_id（身份从 token 派生，不靠外部注入）。
        """
        return await self._get(f"{_AGENT_PREFIX}/agent-trading-accounts")

    async def get_strategy_bindings(self) -> dict[str, Any]:
        """GET /internal/v1/strategy-bindings —— 当前 Token Agent 的全部权威绑定。"""
        return await self._get(f"{_AGENT_PREFIX}/strategy-bindings")

    async def get_strategy_binding(self, strategy_id: str) -> dict[str, Any]:
        """GET /internal/v1/strategy-bindings/{strategy_id} —— 解析单个策略的权威关系。

        Core 会基于 AGENT Token 重新校验 user_id/agent_id；Runtime 不能通过
        请求参数替换账户、Mandate 或授权关系。
        """
        strategy_id = strategy_id.strip()
        if not strategy_id:
            raise ValueError("strategy_id 不能为空")
        return await self._get(f"{_AGENT_PREFIX}/strategy-bindings/{strategy_id}")

    async def get_market_catalog(
        self,
        *,
        market_type: str | None = None,
        query: str = "",
        cursor: str | None = None,
        limit: int = 20,
        trade_supported: bool = True,
    ) -> dict[str, Any]:
        """Read Core's AGENT-owned canonical Market Catalog.

        Instrument identity and execution metadata belong to Core.  This call
        therefore uses only the configured AGENT credential and never forwards
        the transient Mobile USER credential.
        """

        if market_type is not None and market_type.upper() not in {"SPOT", "PERP"}:
            raise ValueError("market_type 必须是 SPOT 或 PERP")
        params: dict[str, Any] = {
            "query": query.strip(),
            "limit": limit,
            "trade_supported": str(trade_supported).lower(),
        }
        if market_type is not None:
            params["market_type"] = market_type.upper()
        if cursor:
            params["cursor"] = cursor
        return await self._get(f"{_AGENT_PREFIX}/markets", params=params)

    async def get_market_instrument(self, instrument_id: str) -> dict[str, Any]:
        """Resolve an exact Core-owned instrument; never accepts venue metadata."""

        instrument_id = instrument_id.strip()
        if not instrument_id:
            raise ValueError("instrument_id 不能为空")
        return await self._get(f"{_AGENT_PREFIX}/markets/{instrument_id}")

    async def get_user_trading_readiness(self, user_bearer_token: str) -> dict[str, Any]:
        """Read the current USER's authoritative delegated-trading readiness.

        The Mobile USER token is used only for this one request.  It is never
        assigned to the client, cached, logged, or reused for an internal
        AGENT operation.
        """

        if not user_bearer_token.strip():
            raise ValueError("USER Bearer Token 不能为空")
        return await self._get(
            f"{_PUBLIC_PREFIX}/me/trading-readiness",
            bearer_token=user_bearer_token,
        )

    # ---- 构造交易（智能体侧，写）----

    async def create_agent_trade_intent(
        self,
        body: dict[str, Any],
        *,
        idempotency_key: str,
        user_bearer_token: str,
    ) -> dict[str, Any]:
        """Create an Intent owned by the authenticated Mobile user."""
        clean_key = idempotency_key.strip()
        if len(clean_key) < 16:
            raise ValueError("idempotency_key 必须至少 16 个字符")
        if not user_bearer_token.strip():
            raise ValueError("USER Bearer Token 不能为空")
        return await self._post_idempotent(
            f"{_PUBLIC_PREFIX}/me/agent-trade-intents",
            json=body,
            idempotency_key=clean_key,
            bearer_token=user_bearer_token,
        )

    async def create_trade_draft(
        self, body: dict[str, Any], *, idempotency_key: str
    ) -> dict[str, Any]:
        """POST /internal/v1/trade-drafts —— 从已验证 Agent 身份创建交易草稿。

        body 由 models.CreateTradeDraftRequest.to_body() 产出；返回 TradeDraftResponse。
        """
        return await self._post_idempotent(
            f"{_AGENT_PREFIX}/trade-drafts", json=body, idempotency_key=idempotency_key
        )

    async def create_spot_swap_draft(
        self,
        body: dict[str, Any],
        *,
        idempotency_key: str,
        user_bearer_token: str,
    ) -> dict[str, Any]:
        """POST /api/v1/swap-drafts -- creates Spot Draft carrying USER Bearer token."""

        clean_key = idempotency_key.strip()
        if not clean_key:
            raise ValueError("idempotency_key 不能为空")
        if not user_bearer_token.strip():
            raise ValueError("USER Bearer Token 不能为空")
        return await self._post_idempotent(
            f"{_PUBLIC_PREFIX}/swap-drafts",
            json=body,
            idempotency_key=clean_key,
            bearer_token=user_bearer_token,
        )

    async def get_spot_swap_draft(
        self, draft_id: str, *, user_bearer_token: str
    ) -> dict[str, Any]:
        """Recover a User-owned Spot Draft by its Core id."""

        draft_id = draft_id.strip()
        user_bearer_token = user_bearer_token.strip()
        if not draft_id:
            raise ValueError("draft_id 不能为空")
        if not user_bearer_token:
            raise ValueError("USER Bearer Token 不能为空")
        return await self._get(
            f"{_PUBLIC_PREFIX}/swap-drafts/{draft_id}",
            bearer_token=user_bearer_token,
        )
