"""工具参数审计哈希 —— 全项目唯一实现（冻结规范）。

规范见 docs/research/tech-stack-assessment.md §6 与 §10：
  1. 剔除值为 None 的键（递归），避免"带/不带可选字段"的同义调用哈希不一致。
  2. json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)
     —— sort_keys 递归排序保证嵌套 key 顺序一致；固定 separators 消除空格差异；
        ensure_ascii=False 统一中文编码。
  3. 金额禁用 float：参数含 float 直接报错，强制调用方用字符串 Decimal，
     杜绝浮点序列化在不同平台/精度下产生不同字符串。
  4. 对规范化后的字符串取 SHA-256。

禁止在其它地方自行拼串计算 args_hash。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_SEPARATORS = (",", ":")


class FloatNotAllowedError(ValueError):
    """工具参数中出现 float —— 金额/数量必须以字符串 Decimal 传入。"""


def _assert_no_float(value: Any, path: str = "$") -> None:
    """递归校验：禁止任何 float（bool 是 int 子类，放行）。"""
    if isinstance(value, float):
        raise FloatNotAllowedError(
            f"float 不允许出现在工具参数中（位置 {path}）；金额/数量请用字符串 Decimal。"
        )
    if isinstance(value, dict):
        for key, item in value.items():
            _assert_no_float(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_float(item, f"{path}[{index}]")


def _drop_none(value: Any) -> Any:
    """递归剔除值为 None 的字典键（不改动原对象，返回新副本）。"""
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


def canonicalize(args: dict[str, Any]) -> str:
    """把工具参数规范化成可复现的 JSON 字符串（不含哈希）。"""
    _assert_no_float(args)
    canonical = _drop_none(args)
    return json.dumps(
        canonical,
        sort_keys=True,
        separators=_SEPARATORS,
        ensure_ascii=False,
    )


def args_hash(args: dict[str, Any]) -> str:
    """按冻结规范计算工具参数的 SHA-256 十六进制摘要。"""
    return hashlib.sha256(canonicalize(args).encode("utf-8")).hexdigest()
