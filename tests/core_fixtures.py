"""共享的 Core 响应夹具。

Trading Readiness 的结构由 `parse_trading_readiness` 严格校验（缺 id、mandate
或 strategy_binding 都会被拒），各测试各抄一份很容易和解析器悄悄跑偏。
统一放这里，改契约时只改一处。
"""

from __future__ import annotations

from typing import Any

DEFAULT_STRATEGY_ID = "strategy_multi_asset_v1"


def trading_readiness_payload(
    *, blocked: bool = False, strategy_id: str = DEFAULT_STRATEGY_ID
) -> dict[str, Any]:
    return {
        "data": {
            "unified_account_id": "uac_1",
            "venue_account_id": "vac_1",
            "ledger_balance_by_asset": {"USDC": "7000000"},
            "venue_balance_by_asset": {"USDC": "12.5"},
            "venue_margin_minor": "20000000",
            "daily_notional_limit_minor": "50000000",
            "daily_notional_used_minor": "50000000" if blocked else "500000",
            "daily_notional_remaining_minor": "0" if blocked else "49500000",
            "capital_status": "AVAILABLE",
            "agent_trading_account": {
                "id": "atac_1",
                "unified_account_id": "uac_1",
                "venue_account_id": "vac_1",
                "agent_id": "agent-test-001",
                "status": "ACTIVE",
            },
            "mandate": {
                "id": "mandate_1",
                "status": "ACTIVE",
                "confirmation_mode": "ALWAYS_CONFIRM",
                "max_notional_per_order_minor": 10000000,
                "max_daily_notional_minor": 50000000,
                "max_total_exposure_minor": 10000000,
                "allowed_symbols": ["BTC", "ETH", "USDC", "USDT", "BNB"],
                "allowed_order_types": ["LIMIT", "EXACT_INPUT"],
                "max_leverage": "2",
                "max_slippage": "0.01",
            },
            "strategy_binding": {
                "strategy_id": strategy_id,
                "agent_id": "agent-test-001",
                "agent_trading_account_id": "atac_1",
                "venue_account_id": "vac_1",
                "mandate_id": "mandate_1",
                "authorization_grant_id": "grant_1",
                "capital_limit_minor": 10000000,
                "status": "ACTIVE",
                "market_types": ["PERP", "SPOT"],
                "allowed_assets": ["BTC", "ETH", "USDC", "USDT", "BNB"],
                "allowed_instruments": [],
                "max_order_notional_minor": 10000000,
                "max_daily_notional_minor": 50000000,
                "max_leverage": "2",
                "max_slippage": "0.01",
            },
            "can_create_agent_draft": not blocked,
            "blockers": ["DAILY_NOTIONAL_LIMIT_REACHED"] if blocked else [],
            "stale": False,
        },
        "request_id": "req_readiness",
    }
