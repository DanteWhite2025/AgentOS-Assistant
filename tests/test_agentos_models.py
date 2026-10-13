"""CreateTradeDraftRequest 入参边界强校验 —— 系统边界的一次性校验全覆盖。

红线：这些字段直接进真测试网，脏数据必须在此 frozen dataclass 里被拦下。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.tools.agentos.models import CreateTradeDraftRequest


def _valid(**overrides):
    """一份合法基线，测试用 overrides 局部改写。"""
    base = {
        "unified_account_id": "uacc_1",
        "venue_account_id": "vacc_1",
        "instrument_id": "perp_hyperliquid_testnet_btc",
        "symbol": "BTC",
        "side": "BUY",
        "quantity": "1.5",
        "limit_price": "3000.00",
        "leverage": "2",
        "max_slippage": "0.005",
    }
    base.update(overrides)
    return CreateTradeDraftRequest(**base)


# ---- happy path ----


def test_valid_request_normalizes_and_defaults():
    req = _valid()
    assert req.order_type == "LIMIT"
    assert req.reduce_only is False
    assert req.side == "BUY"


def test_side_sell_allowed():
    assert _valid(side="SELL").side == "SELL"


# ---- Decimal 归一：四种输入类型都要归一为字符串 ----


@pytest.mark.parametrize(
    "value, expected",
    [
        ("1.5", "1.5"),
        ("  2.0  ", "2.0"),  # str 会 strip
        (3, "3"),  # int
        (3.5, "3.5"),  # float
        (Decimal("1.25"), "1.25"),  # Decimal
        ("0", "0"),  # 合法零
        ("-1.5", "-1.5"),  # 合法负数（正则允许 -）
    ],
)
def test_decimal_normalization(value, expected):
    assert _valid(quantity=value).quantity == expected


# ---- Decimal 非法值：正则必须拒绝 ----


@pytest.mark.parametrize("bad", ["abc", "", "1.2.3", "1,000", "1e3", ".5", "01.5", "--1", "+1"])
def test_illegal_decimal_rejected(bad):
    with pytest.raises(ValueError, match="Decimal"):
        _valid(quantity=bad)


def test_bool_is_rejected_for_decimal_field():
    # bool 是 int 子类，必须先挡掉，否则 True→"1" 会被误当合法
    with pytest.raises(ValueError, match="不能是布尔值"):
        _valid(quantity=True)


def test_unsupported_type_rejected_for_decimal_field():
    with pytest.raises(ValueError, match="类型不支持"):
        _valid(quantity=["1.5"])


# ---- 字符串必填字段不能为空/空白 ----


@pytest.mark.parametrize("field", ["unified_account_id", "venue_account_id", "symbol"])
def test_required_string_fields_not_blank(field):
    with pytest.raises(ValueError, match="不能为空"):
        _valid(**{field: "   "})


# ---- 枚举校验 ----


def test_illegal_side_rejected():
    with pytest.raises(ValueError, match="side 必须是"):
        _valid(side="LONG")


def test_illegal_order_type_rejected():
    with pytest.raises(ValueError, match="order_type 必须是"):
        _valid(order_type="MARKET")


def test_reduce_only_must_be_bool():
    with pytest.raises(ValueError, match="reduce_only 必须是 bool"):
        _valid(reduce_only="yes")


# ---- to_body：mandate_id 剔除 / 保留 ----


def test_to_body_drops_absent_mandate():
    body = _valid().to_body()
    assert "mandate_id" not in body
    assert body["order_type"] == "LIMIT"
    assert body["side"] == "BUY"
    assert body["reduce_only"] is False


def test_to_body_keeps_present_mandate():
    body = _valid(strategy_id="strategy_1", mandate_id="mand_1").to_body()
    assert body["strategy_id"] == "strategy_1"
    assert body["mandate_id"] == "mand_1"


def test_blank_instrument_id_is_rejected_when_present():
    with pytest.raises(ValueError, match="instrument_id 不能为空"):
        _valid(instrument_id="   ")


@pytest.mark.parametrize("field", ["strategy_id", "mandate_id"])
def test_optional_strategy_context_rejects_blank_when_present(field):
    with pytest.raises(ValueError, match=f"{field} 不能为空"):
        _valid(**{field: "   "})


def test_frozen_is_immutable():
    req = _valid()
    with pytest.raises(Exception):
        req.side = "SELL"  # type: ignore[misc]
