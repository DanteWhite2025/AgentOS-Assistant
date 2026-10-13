"""工具调用的展示元数据 —— 前端「正在搜索：XXX」那一行的数据来源。

设计决定（见方案文档）：人话文案由**服务端**下发，而不是客户端各自维护一张
映射表。工具是后端定义的，新增工具时改一行 Python 随后端发布即可生效；放在
客户端就得等三端各自发版，我们已经在联网搜索开关那次踩过这个坑。

同时下发 i18n key，客户端有对应翻译时用自己的，没有就回落到服务端的中文串——
两边的好处都要，且新工具永远不会显示成空白。
"""

from __future__ import annotations

import pytest

from app.tools.builtin import build_default_registry
from app.tools.registry import Tool, describe_tool_call


def test_every_registered_tool_has_display_title() -> None:
    """守卫测试：新增工具时忘了填文案，前端就会显示成原始英文函数名。"""
    missing = [t.name for t in build_default_registry().all_tools() if not t.display_title]
    assert missing == [], f"这些工具缺少展示文案：{missing}"


def test_title_key_is_derived_from_tool_name() -> None:
    """key 由工具名推导，不额外存一个字段——少一个能填错、能与工具名不一致的地方。"""
    tool = Tool(name="web_search", display_title="联网搜索")
    assert describe_tool_call(tool, {})["title_key"] == "tool.web_search.running"


def test_describe_exposes_title_and_whitelisted_params() -> None:
    tool = Tool(name="web_search", display_title="联网搜索", display_params=("query", "topic"))
    described = describe_tool_call(tool, {"query": "BTC 为什么跌", "topic": "news"})

    assert described["title"] == "联网搜索"
    assert described["params"] == {"query": "BTC 为什么跌", "topic": "news"}


def test_params_outside_the_whitelist_are_never_exposed() -> None:
    """白名单是正向的：工具新增了内部字段也不会自动流到前端。"""
    tool = Tool(name="web_search", display_title="联网搜索", display_params=("query",))
    described = describe_tool_call(
        tool, {"query": "BTC", "api_key": "tvly-secret", "idempotency_key": "idem-1"}
    )

    assert described["params"] == {"query": "BTC"}


def test_absent_params_are_omitted_rather_than_null() -> None:
    tool = Tool(name="web_search", display_title="联网搜索", display_params=("query", "topic"))
    assert describe_tool_call(tool, {"query": "BTC"})["params"] == {"query": "BTC"}


def test_title_falls_back_to_tool_name_when_unset() -> None:
    assert describe_tool_call(Tool(name="mystery_tool"), {})["title"] == "mystery_tool"


# ---- 展示即复核。交易草案的构造参数会展示给用户看，而最终签名权在用户手上，
# 所以这一行渲染出来的东西就是用户据以确认的依据。能让"看到的"和"签下的"
# 不一致的字符必须在这里清掉。 ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 一律写成转义序列：裸字符在编辑器和 diff 里同样不可见，
        # 评审时看不出这条用例到底在测什么（ruff 的 PLE2502/PLE2515 也会拦）。
        ("0.1\u202eCTB", "0.1CTB"),  # RLO 双向覆盖，能让数字和符号显示成反向
        ("BTC\u200b\u200bUSDT", "BTCUSDT"),  # 零宽空格
        ("BTC\U000e0041\U000e0042", "BTC"),  # Unicode Tags 区，完全不可见
        ("BTC\u2066\u2069", "BTC"),  # 双向隔离符
        ("BTC\x00\x07", "BTC"),  # 控制字符
    ],
)
def test_invisible_characters_are_stripped_from_params(raw: str, expected: str) -> None:
    tool = Tool(name="create_trade_draft", display_title="创建交易草案", display_params=("symbol",))
    assert describe_tool_call(tool, {"symbol": raw})["params"]["symbol"] == expected


def test_normal_text_survives_sanitizing() -> None:
    """净化不能误伤正常内容——中文、空格、标点、emoji 都要原样保留。"""
    tool = Tool(name="web_search", display_title="联网搜索", display_params=("query",))
    raw = "比特币 为什么跌了？ BTC/USDT 🚀"
    assert describe_tool_call(tool, {"query": raw})["params"]["query"] == raw


def test_long_values_are_truncated_for_display() -> None:
    tool = Tool(name="web_search", display_title="联网搜索", display_params=("query",))
    rendered = describe_tool_call(tool, {"query": "长" * 500})["params"]["query"]

    assert len(rendered) <= 121, "展示用的一行字，不该把整段查询词都塞进去"
    assert rendered.endswith("…")


def test_non_string_params_are_rendered_as_text() -> None:
    """交易参数里有数字和布尔值，前端拿到的应该是能直接显示的字符串。"""
    tool = Tool(
        name="create_trade_draft",
        display_title="创建交易草案",
        display_params=("qty", "leverage", "reduce_only", "symbols"),
    )
    described = describe_tool_call(
        tool, {"qty": 0.1, "leverage": 2, "reduce_only": False, "symbols": ["BTC", "ETH"]}
    )

    assert described["params"] == {
        "qty": "0.1",
        "leverage": "2",
        "reduce_only": "False",
        "symbols": "BTC, ETH",
    }


def test_trade_draft_exposes_full_construction_detail() -> None:
    """Dante 批复：交易类展示构造详情没问题，因为最终签名和确认权在用户。
    展示是用户复核的环节，缺了金额或杠杆反而让复核失去意义。"""
    tool = build_default_registry().get("create_trade_draft")
    assert tool is not None
    for field in ("symbol", "side", "qty", "price", "leverage"):
        assert field in tool.display_params, f"交易草案的 {field} 必须可见"


def test_search_exposes_the_query_itself() -> None:
    """Dante 批复：查询/检索类完整展示查询细节。"""
    tool = build_default_registry().get("web_search")
    assert tool is not None
    assert "query" in tool.display_params


def test_line_breaks_become_spaces_rather_than_disappearing() -> None:
    """直接删掉换行会把两个词粘成一个——"BTC\\nETH" 变 "BTCETH" 看着像个新标的。"""
    tool = Tool(name="web_search", display_title="联网搜索", display_params=("query",))
    described = describe_tool_call(tool, {"query": "BTC\nETH SOL"})

    assert described["params"]["query"] == "BTC ETH SOL"
