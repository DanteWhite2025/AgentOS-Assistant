"""工具注册表与白名单二次校验测试。"""

from __future__ import annotations

from app.tools.registry import Tool, ToolRegistry


def _registry() -> ToolRegistry:
    return ToolRegistry().with_tool(Tool(name="get_positions", resource="positions"))


def test_with_tool_is_immutable() -> None:
    base = ToolRegistry()
    extended = base.with_tool(Tool(name="get_positions", resource="positions"))
    assert base.names() == ()
    assert extended.names() == ("get_positions",)


def test_allowed_when_registered_and_in_scope() -> None:
    assert _registry().is_allowed("get_positions", ["get_positions", "get_balances"])


def test_denied_when_not_in_scope() -> None:
    # 已注册但不在会话 scope 白名单 → 拒绝
    assert not _registry().is_allowed("get_positions", ["get_balances"])


def test_denied_when_not_registered() -> None:
    # 在 scope 里但服务端未注册（客户端伪造工具名） → 拒绝
    assert not _registry().is_allowed("evil_tool", ["evil_tool"])


def test_tool_function_spec_formatting() -> None:
    tool = Tool(
        name="get_market_price",
        description="查询实时价格",
        parameters={
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    )
    spec = tool.to_function_spec()
    assert spec["type"] == "function"
    assert spec["name"] == "get_market_price"
    assert spec["description"] == "查询实时价格"
    assert spec["parameters"]["properties"]["symbol"]["type"] == "string"


def test_builtin_registry_has_rich_function_specs() -> None:
    from app.tools.builtin import build_default_registry

    registry = build_default_registry()
    specs = registry.get_function_specs(["get_market_price", "get_positions", "create_trade_draft"])
    assert len(specs) == 3
    price_spec = next(s for s in specs if s["name"] == "get_market_price")
    pos_spec = next(s for s in specs if s["name"] == "get_positions")
    draft_spec = next(s for s in specs if s["name"] == "create_trade_draft")

    assert "symbol" in price_spec["parameters"]["properties"]
    assert "描述" in price_spec["description"] or "查询" in price_spec["description"]
    assert "持仓" in pos_spec["description"]
    assert draft_spec["parameters"]["required"] == [
        "symbol",
        "side",
        "order_type",
        "price",
        "leverage",
    ]
    assert draft_spec["parameters"]["properties"]["order_type"]["enum"] == ["limit"]
