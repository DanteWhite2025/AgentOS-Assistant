"""AgentOS Staging 交易草稿的入参模型 —— 系统边界的一次性强校验。

红线：这些字段直接进真测试网。入参在此 frozen dataclass 的 __post_init__ 里
按 OpenAPI 契约校验（枚举 / Decimal 正则），失败即抛，绝不把脏数据发上游。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Final, Literal

# spec: components.schemas.Decimal.pattern
_DECIMAL_RE: Final = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?$")

_SIDES: Final = ("BUY", "SELL")
_ORDER_TYPES: Final = ("LIMIT",)  # 契约当前仅支持 LIMIT
_MARKET_TYPES: Final = ("SPOT", "PERP")


def _to_decimal_str(value: Any, field: str) -> str:
    """把 int/float/str/Decimal 归一成契约要求的 Decimal 字符串，并校验格式。"""
    if isinstance(value, Decimal):
        text = format(value, "f")
    elif isinstance(value, bool):  # bool 是 int 子类，需先挡掉
        raise ValueError(f"{field} 不能是布尔值")
    elif isinstance(value, (int, float)):
        text = format(Decimal(str(value)), "f")
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ValueError(f"{field} 类型不支持: {type(value).__name__}")
    if not _DECIMAL_RE.match(text):
        raise ValueError(f"{field} 不是合法 Decimal: {text!r}")
    return text


@dataclass(frozen=True)
class CreateTradeDraftRequest:
    """POST /internal/v1/trade-drafts 的请求体（CreateTradeDraftRequest）。

    不可变：构造即校验。Core 当前 AGENT 契约要求 strategy_id 与
    mandate_id；保留 Optional 仅供旧的非策略调用方在本地明确失败迁移，
    策略交易路径必须同时提供两者。
    """

    unified_account_id: str
    venue_account_id: str
    instrument_id: str | None
    symbol: str
    side: str
    quantity: str
    limit_price: str
    leverage: str
    max_slippage: str
    reduce_only: bool = False
    order_type: str = "LIMIT"
    strategy_id: str | None = None
    mandate_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("unified_account_id", "venue_account_id", "symbol"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} 不能为空")
        if self.instrument_id is not None and not self.instrument_id.strip():
            raise ValueError("instrument_id 不能为空")
        for name in ("strategy_id", "mandate_id"):
            value = getattr(self, name)
            if value is not None and not str(value).strip():
                raise ValueError(f"{name} 不能为空")
        if self.side not in _SIDES:
            raise ValueError(f"side 必须是 {_SIDES}，收到 {self.side!r}")
        if self.order_type not in _ORDER_TYPES:
            raise ValueError(f"order_type 必须是 {_ORDER_TYPES}，收到 {self.order_type!r}")
        if not isinstance(self.reduce_only, bool):
            raise ValueError("reduce_only 必须是 bool")
        # Decimal 字段：object.__setattr__ 因 frozen，归一化后回写
        for name in ("quantity", "limit_price", "leverage", "max_slippage"):
            object.__setattr__(self, name, _to_decimal_str(getattr(self, name), name))

    def to_body(self) -> dict[str, Any]:
        """序列化为请求 JSON（剔除未提供的策略上下文字段）。"""
        body = asdict(self)
        for name in ("instrument_id", "strategy_id", "mandate_id"):
            if body.get(name) is None:
                body.pop(name, None)
        return body


@dataclass(frozen=True)
class MarketInstrument:
    """Core Market Catalog 的公开交易身份。

    Runtime 只把 ``instrument_id``、展示资产和限制用于规划。Venue symbol、
    Token 地址、精度和 Router 能力只能来自这份 Core 响应，不能来自用户或模型。
    """

    instrument_id: str
    market_type: Literal["SPOT", "PERP"]
    venue: str
    environment: str
    display_symbol: str
    venue_symbol: str
    base_asset: str
    quote_asset: str
    trade_supported: bool
    supported_order_types: tuple[str, ...]
    price_decimals: int
    size_decimals: int
    minimum_quantity: str
    minimum_notional: str
    maximum_leverage: str
    status: str
    stale: bool
    as_of: str
    chain_id: int | None = None
    token_in_address: str | None = None
    token_out_address: str | None = None
    token_in_decimals: int | None = None
    token_out_decimals: int | None = None
    token_in_ledger_decimals: int | None = None
    token_out_ledger_decimals: int | None = None

    @classmethod
    def from_dict(cls, value: object) -> MarketInstrument:
        if not isinstance(value, dict):
            raise ValueError("Core Market Catalog instrument 无效")
        market_type = str(value.get("market_type") or "").upper()
        if market_type not in _MARKET_TYPES:
            raise ValueError("Core Market Catalog market_type 无效")
        required = (
            "instrument_id",
            "venue",
            "environment",
            "display_symbol",
            "venue_symbol",
            "base_asset",
            "quote_asset",
            "minimum_quantity",
            "minimum_notional",
            "maximum_leverage",
            "status",
            "as_of",
        )
        missing = [name for name in required if not str(value.get(name) or "").strip()]
        if missing:
            raise ValueError("Core Market Catalog instrument 缺少字段")
        orders = value.get("supported_order_types")
        if not isinstance(orders, list) or not orders:
            raise ValueError("Core Market Catalog supported_order_types 无效")

        def integer(name: str, *, optional: bool = False) -> int | None:
            raw = value.get(name)
            if optional and raw is None:
                return None
            if isinstance(raw, bool):
                raise ValueError(f"Core Market Catalog {name} 无效")
            try:
                parsed = int(str(raw))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Core Market Catalog {name} 无效") from exc
            if parsed < 0:
                raise ValueError(f"Core Market Catalog {name} 无效")
            return parsed

        instrument = cls(
            instrument_id=str(value["instrument_id"]).strip(),
            market_type=market_type,  # type: ignore[arg-type]
            venue=str(value["venue"]).upper(),
            environment=str(value["environment"]).upper(),
            display_symbol=str(value["display_symbol"]).strip(),
            venue_symbol=str(value["venue_symbol"]).strip().upper(),
            base_asset=str(value["base_asset"]).strip().upper(),
            quote_asset=str(value["quote_asset"]).strip().upper(),
            trade_supported=value.get("trade_supported") is True,
            supported_order_types=tuple(str(item).upper() for item in orders),
            price_decimals=integer("price_decimals") or 0,
            size_decimals=integer("size_decimals") or 0,
            minimum_quantity=_to_decimal_str(value["minimum_quantity"], "minimum_quantity"),
            minimum_notional=_to_decimal_str(value["minimum_notional"], "minimum_notional"),
            maximum_leverage=_to_decimal_str(value["maximum_leverage"], "maximum_leverage"),
            status=str(value["status"]).upper(),
            stale=value.get("stale") is True,
            as_of=str(value["as_of"]),
            chain_id=integer("chain_id", optional=True),
            token_in_address=(
                str(value.get("token_in_address")).lower()
                if value.get("token_in_address") is not None
                else None
            ),
            token_out_address=(
                str(value.get("token_out_address")).lower()
                if value.get("token_out_address") is not None
                else None
            ),
            token_in_decimals=integer("token_in_decimals", optional=True),
            token_out_decimals=integer("token_out_decimals", optional=True),
            token_in_ledger_decimals=integer("token_in_ledger_decimals", optional=True),
            token_out_ledger_decimals=integer("token_out_ledger_decimals", optional=True),
        )
        instrument.require_tradable()
        return instrument

    def require_tradable(self) -> None:
        if self.stale:
            raise ValueError("Core Market Catalog 已过期，禁止创建 Draft")
        if not self.trade_supported or self.status != "ACTIVE":
            raise ValueError("Core 当前未开放该交易标的")
        if self.market_type == "PERP":
            if self.venue != "HYPERLIQUID" or self.environment != "TESTNET":
                raise ValueError("Core PERP instrument 场所或环境无效")
        elif (
            self.venue != "PANCAKESWAP"
            or self.environment != "TESTNET"
            or self.chain_id != 97
            or not self.token_in_address
            or not self.token_out_address
            or self.token_in_decimals is None
            or self.token_out_decimals is None
        ):
            raise ValueError("Core SPOT instrument 契约不完整")


@dataclass(frozen=True)
class CreateSpotSwapDraftRequest:
    """POST /internal/v1/swap-drafts 的 Core-owned exact-input 请求。"""

    instrument_id: str
    unified_account_id: str
    chain_id: int
    sell_token: str
    buy_token: str
    sell_amount_minor: str
    max_slippage_bps: int

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise ValueError("instrument_id 不能为空")
        if not self.unified_account_id.strip():
            raise ValueError("unified_account_id 不能为空")
        if self.chain_id != 97:
            raise ValueError("Spot Draft 当前只允许 BSC Testnet chain 97")
        for name in ("sell_token", "buy_token"):
            value = str(getattr(self, name)).lower()
            if not re.fullmatch(r"0x[0-9a-f]{40}", value):
                raise ValueError(f"{name} 不是合法 Core Catalog 地址")
            object.__setattr__(self, name, value)
        if self.sell_token == self.buy_token:
            raise ValueError("Spot Draft 的 sell_token 与 buy_token 不能相同")
        if not re.fullmatch(r"[1-9][0-9]*", str(self.sell_amount_minor)):
            raise ValueError("sell_amount_minor 必须是正整数")
        if not 1 <= self.max_slippage_bps <= 1000:
            raise ValueError("max_slippage_bps 必须在 1..1000")

    def to_body(self) -> dict[str, Any]:
        return asdict(self)
