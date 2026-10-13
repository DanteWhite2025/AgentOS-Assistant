"""args_hash 冻结规范测试（TDD）。"""

from __future__ import annotations

import pytest

from app.audit.hashing import FloatNotAllowedError, args_hash, canonicalize


def test_key_order_irrelevant() -> None:
    a = args_hash({"symbol": "ETH", "side": "long", "qty": "1"})
    b = args_hash({"qty": "1", "side": "long", "symbol": "ETH"})
    assert a == b


def test_nested_key_order_irrelevant() -> None:
    a = args_hash({"o": {"a": "1", "b": "2"}})
    b = args_hash({"o": {"b": "2", "a": "1"}})
    assert a == b


def test_none_keys_dropped() -> None:
    with_none = args_hash({"symbol": "ETH", "note": None})
    without = args_hash({"symbol": "ETH"})
    assert with_none == without


def test_chinese_not_escaped() -> None:
    # ensure_ascii=False：中文原样进哈希，不转 \uXXXX
    assert "备注" in canonicalize({"memo": "备注"})


def test_fixed_separators_no_spaces() -> None:
    assert canonicalize({"a": "1", "b": "2"}) == '{"a":"1","b":"2"}'


def test_float_rejected() -> None:
    with pytest.raises(FloatNotAllowedError):
        args_hash({"qty": 1.5})  # 金额/数量必须用字符串 Decimal


def test_float_rejected_nested() -> None:
    with pytest.raises(FloatNotAllowedError):
        args_hash({"order": {"price": 3000.0}})


def test_bool_allowed() -> None:
    # bool 是 int 子类，不算 float，放行
    assert args_hash({"reduce_only": True})


def test_string_amount_stable() -> None:
    h = args_hash({"qty": "1.50"})
    assert h == args_hash({"qty": "1.50"})
    assert len(h) == 64  # SHA-256 hex
