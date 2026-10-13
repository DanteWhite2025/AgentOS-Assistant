from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "deploy-staging.yml"


def test_deploy_uses_only_formal_core_actor_secret_and_fails_closed() -> None:
    workflow = WORKFLOW.read_text()
    assert "CORE_ACTOR_TOKEN: ${{ secrets.AGENT_CORE_ACTOR_TOKEN }}" in workflow
    assert "secrets.AGENT_CORE_ACTOR_TOKEN ||" not in workflow
    assert "secrets.AGENT_STAGING_TOKEN" not in workflow
    assert 'set_env AGENT_CORE_ACTOR_TOKEN "$CORE_ACTOR_TOKEN"' in workflow


def test_deploy_removes_legacy_values_and_runs_real_read_only_preflight() -> None:
    workflow = WORKFLOW.read_text()
    for legacy in (
        "AGENT_AGENTOS_STAGING_TOKEN",
        "AGENTOS_STAGING_TOKEN",
        "AGENT_STAGING_TOKEN",
    ):
        assert f"/^${legacy}=" not in workflow  # guard against accidental shell expansion form
        assert f"/^{legacy}=/d" in workflow
    assert "python -m app.ops.core_credential_preflight" in workflow
    assert workflow.index("python -m app.ops.core_credential_preflight") < workflow.index(
        "wait_for_endpoint http://127.0.0.1:8081/readyz"
    )
    assert workflow.index("python -m app.ops.core_credential_preflight") < workflow.index(
        "docker compose -p app up -d --no-build"
    )
    assert 'get_market_tickers(["BTC", "ETH"])' in workflow
    assert "catalog_response = await agentos.get_market_catalog(limit=100)" in workflow
    assert "core_market_catalog_verified=" in workflow
    assert '"formal_path": "/internal/v1/markets"' in workflow
    assert '"instrument_ids": sorted(item.instrument_id for item in catalog)' in workflow
    assert "get_agent_trading_accounts" not in workflow
    assert "core_account_summary_verified=" not in workflow
    assert '"formal_path": "/internal/v1/account-summary"' not in workflow
    assert '"write_performed": False' in workflow
