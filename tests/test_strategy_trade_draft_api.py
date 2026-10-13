import json

import httpx

from app.agents.llm import LLMResult
from app.config import Settings
from app.container import build_container
from app.main import create_app
from app.tools.agentos.client import AgentOsStagingClient

_HDR = {"X-User-Id": "u1", "X-Agent-Id": "a1"}
BASE = "https://api-staging.agentos-app.app"
GOOD = '{"symbol":"BTC","side":"BUY","quantity":"0.001","limit_price":"3000","leverage":"2"}'
_ACCTS = [
    {
        "id": "atac_other",
        "agent_id": "agent-test-001",
        "status": "ACTIVE",
        "unified_account_id": "uac_other",
        "venue_account_id": "vac_other",
    },
    {
        "id": "atac_bound",
        "agent_id": "agent-test-001",
        "status": "ACTIVE",
        "unified_account_id": "uac_bound",
        "venue_account_id": "vac_bound",
    },
]


class StubLLM:
    def __init__(self, t):
        self._t = t

    async def complete(self, s, m, tools):
        return LLMResult(text=self._t)


def _upstream(
    create_status=201,
    create_body=None,
    accounts=None,
    accounts_status=200,
    binding_status="ACTIVE",
    binding_strategy_id=None,
    captured=None,
):
    accts = _ACCTS if accounts is None else accounts
    captured = [] if captured is None else captured

    def handler(req):
        captured.append(req)
        if "/strategy-bindings/" in req.url.path:
            requested = req.url.path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "data": {
                        "strategy_id": binding_strategy_id or requested,
                        "agent_id": "agent-test-001",
                        "agent_trading_account_id": "atac_bound",
                        "venue_account_id": "vac_bound",
                        "mandate_id": "mandate_bound",
                        "authorization_grant_id": "grant_bound",
                        "capital_limit_minor": 100_000_000,
                        "status": binding_status,
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
        if req.url.path.endswith("/agent-trading-accounts"):
            return httpx.Response(accounts_status, json={"data": accts})
        return httpx.Response(
            create_status,
            json=create_body
            or {
                "data": {
                    "id": "draft_s",
                    "version": 1,
                    "symbol": "BTC",
                    "side": "BUY",
                    "quantity": "0.001",
                    "limit_price": "3000",
                    "status": "PENDING_CONFIRMATION",
                }
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _app(llm_text=GOOD, upstream=None):
    c = build_container(Settings(provider_mode="fake"))
    c.agentos_client = AgentOsStagingClient(BASE, token="T", client=upstream or _upstream())
    c.llm = StubLLM(llm_text)
    app = create_app()
    app.state.container = c
    return app, c


def _api(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _mk_body(**over):
    b = {
        "name": "s",
        "template_id": "t",
        "runtime_env": "paper",
        "allocation": "100000",
        "capital_capacity": "100000",
        "strategy_capacity": "100000",
        "max_leverage": "3",
    }
    b.update(over)
    return b


async def _new_strategy(api, **over):
    r = await api.post("/v1/agent/strategies", json=_mk_body(**over), headers=_HDR)
    return r.json()["strategy_id"]


async def test_strategy_trade_draft_happy():
    captured = []
    app, c = _app(upstream=_upstream(captured=captured))
    async with _api(app) as api:
        sid = await _new_strategy(api)
        r = await api.post(
            f"/v1/agent/strategies/{sid}/trade-draft",
            json={"text": "买 0.001 BTC 限价 3000"},
            headers=_HDR,
        )
        assert r.status_code == 201, r.text
        assert r.json()["draft_id"] == "draft_s"
        draft_request = next(req for req in captured if req.url.path.endswith("/trade-drafts"))
        draft_body = json.loads(draft_request.content)
        assert draft_body["strategy_id"] == sid
        assert draft_body["mandate_id"] == "mandate_bound"
        assert draft_body["unified_account_id"] == "uac_bound"
        assert draft_body["venue_account_id"] == "vac_bound"
        assert draft_body["symbol"] == "BTC"
        # 审计留痕
        recs = await c.audit_store.list_by_session(sid)
        assert any(rr.result_ref == "draft_s" for rr in recs)


async def test_strategy_trade_draft_rejects_client_account_injection():
    app, _c = _app()
    async with _api(app) as api:
        sid = await _new_strategy(api)
        response = await api.post(
            f"/v1/agent/strategies/{sid}/trade-draft",
            json={"text": "买点", "venue_account_id": "vac_forged"},
            headers=_HDR,
        )
    assert response.status_code == 422


async def test_trade_draft_paused_strategy_409():
    app, _c = _app()
    async with _api(app) as api:
        sid = await _new_strategy(api)
        await api.post(f"/v1/agent/strategies/{sid}/pause", headers=_HDR)
        r = await api.post(
            f"/v1/agent/strategies/{sid}/trade-draft", json={"text": "买"}, headers=_HDR
        )
        assert r.status_code == 409


async def test_trade_draft_leverage_over_mandate_403():
    # 策略 max_leverage=1，但意图 leverage=5 → 派生 mandate 预检拒
    app, _c = _app(
        llm_text='{"symbol":"BTC","side":"BUY","quantity":"0.001","limit_price":"3000","leverage":"5"}'
    )
    async with _api(app) as api:
        sid = await _new_strategy(api, max_leverage="1")
        r = await api.post(
            f"/v1/agent/strategies/{sid}/trade-draft", json={"text": "5x 买"}, headers=_HDR
        )
        assert r.status_code == 403
        assert r.json()["code"] == "POLICY_VIOLATION"


async def test_trade_draft_strategy_not_found_404():
    app, _c = _app()
    async with _api(app) as api:
        r = await api.post(
            "/v1/agent/strategies/nope/trade-draft", json={"text": "买"}, headers=_HDR
        )
        assert r.status_code == 404


async def test_trade_draft_not_configured_503():
    c = build_container(Settings(provider_mode="fake"))  # agentos_client=None
    from app.agents.llm import FakeLLM

    c.llm = FakeLLM()
    app = create_app()
    app.state.container = c
    async with _api(app) as api:
        sid = await _new_strategy(api)
        r = await api.post(
            f"/v1/agent/strategies/{sid}/trade-draft", json={"text": "买"}, headers=_HDR
        )
        assert r.status_code == 503


async def test_trade_draft_no_tradable_account_409():
    app, _c = _app(upstream=_upstream(accounts=[]))
    async with _api(app) as api:
        sid = await _new_strategy(api)
        r = await api.post(
            f"/v1/agent/strategies/{sid}/trade-draft", json={"text": "买点"}, headers=_HDR
        )
        assert r.status_code == 409
        assert r.json()["code"] == "POLICY_VIOLATION"
        assert "不可见" in r.json()["message"]


async def test_trade_draft_discovery_upstream_error_mapped_502():
    app, _c = _app(upstream=_upstream(accounts_status=500))
    async with _api(app) as api:
        sid = await _new_strategy(api)
        r = await api.post(
            f"/v1/agent/strategies/{sid}/trade-draft", json={"text": "买点"}, headers=_HDR
        )
        assert r.status_code == 502
        assert r.json()["code"] == "INTERNAL"


async def test_trade_draft_core_binding_paused_409():
    app, _c = _app(upstream=_upstream(binding_status="PAUSED"))
    async with _api(app) as api:
        sid = await _new_strategy(api)
        r = await api.post(
            f"/v1/agent/strategies/{sid}/trade-draft",
            json={"text": "买点"},
            headers=_HDR,
        )
        assert r.status_code == 409
        assert r.json()["code"] == "POLICY_VIOLATION"
        assert "PAUSED" in r.json()["message"]


async def test_trade_draft_rejects_cross_strategy_binding_409():
    app, _c = _app(upstream=_upstream(binding_strategy_id="strategy_other"))
    async with _api(app) as api:
        sid = await _new_strategy(api)
        r = await api.post(
            f"/v1/agent/strategies/{sid}/trade-draft",
            json={"text": "买点"},
            headers=_HDR,
        )
        assert r.status_code == 409
        assert "strategy_id 不一致" in r.json()["message"]


async def test_trade_draft_intent_parse_error_400():
    app, _c = _app(llm_text="完全不是 JSON 的一句话")  # 触发 IntentParseError
    async with _api(app) as api:
        sid = await _new_strategy(api)
        r = await api.post(
            f"/v1/agent/strategies/{sid}/trade-draft", json={"text": "乱说"}, headers=_HDR
        )
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_REQUEST"


async def test_trade_draft_create_draft_upstream_error_mapped_502():
    app, _c = _app(upstream=_upstream(create_status=500, create_body={"oops": True}))
    async with _api(app) as api:
        sid = await _new_strategy(api)
        r = await api.post(
            f"/v1/agent/strategies/{sid}/trade-draft", json={"text": "买点"}, headers=_HDR
        )
        assert r.status_code == 502
        assert r.json()["code"] == "INTERNAL"
