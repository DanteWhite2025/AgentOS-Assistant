"""/v1/agent/trade-draft 接线全分支覆盖 —— 真跑 FastAPI app（离线注入）。

覆盖：201 happy（账户经发现注入）/ 404 会话不存在 / 403 工具不在白名单 / 503 未配置 /
400 意图解析失败 / 409 无可交易账户 / 上游错误码精细映射（409/502）；
并断言审计留痕 + Idempotency-Key 透传 + 账户来自发现（非客户端 body）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from app.agents.llm import LLMResult
from app.config import Settings
from app.container import build_container
from app.main import create_app
from app.runtime.session import Scope, Session
from app.tools.agentos.client import AgentOsStagingClient

BASE = "https://api-staging.agentos-app.app"
GOOD_INTENT = (
    '{"symbol":"BTC","side":"BUY","quantity":"0.5","limit_price":"3000.00","leverage":"2"}'
)
AUTH = {"Authorization": "Bearer test-user:u_t"}
AUTH_U = {"Authorization": "Bearer test-user:u"}

# Core Binding 精确指定的账户（服务端按 token 派生，非客户端传入）
_DISCOVERED = [
    {
        "id": "atac_other",
        "agent_id": "agent-test-001",
        "status": "ACTIVE",
        "unified_account_id": "uac_other",
        "venue_account_id": "vac_other",
    },
    {
        "id": "atac_disc",
        "agent_id": "agent-test-001",
        "status": "ACTIVE",
        "unified_account_id": "uac_disc",
        "venue_account_id": "vac_disc",
    },
]


class StubLLM:
    def __init__(self, text: str) -> None:
        self._text = text

    async def complete(self, system, messages, tools) -> LLMResult:
        return LLMResult(text=self._text)


def _upstream(*, create_status=201, create_body=None, accounts=None, accounts_status=200):
    """MockTransport 扮 Core：StrategyBinding + 精确账户 + Draft。"""
    captured: list[httpx.Request] = []
    accts = _DISCOVERED if accounts is None else accounts
    default_draft = {
        "data": {
            "id": "draft_api",
            "version": 1,
            "symbol": "BTC",
            "side": "BUY",
            "quantity": "0.5",
            "limit_price": "3000.00",
            "status": "PENDING_CONFIRMATION",
        }
    }

    def handler(request):
        captured.append(request)
        if request.url.path.endswith("/me/trading-readiness"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "unified_account_id": "uac_disc",
                        "venue_account_id": "vac_disc",
                        "ledger_balance_by_asset": {"USDC": "100000000"},
                        "venue_balance_by_asset": {"USDC": "100"},
                        "venue_margin_minor": "100000000",
                        "daily_notional_limit_minor": "100000000",
                        "daily_notional_used_minor": "0",
                        "daily_notional_remaining_minor": "100000000",
                        "agent_trading_account": _DISCOVERED[1],
                        "mandate": {
                            "id": "mandate_disc",
                            "status": "ACTIVE",
                            "confirmation_mode": "ALWAYS_CONFIRM",
                            "max_notional_per_order_minor": 100_000_000,
                            "max_daily_notional_minor": 100_000_000,
                            "max_total_exposure_minor": 100_000_000,
                            "allowed_symbols": ["BTC", "ETH", "USDC"],
                            "allowed_order_types": ["LIMIT"],
                            "max_leverage": "3",
                            "max_slippage": "0.01",
                        },
                        "strategy_binding": {
                            "strategy_id": "strategy_1",
                            "agent_id": "agent-test-001",
                            "agent_trading_account_id": "atac_disc",
                            "venue_account_id": "vac_disc",
                            "mandate_id": "mandate_disc",
                            "authorization_grant_id": "grant_disc",
                            "capital_limit_minor": 100_000_000,
                            "status": "ACTIVE",
                            "market_types": ["PERP"],
                            "allowed_assets": ["BTC", "ETH", "USDC"],
                            "allowed_instruments": [],
                            "max_order_notional_minor": 100_000_000,
                            "max_daily_notional_minor": 100_000_000,
                            "max_leverage": "3",
                            "max_slippage": "0.01",
                        },
                        "can_create_agent_draft": True,
                        "blockers": [],
                        "stale": False,
                    },
                    "request_id": "req_readiness",
                },
            )
        if "/strategy-bindings/" in request.url.path:
            strategy_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "data": {
                        "strategy_id": strategy_id,
                        "agent_id": "agent-test-001",
                        "agent_trading_account_id": "atac_disc",
                        "venue_account_id": "vac_disc",
                        "mandate_id": "mandate_disc",
                        "authorization_grant_id": "grant_disc",
                        "capital_limit_minor": 100_000_000,
                        "status": "ACTIVE",
                        "market_types": ["PERP"],
                        "allowed_assets": ["BTC", "ETH", "USDC"],
                        "allowed_instruments": [],
                        "max_order_notional_minor": 100_000_000,
                        "max_daily_notional_minor": 100_000_000,
                        "max_leverage": "3",
                        "max_slippage": "0.01",
                    }
                },
            )
        if request.url.path.endswith("/agent-trading-accounts"):
            return httpx.Response(accounts_status, json={"data": accts})
        return httpx.Response(
            create_status, json=create_body if create_body is not None else default_draft
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), captured


def _app_with(container):
    app = create_app()
    app.state.container = container  # 绕过 lifespan，直接注入
    return app


def _api(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _new_session(api) -> str:
    resp = await api.post("/v1/agent/sessions", json={"modality": "text"}, headers=AUTH)
    assert resp.status_code == 201
    return resp.json()["session_id"]


def _draft_body(session_id: str, text: str = "买点") -> dict[str, str]:
    return {"session_id": session_id, "strategy_id": "strategy_1", "text": text}


# ---- 201 happy path：账户经发现注入 + 审计 + 幂等键透传 ----


async def test_happy_path_creates_draft_card():
    container = build_container(Settings(provider_mode="fake"))
    upstream, captured = _upstream(create_status=201)
    container.agentos_client = AgentOsStagingClient(BASE, token="T", client=upstream)
    container.llm = StubLLM(GOOD_INTENT)
    app = _app_with(container)
    try:
        async with _api(app) as api:
            sid = await _new_session(api)
            resp = await api.post(
                "/v1/agent/trade-draft",
                json=_draft_body(sid, "买 0.5 BTC 限价 3000"),  # 不传账户或 Mandate
                headers={**AUTH, "Idempotency-Key": "idem_api_1"},
            )
            assert resp.status_code == 201, resp.text
            body = resp.json()
            assert body["draft_id"] == "draft_api"
            assert body["status"] == "PENDING_CONFIRMATION"
            assert body["trace_id"].startswith("trc_")
            # 上游收到建草稿 + 幂等键透传 + 账户来自发现（非客户端）
            create_reqs = [r for r in captured if r.url.path.endswith("/trade-drafts")]
            assert create_reqs and create_reqs[-1].headers.get("Idempotency-Key") == "idem_api_1"
            import json as _json

            sent = _json.loads(create_reqs[-1].content)
            assert (
                sent["unified_account_id"] == "uac_disc" and sent["venue_account_id"] == "vac_disc"
            )
            assert sent["strategy_id"] == "strategy_1"
            assert sent["mandate_id"] == "mandate_disc"
            # 发现调用确实发生
            assert any(r.url.path.endswith("/agent-trading-accounts") for r in captured)
            # 审计留痕：result_ref = draft_id
            records = await container.audit_store.list_by_session(sid)
            assert any(
                r.result_ref == "draft_api" and r.tool_name == "agentos_create_trade_draft"
                for r in records
            )
    finally:
        await upstream.aclose()


async def test_client_cannot_inject_account_or_mandate_relationships():
    container = build_container(Settings(provider_mode="fake"))
    app = _app_with(container)
    async with _api(app) as api:
        response = await api.post(
            "/v1/agent/trade-draft",
            json={
                **_draft_body("session_any"),
                "unified_account_id": "uac_forged",
                "venue_account_id": "vac_forged",
                "mandate_id": "mandate_forged",
            },
            headers=AUTH,
        )
    assert response.status_code == 422
    fields = {tuple(error["loc"]) for error in response.json()["detail"]}
    assert ("body", "unified_account_id") in fields
    assert ("body", "venue_account_id") in fields
    assert ("body", "mandate_id") in fields


# ---- 404 会话不存在（发现之前就短路，无上游调用）----


async def test_session_not_found_404():
    container = build_container(Settings(provider_mode="fake"))
    app = _app_with(container)
    async with _api(app) as api:
        resp = await api.post(
            "/v1/agent/trade-draft", json=_draft_body("sess_nope", "x"), headers=AUTH
        )
    assert resp.status_code == 404
    assert resp.json()["code"] == "SESSION_NOT_FOUND"


# ---- 403 工具不在白名单 ----


async def test_tool_not_in_scope_403():
    container = build_container(Settings(provider_mode="fake"))
    now = datetime.now(UTC)
    scope = Scope(
        agents=("avatar",), tools=("get_positions",)
    )  # 故意不含 agentos_create_trade_draft
    await container.session_store.create(
        Session("sess_noscope", "u", "text", scope, "created", now, now)
    )
    app = _app_with(container)
    async with _api(app) as api:
        resp = await api.post(
            "/v1/agent/trade-draft",
            json=_draft_body("sess_noscope", "x"),
            headers=AUTH_U,
        )
    assert resp.status_code == 403
    assert resp.json()["code"] == "TOOL_NOT_IN_SCOPE"


# ---- 503 真链路未配置 ----


async def test_agentos_not_configured_503():
    container = build_container(Settings(provider_mode="fake"))  # agentos_client=None
    app = _app_with(container)
    async with _api(app) as api:
        sid = await _new_session(api)
        resp = await api.post("/v1/agent/trade-draft", json=_draft_body(sid, "x"), headers=AUTH)
    assert resp.status_code == 503
    assert resp.json()["code"] == "INTERNAL"


# ---- 400 意图解析失败 ----


async def test_intent_parse_error_400():
    container = build_container(Settings(provider_mode="fake"))
    upstream, _ = _upstream()
    container.agentos_client = AgentOsStagingClient(BASE, token="T", client=upstream)
    container.llm = StubLLM("完全不是 JSON 的一句话")  # 触发 IntentParseError
    app = _app_with(container)
    try:
        async with _api(app) as api:
            sid = await _new_session(api)
            resp = await api.post(
                "/v1/agent/trade-draft", json=_draft_body(sid, "乱说"), headers=AUTH
            )
        assert resp.status_code == 400
        assert resp.json()["code"] == "INVALID_REQUEST"
    finally:
        await upstream.aclose()


# ---- 409 无可交易账户（发现返回空）----


async def test_no_tradable_account_409():
    container = build_container(Settings(provider_mode="fake"))
    upstream, _ = _upstream(accounts=[])  # 发现返回空
    container.agentos_client = AgentOsStagingClient(BASE, token="T", client=upstream)
    container.llm = StubLLM(GOOD_INTENT)
    app = _app_with(container)
    try:
        async with _api(app) as api:
            sid = await _new_session(api)
            resp = await api.post("/v1/agent/trade-draft", json=_draft_body(sid), headers=AUTH)
        assert resp.status_code == 409
        assert resp.json()["code"] == "POLICY_VIOLATION"
    finally:
        await upstream.aclose()


# ---- 发现调用上游报错 → 映射（Runtime credential 401 → 503 INTERNAL）----


async def test_discovery_upstream_error_mapped():
    container = build_container(Settings(provider_mode="fake"))
    upstream, _ = _upstream(accounts_status=401, accounts={"error": {"code": "UNAUTHENTICATED"}})
    # accounts 传 dict 会被当 data，用不到；关键是 accounts_status=401 让发现调用 raise
    container.agentos_client = AgentOsStagingClient(BASE, token="T", client=upstream)
    container.llm = StubLLM(GOOD_INTENT)
    app = _app_with(container)
    try:
        async with _api(app) as api:
            sid = await _new_session(api)
            resp = await api.post("/v1/agent/trade-draft", json=_draft_body(sid), headers=AUTH)
        assert resp.status_code == 503
        assert resp.json()["code"] == "INTERNAL"
        assert resp.json()["message"] == "智能体服务凭证暂不可用"
        assert resp.json()["details"]["credential_refresh_required"] is True
    finally:
        await upstream.aclose()


# ---- 建草稿上游错误码精细映射（409 INVALID_STATE_TRANSITION → 409 POLICY_VIOLATION）----


async def test_upstream_error_code_mapped_409():
    container = build_container(Settings(provider_mode="fake"))
    upstream, _ = _upstream(
        create_status=409,
        create_body={
            "error": {"code": "INVALID_STATE_TRANSITION", "message": "state", "retryable": False}
        },
    )
    container.agentos_client = AgentOsStagingClient(BASE, token="T", client=upstream)
    container.llm = StubLLM(GOOD_INTENT)
    app = _app_with(container)
    try:
        async with _api(app) as api:
            sid = await _new_session(api)
            resp = await api.post("/v1/agent/trade-draft", json=_draft_body(sid), headers=AUTH)
        assert resp.status_code == 409
        j = resp.json()
        assert j["code"] == "POLICY_VIOLATION"
        assert j["details"]["upstream_code"] == "INVALID_STATE_TRANSITION"
    finally:
        await upstream.aclose()


# ---- 建草稿上游未知错误（500 无 code）→ 默认 502 INTERNAL ----


async def test_upstream_unknown_error_default_502():
    container = build_container(Settings(provider_mode="fake"))
    upstream, _ = _upstream(create_status=500, create_body={"oops": True})
    container.agentos_client = AgentOsStagingClient(BASE, token="T", client=upstream)
    container.llm = StubLLM(GOOD_INTENT)
    app = _app_with(container)
    try:
        async with _api(app) as api:
            sid = await _new_session(api)
            resp = await api.post("/v1/agent/trade-draft", json=_draft_body(sid), headers=AUTH)
        assert resp.status_code == 502
        assert resp.json()["code"] == "INTERNAL"
    finally:
        await upstream.aclose()
