from app.usage.tool_providers import provider_for_tool


def test_web_search_maps_to_tavily() -> None:
    assert provider_for_tool("web_search") == "tavily"


def test_core_tools_map_to_core() -> None:
    for name in (
        "get_market_price",
        "market_tickers",
        "search_market_catalog",
        "get_balances",
        "get_positions",
        "get_orders",
        "get_account_summary",
        "get_risk_summary",
        "get_trading_readiness",
        "get_transaction_history",
        "create_trade_draft",
        "create_perp_trade_draft",
        "create_spot_swap_draft",
        "agentos_get_balances",
        "agentos_get_positions",
        "agentos_get_orders",
        "agentos_create_trade_draft",
    ):
        assert provider_for_tool(name) == "core", name


def test_internal_tools_map_to_internal() -> None:
    assert provider_for_tool("update_user_preference") == "internal"
    assert provider_for_tool("ask_investment_expert") == "internal"


def test_unknown_tool_is_flagged_not_silently_bucketed() -> None:
    """新工具漏配时要在报表里看得见，不能默默归到某个 provider。"""
    assert provider_for_tool("some_new_tool_2027") == "unknown"


def test_every_registered_tool_has_an_explicit_provider() -> None:
    """全仓已注册的工具都必须显式登记归属。

    这条会在有人加了新工具但忘了配 provider 时变红——比等到报表里出现一堆
    unknown 再回头查要早得多。
    """
    from app.tools.builtin import build_default_registry

    names = list(build_default_registry().names())
    assert names, "注册表不该是空的"
    unmapped = [n for n in names if provider_for_tool(n) == "unknown"]
    assert not unmapped, f"这些工具还没登记 provider 归属: {unmapped}"
