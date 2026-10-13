"""交易流编排 —— 意图解析(LLM) → 构造草稿 → 渲染确认卡片。

职责边界：智能体只到「构造草稿 + 展示确认卡片」；confirm 由 Mobile
直接调用 Core USER 接口。Runtime 不接收 Step-Up Token，也不能替用户确认。

LLM 只解析「交易要素」（symbol/side/数量/限价…），账户身份类字段
（unified_account_id / venue_account_id）由会话上下文注入，绝不让模型编造。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.agents.llm import LLMMessage, LLMProvider
from app.tools.agentos.client import AgentOsStagingClient
from app.tools.agentos.models import CreateTradeDraftRequest, MarketInstrument

_PARSER_SYSTEM = (
    "你是交易意图解析器。把用户的下单意图解析成严格 JSON，只输出 JSON 本体，不要任何解释或代码块围栏。\n"
    "字段：symbol(string，交易所裸币名，如 BTC；不要带计价币或连字符)、"
    "side(必须是 BUY 或 SELL)、quantity(string 数量)、"
    "limit_price(string 限价)、leverage(string 可选)、max_slippage(string 可选，如 0.005)、"
    "reduce_only(bool 可选)。仅支持限价单(LIMIT)。缺失的可选字段直接省略。"
)

_SIDE_CN = {"BUY": "买入", "SELL": "卖出"}

# 团队约定（2026-08-08，与 Core owner Bruce 对齐）：symbol 用交易所裸币名（与 Hyperliquid 统一），如 BTC。
# 长的排前面：先剥 USDT/USDC，再剥 USD。Gemini Live 会把 BTCUSD/ETHUSD 当外汇代码发出，
# 而 Hyperliquid venue_symbol 是 BTC/ETH；不剥 USD 时 Core 会回 INVALID_SYMBOL。
# 剩余 base 至少 2 个字符，避免把 BUSD 剥成 B。
_KNOWN_QUOTES = ("USDT", "USDC", "USD")
_MIN_QUOTE_BASE_LEN = 2
# 合约类后缀。LLM 常把 Catalog 的 display_symbol（如 ETH-PERP）或自己编的
# ETHPERP 当成 symbol 发过来，而 Core 的 venue_symbol 是裸币名 ETH ——
# 线上实测过 query=ETHPERP 查不到标的。长的排前面，先剥 PERPETUAL 再剥 PERP。
_CONTRACT_SUFFIXES = ("PERPETUAL", "PERPS", "PERP", "SWAP")


def _strip_contract_suffix(value: str) -> str:
    """剥掉合约后缀；后缀本身就是资产名时（裸 PERP）原样返回。"""

    for suffix in _CONTRACT_SUFFIXES:
        if value.endswith(suffix) and value != suffix:
            return value[: -len(suffix)]
    return value


def _strip_known_quote(value: str) -> str:
    """剥掉连写计价币；剥完后 base 太短则原样返回。"""

    for quote in _KNOWN_QUOTES:
        if value.endswith(quote) and value != quote:
            base = value[: -len(quote)]
            if len(base) >= _MIN_QUOTE_BASE_LEN:
                return base
    return value


def normalize_symbol(raw: str) -> str:
    """把 symbol 规范成交易所裸币名（如 BTC）——与 Core/Hyperliquid 统一。

    LLM 输出不稳定（可能给 BTC / BTCUSDT / BTCUSD / BTC-USDT / BTC/USDT），此处确定性兜底：
    统一大写去空白，'/'、'_'、空格 → '-'；含 '-' 取连字符前的 base；
    连写(BTCUSDT/BTCUSD)剥离已知计价币；否则原样（已是裸币名）。
    """
    s = raw.strip().upper().replace("/", "-").replace("_", "-").replace(" ", "-")
    if "-" in s:
        return _strip_contract_suffix(s.split("-", 1)[0]) or s.split("-", 1)[0]
    return _strip_contract_suffix(_strip_known_quote(s))


def catalog_asset_query(raw: str) -> str:
    """Reduce a model/user symbol to a Catalog search term, never a venue symbol.

    Values such as ``BTCUSDC`` or ``BTC/USDT`` are only used to find the base
    asset in Core's Catalog.  The value sent to the venue market-data endpoint
    is always the unique Catalog ``venue_symbol`` resolved afterwards.
    """

    normalized = re.sub(r"[^A-Z0-9]", "", raw.strip().upper())
    if not normalized:
        raise MarketInstrumentResolutionError(
            "MARKET_INSTRUMENT_INVALID", "行情标的不能为空", 400
        )
    normalized = _strip_known_quote(_strip_contract_suffix(normalized))
    if not normalized or len(normalized) > 32:
        raise MarketInstrumentResolutionError(
            "MARKET_INSTRUMENT_INVALID", "行情标的格式无效", 400
        )
    return normalized


class IntentParseError(ValueError):
    """LLM 未能解析出合法交易意图。"""


class NoTradableAccountError(RuntimeError):
    """token 名下没有可交易账户（无 ACTIVE + venue_account_id 的账户）。"""


class StrategyBindingError(RuntimeError):
    """Core StrategyBinding 不完整、非 ACTIVE 或与 Agent 账户不一致。"""


class TradingReadinessError(RuntimeError):
    """Core USER Trading Readiness 响应缺少正式账户上下文。"""


class MarketInstrumentResolutionError(RuntimeError):
    """A safe, classified failure while resolving Core-owned instrument identity."""

    def __init__(self, reason_code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.status_code = status_code


def resolve_unique_market_instrument(
    response: dict[str, Any],
    *,
    query: str,
    market_type: str,
) -> MarketInstrument:
    """Resolve one exact, tradable instrument from a Core Catalog response.

    Pair-like model output is accepted only as a *search hint*.  Runtime never
    derives or guesses the exchange symbol: the returned ``venue_symbol`` and
    all execution identity come from the single matching Catalog record.
    """

    expected_type = market_type.strip().upper()
    if expected_type not in {"SPOT", "PERP"}:
        raise MarketInstrumentResolutionError(
            "MARKET_TYPE_INVALID", "交易类型必须是 SPOT 或 PERP", 400
        )
    data = response.get("data")
    meta = response.get("meta")
    if not isinstance(data, list) or not isinstance(meta, dict):
        raise MarketInstrumentResolutionError(
            "MARKET_CATALOG_INVALID", "Core Market Catalog 响应不完整", 503
        )
    if meta.get("stale") is True:
        raise MarketInstrumentResolutionError(
            "MARKET_CATALOG_STALE", "Core Market Catalog 已过期，请稍后重试", 503
        )

    asset_query = catalog_asset_query(query)
    raw_matches = [
        value
        for value in data
        if isinstance(value, dict)
        and str(value.get("market_type") or "").upper() == expected_type
        and asset_query
        in {
            str(value.get("base_asset") or "").upper(),
            re.sub(r"[^A-Z0-9]", "", str(value.get("venue_symbol") or "").upper()),
            re.sub(r"[^A-Z0-9]", "", str(value.get("display_symbol") or "").upper()),
        }
    ]
    if not raw_matches:
        raise MarketInstrumentResolutionError(
            "MARKET_INSTRUMENT_NOT_FOUND",
            "Core Market Catalog 未找到对应行情标的",
            400,
        )
    if len(raw_matches) != 1:
        raise MarketInstrumentResolutionError(
            "MARKET_INSTRUMENT_AMBIGUOUS",
            "Core Market Catalog 返回了多个匹配标的，请明确交易类型或标的",
            400,
        )
    value = raw_matches[0]
    if value.get("stale") is True:
        raise MarketInstrumentResolutionError(
            "MARKET_INSTRUMENT_STALE", "Core 行情标的目录已过期，请稍后重试", 503
        )
    if value.get("trade_supported") is not True or str(value.get("status") or "").upper() != "ACTIVE":
        raise MarketInstrumentResolutionError(
            "MARKET_INSTRUMENT_NOT_TRADABLE", "Core 当前未开放该交易标的", 403
        )
    try:
        return MarketInstrument.from_dict(value)
    except ValueError as exc:
        raise MarketInstrumentResolutionError(
            "MARKET_CATALOG_INVALID", "Core Market Catalog 标的数据不完整", 503
        ) from exc


def parse_account_summary(response: dict[str, Any]) -> dict[str, Any]:
    """Validate the formal Agent account-summary without inventing venue equity.

    Core currently returns one operational Ledger balance plus Agent-scoped
    positions and orders.  None of those fields is an authoritative
    Hyperliquid account-equity value, so the distinction is explicit in every
    tool result.
    """

    data = response.get("data")
    if not isinstance(data, dict):
        raise TradingReadinessError("Core Account Summary 缺少 data")
    balance = data.get("balance")
    positions = data.get("positions")
    orders = data.get("orders")
    if (
        not isinstance(balance, dict)
        or not isinstance(positions, list)
        or not isinstance(orders, list)
    ):
        raise TradingReadinessError("Core Account Summary 契约不完整")
    return {
        "source": "CORE_ACCOUNT_SUMMARY",
        "balance": balance,
        "balance_semantics": "CORE_LEDGER_NOT_VENUE_EQUITY",
        "positions": positions,
        "orders": orders,
        "venue_equity": {
            "status": "NOT_AVAILABLE",
            "code": "EQUITY_UNAVAILABLE",
            "reason": "CORE_ACCOUNT_SUMMARY_HAS_NO_AUTHORITATIVE_VENUE_EQUITY",
        },
        "request_id": str(response.get("request_id") or "")[:128] or None,
    }


@dataclass(frozen=True)
class TradingReadinessContext:
    """Current USER's read-only delegated-trading state, resolved by Core.

    This object deliberately contains no USER token.  Account and control-plane
    identifiers come only from ``GET /api/v1/me/trading-readiness``.
    """

    unified_account_id: str
    venue_account_id: str
    agent_trading_account_id: str
    agent_id: str
    ledger_balance_by_asset: dict[str, str]
    venue_balance_by_asset: dict[str, str]
    venue_margin_minor: int
    capital_limit_minor: int
    per_order_limit_minor: int
    total_exposure_limit_minor: int
    daily_notional_limit_minor: int
    daily_notional_used_minor: int
    daily_notional_remaining_minor: int
    available_trade_notional_minor: int
    mandate_id: str
    mandate_status: str
    confirmation_mode: str
    allowed_symbols: tuple[str, ...]
    allowed_order_types: tuple[str, ...]
    max_leverage: str
    max_slippage: str
    strategy_id: str
    strategy_status: str
    can_create_agent_draft: bool
    blockers: tuple[str, ...]
    stale: bool
    request_id: str | None

    def tool_output(self) -> dict[str, Any]:
        return {
            "source": "CORE_TRADING_READINESS",
            "unified_account_id": self.unified_account_id,
            "venue_account_id": self.venue_account_id,
            "agent_trading_account": {
                "id": self.agent_trading_account_id,
                "agent_id": self.agent_id,
                "status": "ACTIVE",
            },
            "ledger_balance_by_asset": dict(self.ledger_balance_by_asset),
            "venue_balance_by_asset": dict(self.venue_balance_by_asset),
            "venue_margin_minor": str(self.venue_margin_minor),
            "capital_limit_minor": str(self.capital_limit_minor),
            "per_order_limit_minor": str(self.per_order_limit_minor),
            "total_exposure_limit_minor": str(self.total_exposure_limit_minor),
            "total_exposure_remaining": {
                "status": "NOT_AVAILABLE",
                "code": "TOTAL_EXPOSURE_REMAINING_UNAVAILABLE",
            },
            "daily_notional_limit_minor": str(self.daily_notional_limit_minor),
            "daily_notional_used_minor": str(self.daily_notional_used_minor),
            "daily_notional_remaining_minor": str(self.daily_notional_remaining_minor),
            "available_trade_notional_minor": str(self.available_trade_notional_minor),
            "available_trade_notional_is_upper_bound": True,
            "venue_equity": {
                "status": "NOT_AVAILABLE",
                "code": "EQUITY_UNAVAILABLE",
                "reason": "CORE_HAS_NO_AUTHORITATIVE_VENUE_EQUITY_FIELD",
            },
            "mandate": {
                "id": self.mandate_id,
                "status": self.mandate_status,
                "confirmation_mode": self.confirmation_mode,
                "max_notional_per_order_minor": str(self.per_order_limit_minor),
                "max_daily_notional_minor": str(self.daily_notional_limit_minor),
                "max_total_exposure_minor": str(self.total_exposure_limit_minor),
                "allowed_symbols": list(self.allowed_symbols),
                "allowed_order_types": list(self.allowed_order_types),
                "max_leverage": self.max_leverage,
                "max_slippage": self.max_slippage,
            },
            "strategy_binding": {
                "strategy_id": self.strategy_id,
                "status": self.strategy_status,
            },
            "can_create_agent_draft": self.can_create_agent_draft,
            "blockers": list(self.blockers),
            "stale": self.stale,
            "request_id": self.request_id,
        }


def _positive_int(value: object, field: str, *, allow_zero: bool = False) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise TradingReadinessError(f"Core Trading Readiness 的 {field} 无效") from exc
    if parsed < 0 or (parsed == 0 and not allow_zero):
        raise TradingReadinessError(f"Core Trading Readiness 的 {field} 无效")
    return parsed


def _minor_map(value: object, field: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TradingReadinessError(f"Core Trading Readiness 的 {field} 无效")
    result: dict[str, str] = {}
    for raw_asset, raw_amount in value.items():
        asset = str(raw_asset).strip().upper()
        if not asset:
            raise TradingReadinessError(f"Core Trading Readiness 的 {field} 无效")
        amount = _positive_int(raw_amount, f"{field}.{asset}", allow_zero=True)
        result[asset] = str(amount)
    return result


def _decimal_map(value: object, field: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise TradingReadinessError(f"Core Trading Readiness 的 {field} 无效")
    result: dict[str, str] = {}
    for raw_asset, raw_amount in value.items():
        asset = str(raw_asset).strip().upper()
        try:
            amount = Decimal(str(raw_amount))
        except (InvalidOperation, ValueError) as exc:
            raise TradingReadinessError(
                f"Core Trading Readiness 的 {field}.{asset or 'UNKNOWN'} 无效"
            ) from exc
        if not asset or not amount.is_finite():
            raise TradingReadinessError(f"Core Trading Readiness 的 {field} 无效")
        result[asset] = format(amount, "f")
    return result


def parse_trading_readiness(
    response: dict[str, Any],
    *,
    expected_agent_id: str,
    expected_strategy_id: str | None = None,
) -> TradingReadinessContext:
    """Validate and reduce the current formal Core readiness response.

    Read-only callers may still inspect a not-ready result.  Draft creation must
    additionally call :func:`require_draft_ready` before writing anything.
    """

    data = response.get("data")
    if not isinstance(data, dict):
        raise TradingReadinessError("Core Trading Readiness 缺少 data")
    account = data.get("agent_trading_account")
    mandate = data.get("mandate")
    binding = data.get("strategy_binding")
    if (
        not isinstance(account, dict)
        or not isinstance(mandate, dict)
        or not isinstance(binding, dict)
    ):
        raise TradingReadinessError("Core Trading Readiness 缺少 Agent 账户、Mandate 或策略绑定")

    unified_account_id = str(data.get("unified_account_id") or "").strip()
    venue_account_id = str(data.get("venue_account_id") or "").strip()
    account_id = str(account.get("id") or "").strip()
    agent_id = str(account.get("agent_id") or "").strip()
    account_unified_id = str(account.get("unified_account_id") or "").strip()
    account_venue_id = str(account.get("venue_account_id") or "").strip()
    if not all((unified_account_id, venue_account_id, account_id)):
        raise TradingReadinessError("Core Trading Readiness 缺少正式账户标识")
    if account_unified_id != unified_account_id or account_venue_id != venue_account_id:
        raise TradingReadinessError("Core Trading Readiness 的账户关联不一致")
    if agent_id != expected_agent_id:
        raise TradingReadinessError("Core Trading Readiness 返回了非预期 Agent")

    strategy_id = str(binding.get("strategy_id") or "").strip()
    if expected_strategy_id is not None and strategy_id != expected_strategy_id:
        raise TradingReadinessError("Core Trading Readiness 返回了非预期 Strategy Binding")
    if str(binding.get("agent_trading_account_id") or "").strip() != account_id:
        raise TradingReadinessError("Strategy Binding 与 Agent Trading Account 不一致")
    if str(binding.get("venue_account_id") or "").strip() != venue_account_id:
        raise TradingReadinessError("Strategy Binding 与 Venue Account 不一致")

    mandate_id = str(mandate.get("id") or "").strip()
    if not mandate_id or str(binding.get("mandate_id") or "").strip() != mandate_id:
        raise TradingReadinessError("Strategy Binding 与 Mandate 不一致")
    confirmation_mode = str(mandate.get("confirmation_mode") or "").upper()
    if confirmation_mode != "ALWAYS_CONFIRM":
        raise TradingReadinessError("Runtime 仅接受 ALWAYS_CONFIRM Mandate")
    allowed_symbols_raw = mandate.get("allowed_symbols")
    allowed_orders_raw = mandate.get("allowed_order_types")
    if not isinstance(allowed_symbols_raw, list) or not allowed_symbols_raw:
        raise TradingReadinessError("Core Mandate 缺少 allowed_symbols")
    if not isinstance(allowed_orders_raw, list) or not allowed_orders_raw:
        raise TradingReadinessError("Core Mandate 缺少 allowed_order_types")
    allowed_symbols = tuple(
        str(value).strip().upper() for value in allowed_symbols_raw if str(value).strip()
    )
    allowed_order_types = tuple(
        str(value).strip().upper() for value in allowed_orders_raw if str(value).strip()
    )
    if len(allowed_symbols) != len(allowed_symbols_raw) or len(allowed_order_types) != len(
        allowed_orders_raw
    ):
        raise TradingReadinessError("Core Mandate 允许范围无效")
    try:
        max_leverage = Decimal(str(mandate.get("max_leverage")))
        max_slippage = Decimal(str(mandate.get("max_slippage")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TradingReadinessError("Core Mandate 杠杆或滑点限制无效") from exc
    if (
        not max_leverage.is_finite()
        or max_leverage <= 0
        or not max_slippage.is_finite()
        or max_slippage <= 0
    ):
        raise TradingReadinessError("Core Mandate 杠杆或滑点限制无效")

    venue_margin_minor = _positive_int(
        data.get("venue_margin_minor"), "venue_margin_minor", allow_zero=True
    )
    ledger_balance_by_asset = _minor_map(
        data.get("ledger_balance_by_asset"), "ledger_balance_by_asset"
    )
    venue_balance_by_asset = _decimal_map(
        data.get("venue_balance_by_asset"), "venue_balance_by_asset"
    )
    capital_limit_minor = _positive_int(binding.get("capital_limit_minor"), "capital_limit_minor")
    max_order_minor = _positive_int(
        mandate.get("max_notional_per_order_minor"),
        "max_notional_per_order_minor",
    )
    max_total_exposure_minor = _positive_int(
        mandate.get("max_total_exposure_minor"),
        "max_total_exposure_minor",
    )
    max_daily_notional_minor = _positive_int(
        mandate.get("max_daily_notional_minor"),
        "max_daily_notional_minor",
    )
    daily_notional_limit_minor = _positive_int(
        data.get("daily_notional_limit_minor"),
        "daily_notional_limit_minor",
        allow_zero=True,
    )
    daily_notional_used_minor = _positive_int(
        data.get("daily_notional_used_minor"),
        "daily_notional_used_minor",
        allow_zero=True,
    )
    daily_notional_remaining_minor = _positive_int(
        data.get("daily_notional_remaining_minor"),
        "daily_notional_remaining_minor",
        allow_zero=True,
    )
    if max_daily_notional_minor != daily_notional_limit_minor:
        raise TradingReadinessError("Core Trading Readiness 的日额度与 Mandate 不一致")
    expected_daily_remaining = max(daily_notional_limit_minor - daily_notional_used_minor, 0)
    if daily_notional_remaining_minor != expected_daily_remaining:
        raise TradingReadinessError("Core Trading Readiness 的日剩余额度不一致")
    available_minor = min(
        venue_margin_minor,
        capital_limit_minor,
        max_order_minor,
        max_total_exposure_minor,
        daily_notional_remaining_minor,
    )
    blockers = data.get("blockers")
    blocker_values = (
        tuple(str(value)[:128] for value in blockers if isinstance(value, str))
        if isinstance(blockers, list)
        else ()
    )
    return TradingReadinessContext(
        unified_account_id=unified_account_id,
        venue_account_id=venue_account_id,
        agent_trading_account_id=account_id,
        agent_id=agent_id,
        ledger_balance_by_asset=ledger_balance_by_asset,
        venue_balance_by_asset=venue_balance_by_asset,
        venue_margin_minor=venue_margin_minor,
        capital_limit_minor=capital_limit_minor,
        per_order_limit_minor=max_order_minor,
        total_exposure_limit_minor=max_total_exposure_minor,
        daily_notional_limit_minor=daily_notional_limit_minor,
        daily_notional_used_minor=daily_notional_used_minor,
        daily_notional_remaining_minor=daily_notional_remaining_minor,
        available_trade_notional_minor=available_minor,
        mandate_id=mandate_id,
        mandate_status=str(mandate.get("status") or "UNKNOWN").upper(),
        confirmation_mode=confirmation_mode,
        allowed_symbols=allowed_symbols,
        allowed_order_types=allowed_order_types,
        max_leverage=format(max_leverage, "f"),
        max_slippage=format(max_slippage, "f"),
        strategy_id=strategy_id,
        strategy_status=str(binding.get("status") or "UNKNOWN").upper(),
        can_create_agent_draft=data.get("can_create_agent_draft") is True,
        blockers=blocker_values,
        stale=data.get("stale") is True,
        request_id=str(response.get("request_id") or "")[:128] or None,
    )


def require_draft_ready(readiness: TradingReadinessContext) -> None:
    """Fail closed before creating a Core Draft; never confirms or executes it."""

    if readiness.stale:
        raise TradingReadinessError("Core Trading Readiness 已过期")
    if not readiness.can_create_agent_draft:
        blockers = ", ".join(readiness.blockers) or "UNKNOWN"
        raise TradingReadinessError(f"当前账户不可创建 Agent Draft：{blockers}")
    if readiness.mandate_status != "ACTIVE":
        raise TradingReadinessError("Mandate 当前不是 ACTIVE")
    if readiness.strategy_status != "ACTIVE":
        raise TradingReadinessError("Strategy Binding 当前不是 ACTIVE")


@dataclass(frozen=True, slots=True)
class SpotTradingReadinessContext:
    unified_account_id: str


def parse_spot_trading_readiness(response: dict[str, Any]) -> SpotTradingReadinessContext:
    """Parse USER token readiness/account response for one-off Spot Drafts."""

    data = response.get("data")
    if not isinstance(data, dict):
        raise TradingReadinessError("Core Trading Readiness 缺少 data")
    unified_account_id = str(data.get("unified_account_id") or "").strip()
    if not unified_account_id:
        raise TradingReadinessError("Core Trading Readiness 缺少 unified_account_id")
    return SpotTradingReadinessContext(unified_account_id=unified_account_id)


@dataclass(frozen=True)
class StrategyExecutionContext:
    """从 Core 权威绑定解析出的策略执行上下文。"""

    strategy_id: str
    agent_trading_account_id: str
    unified_account_id: str
    venue_account_id: str
    mandate_id: str
    authorization_grant_id: str
    capital_limit_minor: int
    market_types: tuple[str, ...]
    allowed_assets: tuple[str, ...]
    allowed_instruments: tuple[str, ...]
    max_order_notional_minor: int
    max_daily_notional_minor: int
    max_leverage: str
    max_slippage: str


async def discover_trading_account(client: AgentOsStagingClient) -> tuple[str, str]:
    """按 token 身份发现可交易账户，返回 (unified_account_id, venue_account_id)。

    红线：账户身份从 token(user_id+agent_id) 派生，**不信任客户端传入的账户 id**。
    选取第一个 ACTIVE 且带 venue_account_id 的账户。
    """
    resp = await client.get_agent_trading_accounts()
    accounts = resp.get("data") or []
    for account in accounts:
        if str(account.get("status", "")).upper() == "ACTIVE" and account.get("venue_account_id"):
            return str(account["unified_account_id"]), str(account["venue_account_id"])
    raise NoTradableAccountError("token 名下无可交易账户（需 ACTIVE 且有 venue_account_id）")


async def resolve_strategy_execution_context(
    client: AgentOsStagingClient,
    strategy_id: str,
    *,
    expected_agent_id: str | None = None,
) -> StrategyExecutionContext:
    """用 Core StrategyBinding 精确解析策略账户，不从 ACTIVE 列表中猜测。

    Core 已按 AGENT Token 校验 binding 归属；Runtime 再做结构与交叉关系校验，
    防止上游响应缺字段或本地误把另一个策略的账户用于当前 Draft。
    """
    requested_strategy_id = strategy_id.strip()
    if not requested_strategy_id:
        raise StrategyBindingError("strategy_id 不能为空")

    binding_response = await client.get_strategy_binding(requested_strategy_id)
    binding = binding_response.get("data") or {}
    if str(binding.get("strategy_id", "")).strip() != requested_strategy_id:
        raise StrategyBindingError("Core 返回的 StrategyBinding 与请求 strategy_id 不一致")
    if str(binding.get("status", "")).upper() != "ACTIVE":
        raise StrategyBindingError(
            f"Core StrategyBinding 当前不可新建订单：{binding.get('status') or 'UNKNOWN'}"
        )
    binding_agent_id = str(binding.get("agent_id") or "").strip()
    if expected_agent_id is not None and binding_agent_id != expected_agent_id:
        raise StrategyBindingError("Core StrategyBinding 返回了非预期 Agent")

    required = (
        "agent_trading_account_id",
        "venue_account_id",
        "mandate_id",
        "authorization_grant_id",
    )
    missing = [name for name in required if not str(binding.get(name, "")).strip()]
    if missing:
        raise StrategyBindingError(f"Core StrategyBinding 缺少字段：{', '.join(missing)}")
    try:
        capital_limit_minor = int(binding["capital_limit_minor"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StrategyBindingError("Core StrategyBinding 的 capital_limit_minor 无效") from exc
    if capital_limit_minor <= 0:
        raise StrategyBindingError("Core StrategyBinding 的 capital_limit_minor 必须大于 0")
    market_types_raw = binding.get("market_types")
    allowed_assets_raw = binding.get("allowed_assets")
    if not isinstance(market_types_raw, list) or not market_types_raw:
        raise StrategyBindingError("Core StrategyBinding 缺少 market_types 权威范围")
    if not isinstance(allowed_assets_raw, list) or not allowed_assets_raw:
        raise StrategyBindingError("Core StrategyBinding 缺少 allowed_assets 权威范围")
    market_types = tuple(str(value).strip().upper() for value in market_types_raw)
    allowed_assets = tuple(
        str(value).strip().upper() for value in allowed_assets_raw if str(value).strip()
    )
    if any(value not in {"SPOT", "PERP"} for value in market_types):
        raise StrategyBindingError("Core StrategyBinding 交易范围无效")
    allowed_instruments_raw = binding.get("allowed_instruments") or []
    if not isinstance(allowed_instruments_raw, list):
        raise StrategyBindingError("Core StrategyBinding allowed_instruments 无效")
    allowed_instruments = tuple(
        str(value).strip() for value in allowed_instruments_raw if str(value).strip()
    )

    def strategy_limit(name: str, fallback: object) -> int:
        try:
            result = int(str(binding.get(name, fallback)))
        except (TypeError, ValueError) as exc:
            raise StrategyBindingError(f"Core StrategyBinding 的 {name} 无效") from exc
        if result <= 0:
            raise StrategyBindingError(f"Core StrategyBinding 的 {name} 无效")
        return result

    max_order_notional_minor = strategy_limit("max_order_notional_minor", capital_limit_minor)
    max_daily_notional_minor = strategy_limit("max_daily_notional_minor", capital_limit_minor)
    try:
        strategy_max_leverage = Decimal(str(binding.get("max_leverage")))
        strategy_max_slippage = Decimal(str(binding.get("max_slippage")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise StrategyBindingError("Core StrategyBinding 杠杆或滑点限制无效") from exc
    if (
        not strategy_max_leverage.is_finite()
        or strategy_max_leverage <= 0
        or not strategy_max_slippage.is_finite()
        or strategy_max_slippage <= 0
    ):
        raise StrategyBindingError("Core StrategyBinding 杠杆或滑点限制无效")

    account_response = await client.get_agent_trading_accounts()
    accounts = account_response.get("data") or []
    account_id = str(binding["agent_trading_account_id"])
    account = next((item for item in accounts if str(item.get("id", "")) == account_id), None)
    if account is None:
        raise StrategyBindingError("StrategyBinding 对应的 Agent Trading Account 不可见")
    if str(account.get("status", "")).upper() != "ACTIVE":
        raise StrategyBindingError(
            f"StrategyBinding 对应账户不可交易：{account.get('status') or 'UNKNOWN'}"
        )
    if str(account.get("agent_id") or "").strip() != binding_agent_id:
        raise StrategyBindingError("StrategyBinding 与 Agent Trading Account 的 Agent 不一致")
    unified_account_id = str(account.get("unified_account_id", "")).strip()
    account_venue_id = str(account.get("venue_account_id", "")).strip()
    binding_venue_id = str(binding["venue_account_id"]).strip()
    if not unified_account_id:
        raise StrategyBindingError("Agent Trading Account 缺少 unified_account_id")
    if account_venue_id != binding_venue_id:
        raise StrategyBindingError("StrategyBinding 与 Agent Trading Account 的 Venue 不一致")

    return StrategyExecutionContext(
        strategy_id=requested_strategy_id,
        agent_trading_account_id=account_id,
        unified_account_id=unified_account_id,
        venue_account_id=binding_venue_id,
        mandate_id=str(binding["mandate_id"]).strip(),
        authorization_grant_id=str(binding["authorization_grant_id"]).strip(),
        capital_limit_minor=capital_limit_minor,
        market_types=market_types,
        allowed_assets=allowed_assets,
        allowed_instruments=allowed_instruments,
        max_order_notional_minor=max_order_notional_minor,
        max_daily_notional_minor=max_daily_notional_minor,
        max_leverage=format(strategy_max_leverage, "f"),
        max_slippage=format(strategy_max_slippage, "f"),
    )


def parse_market_catalog(response: dict[str, Any]) -> tuple[MarketInstrument, ...]:
    """Parse a Core catalog page and reject stale or malformed metadata."""

    data = response.get("data")
    meta = response.get("meta")
    if not isinstance(data, list) or not isinstance(meta, dict):
        raise TradingReadinessError("Core Market Catalog 响应不完整")
    if meta.get("stale") is True:
        raise TradingReadinessError("Core Market Catalog 已过期，禁止创建 Draft")
    return tuple(MarketInstrument.from_dict(value) for value in data)


def parse_market_instrument(response: dict[str, Any]) -> MarketInstrument:
    """Parse one exact Core-owned instrument."""

    return MarketInstrument.from_dict(response.get("data"))


def require_instrument_allowed(
    instrument: MarketInstrument,
    readiness: TradingReadinessContext,
    strategy: StrategyExecutionContext | None = None,
) -> None:
    """Apply Catalog ∩ Mandate ∩ Strategy without inventing local symbols."""

    if strategy and strategy.market_types and instrument.market_type not in strategy.market_types:
        raise TradingReadinessError("当前策略不允许该交易类型")
    if strategy and strategy.allowed_instruments and instrument.instrument_id not in set(
        strategy.allowed_instruments
    ):
        raise TradingReadinessError("当前策略不允许该交易标的")
    assets = {instrument.base_asset, instrument.quote_asset}
    if readiness.allowed_symbols:
        mandate_assets = set(readiness.allowed_symbols)
        if "*" not in mandate_assets and not assets.issubset(mandate_assets):
            raise TradingReadinessError("当前 Mandate 不允许该交易资产")
    if strategy and strategy.allowed_assets:
        strategy_assets = set(strategy.allowed_assets)
        if "*" not in strategy_assets and not assets.issubset(strategy_assets):
            raise TradingReadinessError("当前策略不允许该交易资产")


@dataclass(frozen=True)
class TradeIntent:
    """LLM 从自然语言解析出的交易要素（不含账户身份）。"""

    symbol: str
    side: str
    quantity: str
    limit_price: str
    leverage: str | None = None
    max_slippage: str | None = None
    reduce_only: bool = False


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出里稳妥地抠出第一段 JSON 对象。"""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise IntentParseError(f"未从模型输出中找到 JSON: {text[:120]!r}")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise IntentParseError(f"模型输出不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):  # pragma: no cover - 防御：substring 恒为 {…}，解析成功必为 dict
        raise IntentParseError("模型输出的 JSON 不是对象")
    return data


async def parse_trade_intent(llm: LLMProvider, user_text: str) -> TradeIntent:
    """调 LLM 把自然语言解析成 TradeIntent（含必需字段校验）。"""
    result = await llm.complete(_PARSER_SYSTEM, (LLMMessage("user", user_text),), ())
    data = _extract_json(result.text)
    missing = [k for k in ("symbol", "side", "quantity", "limit_price") if not data.get(k)]
    if missing:
        raise IntentParseError(f"交易意图缺少必填要素: {missing}")
    return TradeIntent(
        symbol=str(data["symbol"]),
        side=str(data["side"]).upper(),
        quantity=str(data["quantity"]),
        limit_price=str(data["limit_price"]),
        leverage=None if data.get("leverage") is None else str(data["leverage"]),
        max_slippage=None if data.get("max_slippage") is None else str(data["max_slippage"]),
        reduce_only=bool(data.get("reduce_only", False)),
    )


def build_draft_request(
    intent: TradeIntent,
    *,
    unified_account_id: str,
    venue_account_id: str,
    instrument_id: str | None = None,
    strategy_id: str | None = None,
    mandate_id: str | None = None,
    default_leverage: str = "1",
    default_max_slippage: str = "0.005",
) -> CreateTradeDraftRequest:
    """把 TradeIntent + 会话账户上下文合成契约请求（再过一次 models 边界校验）。"""
    return CreateTradeDraftRequest(
        unified_account_id=unified_account_id,
        venue_account_id=venue_account_id,
        instrument_id=instrument_id,
        symbol=normalize_symbol(intent.symbol),
        side=intent.side,
        quantity=intent.quantity,
        limit_price=intent.limit_price,
        leverage=intent.leverage or default_leverage,
        max_slippage=intent.max_slippage or default_max_slippage,
        reduce_only=intent.reduce_only,
        strategy_id=strategy_id,
        mandate_id=mandate_id,
    )


def render_confirmation_card(draft_response: dict[str, Any]) -> str:
    """把 TradeDraftResponse 渲染成给用户看的中文确认卡片。"""
    data = draft_response.get("data") or {}
    side = data.get("side", "")
    side_label = _SIDE_CN.get(side, side)
    lines = [
        "🧾 交易确认单",
        f"标的：{data.get('symbol', '—')}",
        f"方向：{side_label}（限价单）",
        f"数量：{data.get('quantity', '—')}",
        f"限价：{data.get('limit_price', '—')}",
        f"杠杆：{data.get('leverage', '—')}x",
        f"最大滑点：{data.get('max_slippage', '—')}",
        f"状态：{data.get('status', '—')}",
        f"草稿号：{data.get('id', '—')} (v{data.get('version', '—')})",
        f"有效期至：{data.get('expires_at', '—')}",
        "",
        "⚠️ 请你本人核对后，通过二次验证(step-up)确认发起；智能体不代为确认。",
    ]
    return "\n".join(lines)


async def create_draft_from_text(
    client: AgentOsStagingClient,
    llm: LLMProvider,
    user_text: str,
    *,
    unified_account_id: str,
    venue_account_id: str,
    idempotency_key: str,
    strategy_id: str | None = None,
    mandate_id: str | None = None,
    default_leverage: str = "1",
    default_max_slippage: str = "0.005",
) -> tuple[dict[str, Any], str]:
    """一站式：自然语言 → 意图 → 建草稿 → 确认卡片。返回 (draft_response, card)。"""
    intent = await parse_trade_intent(llm, user_text)
    request = build_draft_request(
        intent,
        unified_account_id=unified_account_id,
        venue_account_id=venue_account_id,
        strategy_id=strategy_id,
        mandate_id=mandate_id,
        default_leverage=default_leverage,
        default_max_slippage=default_max_slippage,
    )
    draft = await client.create_trade_draft(request.to_body(), idempotency_key=idempotency_key)
    return draft, render_confirmation_card(draft)
