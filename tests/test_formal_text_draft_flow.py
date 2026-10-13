from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx

from app.agents.llm import LLMResult, LLMToolCall
from app.agents.runner import AgentProfile, AgentRunner
from app.audit.store import InMemoryAuditStore
from app.policies.engine import PolicyEngine
from app.policies.mandate import default_mandate
from app.runtime.session import Scope, Session
from app.tools.agentos.client import AgentOsStagingClient
from app.tools.builtin import build_default_registry
from app.tools.core_client import FakeCoreClient, MarketTickerResponse
from app.tools.executor import ToolExecutor
from tests.core_fixtures import trading_readiness_payload

# 脚本化 LLM 的收尾回复。必须是一段「像样的、谈到了账户与额度」的答复 ——
# 用「坚硬」这类两字占位串会让下面的 `"uac_1" not in result.text` 变成空断言：
# 短到不可能包含任何标识，看着像防泄露门禁，实际一点都守不住。
ASSISTANT_REPLY = (
    "已查到 BTC 永续最新标记价，数据来自 Hyperliquid Mainnet 只读行情，"
    "交易环境是 Hyperliquid Testnet。你的账户当前可用交易额度充足，"
    "日内额度也还有富余。我只生成了待确认的交易草案，没有替你下单或确认。"
)

USER_TOKEN = "user-token-never-store-or-log"
AGENT_TOKEN = "agent-token-never-return"
STRATEGY_ID = "strategy_multi_asset_v1"


def _perp_instrument(symbol: str) -> dict[str, Any]:
    return {
        "instrument_id": f"perp_hyperliquid_testnet_{symbol.lower()}",
        "market_type": "PERP",
        "venue": "HYPERLIQUID",
        "environment": "TESTNET",
        "display_symbol": f"{symbol}-PERP",
        "venue_symbol": symbol,
        "base_asset": symbol,
        "quote_asset": "USDC",
        "trade_supported": True,
        "supported_order_types": ["LIMIT"],
        "price_decimals": 2,
        "size_decimals": 5,
        "minimum_quantity": "0.00001",
        "minimum_notional": "1",
        "maximum_leverage": "2",
        "status": "ACTIVE",
        "stale": False,
        "as_of": "2026-08-16T00:00:00Z",
    }


class ScriptedLLM:
    def __init__(self, *, create_draft: bool, final_text: str = ASSISTANT_REPLY) -> None:
        self._create_draft = create_draft
        self._final_text = final_text
        self.calls = 0

    async def complete(self, system, messages, tools) -> LLMResult:
        self.calls += 1
        if any(message.role == "tool" for message in messages):
            return LLMResult(text=self._final_text)
        calls = [
            # Reproduce the real model drift: Core Catalog owns venue_symbol=BTC,
            # but the LLM emits a pair-like ticker symbol.
            LLMToolCall("call_market", "market_tickers", {"symbols": ["BTCUSDC"]}),
            LLMToolCall("call_account", "get_account_summary", {}),
            LLMToolCall("call_readiness", "get_trading_readiness", {}),
        ]
        if self._create_draft:
            calls.append(
                LLMToolCall(
                    "call_draft",
                    "create_trade_draft",
                    {
                        "symbol": "BTC",
                        "side": "buy",
                        "order_type": "limit",
                        "notional_usdc": "10",
                        "price": "100000",
                        "leverage": 1,
                    },
                )
            )
        return LLMResult(text="", tool_calls=tuple(calls))


class ReadOnlyMarketCore(FakeCoreClient):
    async def get_market_tickers(
        self, symbols: list[str], request_id: str | None = None
    ) -> MarketTickerResponse:
        assert symbols == ["BTC"]
        return MarketTickerResponse.model_validate(
            {
                "data": [
                    {
                        "symbol": "BTC",
                        "last_price": "100000",
                        "mark_price": "100000",
                        "oracle_price": "100000",
                        "change_24h": "1",
                        "change_percent_24h": "0.001",
                        "high_24h": "101000",
                        "low_24h": "99000",
                        "volume_24h": "10",
                        "quote_volume_24h": "1000000",
                        "funding_rate": "0.0001",
                        "open_interest": "100",
                        "source": "HYPERLIQUID_INFO_API",
                        "environment": "MAINNET",
                        "as_of": "2026-08-16T00:00:00+00:00",
                        "stale": False,
                    }
                ],
                "meta": {
                    "source": "HYPERLIQUID",
                    "environment": "MAINNET",
                    "as_of": "2026-08-16T00:00:00+00:00",
                    "stale": False,
                    "next_cursor": None,
                },
                "request_id": "req_market",
            }
        )


def _session(registry) -> Session:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    return Session(
        session_id="sess_text",
        user_id="u1",
        modality="text",
        scope=Scope(agents=("strategy",), tools=registry.names()),
        state="created",
        created_at=now,
        last_activity_at=now,
    )


async def _run(
    *, create_draft: bool, blocked: bool = False, final_text: str = ASSISTANT_REPLY
) -> tuple[Any, list[httpx.Request], ScriptedLLM]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/me/trading-readiness":
            assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
            return httpx.Response(200, json=trading_readiness_payload(blocked=blocked))
        if request.url.path == "/internal/v1/markets":
            assert request.headers["Authorization"] == f"Bearer {AGENT_TOKEN}"
            return httpx.Response(
                200,
                json={
                    "data": [_perp_instrument("BTC")],
                    "meta": {"stale": False, "next_cursor": None},
                    "request_id": "req_catalog",
                },
            )
        if request.url.path == "/api/v1/me/agent-trade-intents":
            assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
            body = json.loads(request.content)
            assert body["agent_id"] == "agent-test-001"
            assert body["source"] == "TEXT"
            assert "strategy_id" not in body
            assert "mandate_id" not in body
            assert body["leverage"] == "1"
            return httpx.Response(
                201,
                json={
                    "data": {"id": "aint_1", "status": "PENDING_USER"},
                    "request_id": "req_draft",
                },
            )
        if request.url.path == "/api/v1/me/account-summary":
            assert request.headers["Authorization"] == f"Bearer {USER_TOKEN}"
            assert request.url.params.get("unified_account_id") == "uac_1"
            assert set(request.url.params) == {"unified_account_id"}
            return httpx.Response(
                200,
                json={
                    "data": {
                        "balance": {"asset": "USDC", "available_minor": 7000000},
                        "positions": [],
                        "orders": [],
                    },
                    "request_id": "req_summary",
                },
            )
        assert request.headers["Authorization"] == f"Bearer {AGENT_TOKEN}"
        if request.url.path == f"/internal/v1/strategy-bindings/{STRATEGY_ID}":
            return httpx.Response(
                200,
                json={"data": trading_readiness_payload(blocked=blocked)["data"]["strategy_binding"]},
            )
        if request.url.path == "/internal/v1/agent-trading-accounts":
            return httpx.Response(
                200,
                json={"data": [trading_readiness_payload(blocked=blocked)["data"]["agent_trading_account"]]},
            )
        if request.url.path == "/internal/v1/balances":
            return httpx.Response(200, json={"data": []})
        if request.url.path in {"/internal/v1/positions", "/internal/v1/orders"}:
            return httpx.Response(200, json={"data": []})
        raise AssertionError(f"legacy or unexpected Core route: {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        agentos = AgentOsStagingClient("https://core.example", AGENT_TOKEN, client)
        registry = build_default_registry()
        executor = ToolExecutor(
            registry,
            ReadOnlyMarketCore(),
            PolicyEngine(default_mandate()),
            agentos_client=agentos,
            expected_agent_id="agent-test-001",
        )
        llm = ScriptedLLM(create_draft=create_draft, final_text=final_text)
        runner = AgentRunner(
            AgentProfile("strategy", "test", "只读查询；计划只创建 Draft", registry.names()),
            executor,
            llm,
            InMemoryAuditStore(),
        )
        result = await runner.run_turn(
            _session(registry),
            (
                "查询当前 BTC 行情和我的可用交易额度，然后生成一个 BTC、约 10 USDC、"
                "1 倍杠杆的限价交易计划，但先不要执行。"
                if create_draft
                else "只查询当前 BTC 行情、账户摘要和我的可用交易额度。"
            ),
            "trace_text",
            datetime(2026, 8, 16, tzinfo=UTC),
            user_bearer_token=USER_TOKEN,
            idempotency_key="idem_mobile_text_1",
        )
    return result, requests, llm


async def test_readonly_market_and_limit_query_never_creates_draft() -> None:
    result, requests, _llm = await _run(create_draft=False)
    assert [tool.name for tool in result.executed_tools] == [
        "market_tickers",
        "get_account_summary",
        "get_trading_readiness",
    ]
    assert result.executed_tools[0].result["data"][0]["symbol"] == "BTC"
    assert result.executed_tools[0].result["meta"]["environment"] == "MAINNET"
    assert "instruments" not in result.executed_tools[0].result
    summary = result.executed_tools[1].result["account_summary"]
    readiness = result.executed_tools[2].result
    assert readiness["available_trade_notional_minor"] == "10000000"
    assert readiness["daily_notional_remaining_minor"] == "49500000"
    assert readiness["total_exposure_limit_minor"] == "10000000"
    assert summary["balance_semantics"] == "CORE_LEDGER_NOT_VENUE_EQUITY"
    assert summary["venue_equity"]["status"] == "NOT_AVAILABLE"
    assert readiness["mandate"]["confirmation_mode"] == "ALWAYS_CONFIRM"
    assert all(request.method == "GET" for request in requests)
    assert all("user_id" not in request.url.params for request in requests)
    summary_requests = [
        request for request in requests if request.url.path == "/api/v1/me/account-summary"
    ]
    assert len(summary_requests) == 1


async def test_plan_uses_formal_accounts_and_only_creates_one_core_draft() -> None:
    result, requests, llm = await _run(create_draft=True)
    draft = result.executed_tools[-1].result
    assert draft == {
        "status": "PENDING_USER",
        "trade_intent_id": "aint_1",
        "trade_draft_id": "aint_1",
        "core_draft_id": "aint_1",
        "draft_version": None,
        "market_type": "PERP",
        "instrument_id": "perp_hyperliquid_testnet_btc",
        "agent_id": "agent-test-001",
        "confirmation_mode": "ALWAYS_CONFIRM",
        "execution_requested": False,
        "request_id": "req_draft",
    }
    writes = [request for request in requests if request.method == "POST"]
    assert [request.url.path for request in writes] == ["/api/v1/me/agent-trade-intents"]
    assert writes[0].headers["Idempotency-Key"].startswith("idem_perp_")
    assert all("confirm" not in request.url.path for request in requests)
    assert all("execution" not in request.url.path for request in requests)
    assert all(request.method == "GET" for request in requests if "orders" in request.url.path)
    assert result.text == ASSISTANT_REPLY
    # 正式标识只允许存在于 Core 请求里，绝不能出现在给用户的答复中。
    for secret in ("uac_1", "vac_1", "mandate_1", "atac_1", "grant_1"):
        assert secret not in result.text
    assert USER_TOKEN not in repr(result)
    assert AGENT_TOKEN not in repr(result)
    assert llm.calls == 2


async def test_legacy_readiness_blocker_does_not_block_user_wallet_intent() -> None:
    result, requests, llm = await _run(create_draft=True, blocked=True, final_text=ASSISTANT_REPLY)

    assert [tool.name for tool in result.executed_tools] == [
        "market_tickers",
        "get_account_summary",
        "get_trading_readiness",
        "create_trade_draft",
    ]
    assert result.blockers == ()
    assert result.executed_tools[-1].result["trade_intent_id"] == "aint_1"
    assert result.executed_tools[-1].result["status"] == "PENDING_USER"
    writes = [request for request in requests if request.method == "POST"]
    assert [request.url.path for request in writes] == ["/api/v1/me/agent-trade-intents"]
    for secret in ("uac_1", "vac_1", "mandate_1", "atac_1", "grant_1"):
        assert secret not in result.text
    assert USER_TOKEN not in repr(result)
    assert AGENT_TOKEN not in repr(result)
    assert result.text == ASSISTANT_REPLY
    assert llm.calls == 2
