"""策略域内统一的金额解析 —— 金额一律走 Decimal，绝不落到 float。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Final

# 与 PG 侧 Numeric(scale=8) 对齐：分摊、比例等中间结果统一量化到 8 位小数
MONEY_QUANT: Final = Decimal("0.00000001")


def to_decimal(value: Any, field: str) -> Decimal:
    """把任意入参转成 Decimal；bool 与非数值文本一律拒绝。"""
    if isinstance(value, bool):
        raise ValueError(f"{field} 不能是布尔值")
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} 不是合法数值: {value!r}") from exc
