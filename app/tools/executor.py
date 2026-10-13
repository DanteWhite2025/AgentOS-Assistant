"""工具执行器 —— 白名单二次校验与 Core/Runtime 工具调用边界。

无论来自工具旁挂端点还是 AgentRunner，工具执行都必须经此：
1. 以 session.scope.tools 强校验（不信任客户端上报的名字）；
2. PERP AgentTradeIntent 只描述用户待确认的交易意图，不执行、不签名；
3. 旧 Core Draft 路径仍保留原有 PolicyEngine/Mandate 预检，等待独立清理。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.news.local_search import local_news_tool_output, search_local_news
from app.policies.engine import PolicyEngine, PolicyViolationError
from app.tools.agentos.client import AgentOsStagingClient
from app.tools.agentos.trade_flow import (
    MarketInstrumentResolutionError,
    TradeIntent,
    TradingReadinessError,
    build_draft_request,
    catalog_asset_query,
    normalize_symbol,
    parse_account_summary,
    parse_market_catalog,
    parse_market_instrument,
    parse_spot_trading_readiness,
    parse_trading_readiness,
    require_draft_ready,
    require_instrument_allowed,
    resolve_strategy_execution_context,
    resolve_unique_market_instrument,
)
from app.tools.core_client import (
    CoreClient,
    market_price_tool_output,
    market_tickers_tool_output,
)
from app.tools.investment_expert_tools import ask_investment_expert
from app.tools.memory_tools import validate_preference_content, validate_preference_key
from app.tools.query_normalization import normalize_search_query
from app.tools.registry import ToolRegistry
from app.tools.tavily_client import TavilySearchClient, TavilySearchConfig

logger = logging.getLogger(__name__)

_SPOT_ALL_ASSET_FILTERS = frozenset(
    {"", "*", "ALL", "SPOT", "现货", "全部", "所有", "资产", "代币"}
)
_SPOT_TICKER_RE = re.compile(r"[A-Z]{2,10}")
_SPOT_PROJECTION_NOTE = (
    "只列出 Core asset-snapshot 实际返回的现货。"
    "App 资产页可能还有原生 BNB、Meme 等未投影币种；投影里没有不等于余额为 0。"
)


def _normalize_asset_filter(raw: object) -> str:
    value = str(raw or "").strip().upper()
    if value in {item.upper() for item in _SPOT_ALL_ASSET_FILTERS}:
        return ""
    return value


def _format_token_balance(raw_value: object, decimals: object) -> str:
    try:
        dec = int(decimals if decimals is not None else 18)
        raw_val = int(str(raw_value or "0"))
        return f"{raw_val / (10 ** dec):.8f}".rstrip("0").rstrip(".")
    except Exception:
        return ""


def _map_spot_holdings(spot: dict[str, Any]) -> list[dict[str, Any]]:
    raw_holdings = spot.get("holdings") or []
    if not isinstance(raw_holdings, list):
        return []
    holdings: list[dict[str, Any]] = []
    for item in raw_holdings:
        if not isinstance(item, dict):
            continue
        holding = dict(item)
        holding["venue"] = holding.get("venue") or "BSC"
        holding["balance"] = _format_token_balance(
            holding.get("total_raw"), holding.get("decimals")
        ) or str(holding.get("usd_cents") or "0")
        holdings.append(holding)
    return holdings


def _is_zero_amount(value: object) -> bool:
    raw = str(value or "").strip()
    if raw in {"", "0", "0.0", "0.00"}:
        return True
    try:
        return Decimal(raw) == 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def _nonzero_holdings(holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    visible: list[dict[str, Any]] = []
    for item in holdings:
        amount = item.get("total_raw")
        if amount in (None, ""):
            amount = item.get("balance")
        if not _is_zero_amount(amount):
            visible.append(item)
    return visible


def _asset_filter_keys(asset_filter: str) -> set[str]:
    value = asset_filter.strip().upper()
    if not value:
        return set()
    keys = {value}
    compact = "".join(ch for ch in value if ch.isalnum())
    if compact:
        keys.add(compact)
    keys.update(_SPOT_TICKER_RE.findall(value))
    normalized = normalize_symbol(value)
    if normalized:
        keys.add(normalized)
    return {key for key in keys if key}


def _filter_holdings_by_asset(
    holdings: list[dict[str, Any]], asset_filter: str
) -> list[dict[str, Any]]:
    if not asset_filter:
        return holdings
    wanted = _asset_filter_keys(asset_filter)
    return [
        item
        for item in holdings
        if str(item.get("asset") or "").strip().upper() in wanted
    ]


def _attention_codes(snap_data: dict[str, Any]) -> dict[str, Any]:
    attention = snap_data.get("attention")
    if isinstance(attention, dict):
        return attention
    return {"required": False, "codes": []}


def _has_attention_code(attention: dict[str, Any], code: str) -> bool:
    codes = attention.get("codes")
    if not isinstance(codes, list):
        return False
    return code in {str(item) for item in codes}


class ToolNotAllowedError(Exception):
    """工具不在会话白名单（对应 403 TOOL_NOT_IN_SCOPE）。"""


class UnknownToolError(Exception):
    """工具未注册或无处理路径。"""


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        core: CoreClient,
        policy_engine: PolicyEngine | None = None,
        *,
        agentos_client: AgentOsStagingClient | None = None,
        memory_store: Any = None,
        expected_agent_id: str = "agent-test-001",
        default_leverage: str = "1",
        default_max_slippage: str = "0.005",
        tavily_api_key: str = "",
        tavily_timeout_seconds: float = 5.0,
        tavily_http_client: httpx.AsyncClient | None = None,
        tavily_cache_redis: Any = None,
        tavily_cache_ttl_seconds: int = 90,
        tavily_max_retries: int = 1,
        news_store: Any = None,
        news_lookback_hours: int = 24,
        investment_expert_client: Any = None,
        minara_client: Any = None,
    ) -> None:
        self._registry = registry
        self._core = core
        self._policy = policy_engine
        self._agentos = agentos_client
        self._memory_store = memory_store
        self._expected_agent_id = expected_agent_id
        self._default_leverage = default_leverage
        self._default_max_slippage = default_max_slippage
        self._tavily: TavilySearchClient | None = (
            TavilySearchClient(
                TavilySearchConfig(
                    api_key=tavily_api_key,
                    timeout_seconds=tavily_timeout_seconds,
                    cache_ttl_seconds=tavily_cache_ttl_seconds,
                    max_retries=tavily_max_retries,
                ),
                http_client=tavily_http_client,
                redis_client=tavily_cache_redis,
            )
            if tavily_api_key
            else None
        )
        self._news_store = news_store
        self._news_lookback_hours = news_lookback_hours
        self._investment_expert_client = investment_expert_client or minara_client
        self._minara_client = self._investment_expert_client
        self._draft_locks: dict[str, asyncio.Lock] = {}
        self._draft_results: dict[str, tuple[str, dict[str, Any]]] = {}

    async def execute(
        self,
        tool_name: str,
        user_id: str,
        args: dict[str, Any],
        scope_tools: list[str],
        *,
        user_bearer_token: str | None = None,
        idempotency_key: str | None = None,
        intent_source: str = "TEXT",
    ) -> dict[str, Any]:
        # 红线：服务端二次强校验，不信任客户端上报的 tool_name
        if not self._registry.is_allowed(tool_name, scope_tools):
            raise ToolNotAllowedError(tool_name)
        tool = self._registry.get(tool_name)
        if tool is None:
            raise UnknownToolError(tool_name)
        # Fake/test mode preserves the historical isolation rule. In real mode
        # the same executor has the explicit formal Core client injected.
        if (
            tool.external
            and self._agentos is None
            and tool_name not in {"web_search", "ask_investment_expert"}
        ):
            raise UnknownToolError(f"{tool_name} 为 external 工具，须经 AgentOsStagingClient 执行")
        if tool_name == "ask_investment_expert":
            if self._investment_expert_client is None:
                return {"error": "投研证据抓取未配置"}
            message = str(args.get("message") or "")
            raw_symbols = args.get("symbols") or []
            if (
                not isinstance(raw_symbols, list)
                or len(raw_symbols) > 8
                or any(not isinstance(s, str) for s in raw_symbols)
            ):
                return {"error": "symbols 必须是最多 8 个字符串"}
            # 历史 mode 参数（fast/expert）已随证据化改造下线：深度分析工作转移
            # 给主脑，本工具只剩取证一个职责。模型出于历史习惯仍可能带上 mode，
            # 静默忽略而不是报错——为它返回 error 会白烧一整轮工具调用。
            return await ask_investment_expert(
                message, client=self._investment_expert_client, symbols=tuple(raw_symbols)
            )
        if tool_name == "web_search":
            raw_query = str(args.get("query") or "").strip()
            if not raw_query:
                raise ValueError("web_search 必须提供搜索关键词")
            if self._tavily is None:
                raise ValueError("联网搜索未配置（AGENT_TAVILY_API_KEY）")
            query = normalize_search_query(raw_query)
            topic = str(args.get("topic") or "general")
            time_range = args.get("time_range")
            if time_range:
                time_range = str(time_range)
            if self._news_store is not None:
                try:
                    local_items = await search_local_news(
                        self._news_store, query, lookback_hours=self._news_lookback_hours
                    )
                except Exception as exc:
                    # 本地库只是 Tavily 的前置降本方案，绝不能因为它挂了
                    # （数据库超时/连不上）就让整个联网搜索跟着失败。
                    logger.warning("本地新闻库检索异常，降级到 Tavily: %s", exc)
                    local_items = []
                if local_items:
                    return local_news_tool_output(local_items, query)
            return await self._tavily.search(query, topic=topic, time_range=time_range)
        if tool_name == "update_user_preference":
            category = args.get("category")
            key = args.get("key")
            content = args.get("content")
            if not category or not key:
                raise ValueError("update_user_preference requires category, key, and content")
            validated_key = validate_preference_key(key)
            validated_content = validate_preference_content(str(category), content)
            if self._memory_store is None:
                return {
                    "status": "ok",
                    "category": str(category),
                    "key": validated_key,
                    "content": validated_content,
                }
            item = await self._memory_store.remember_profile(
                user_id=user_id,
                category=str(category),
                key=validated_key,
                content=validated_content,
            )
            return {
                "status": "ok",
                "category": item.category,
                "key": item.key,
                "content": item.content,
                "updated_at": item.updated_at.isoformat(),
            }
        if tool_name == "market_tickers":
            if set(args) != {"symbols"} or not isinstance(args.get("symbols"), list):
                raise ValueError("market_tickers requires exactly one array field: symbols")
            # Read-only Mainnet market data is intentionally independent from
            # the Testnet execution catalog. A transient Testnet meta refresh
            # must not make a plain price query unavailable. Pair-like model
            # output is reduced to a base-asset search symbol; Core's market
            # data endpoint remains the authority for supported ticker names.
            symbols = self._normalize_market_data_symbols(args["symbols"])
            ticker_response = await self._core.get_market_tickers(symbols)
            return market_tickers_tool_output(ticker_response)
        if tool_name == "get_market_price":
            if set(args) != {"symbol"} or not isinstance(args.get("symbol"), str):
                raise ValueError("get_market_price requires exactly one string field: symbol")
            symbol = self._normalize_market_data_symbols([args["symbol"]])[0]
            ticker_response = await self._core.get_market_tickers([symbol])
            return market_price_tool_output(ticker_response)
        if self._agentos is not None and tool_name == "search_market_catalog":
            allowed = {"market_type", "query", "cursor", "limit"}
            if set(args) - allowed:
                raise ValueError("Market Catalog 查询包含未知字段")
            # LLM 常把 Catalog 的 display_symbol（ETH-PERP）或自编的 ETHPERP 当
            # symbol 发过来，而 venue_symbol 是裸币名 —— 原样透传会查不到标的
            # （线上实测 query=ETHPERP 命中 0 条）。空 query 表示「列全部」，保留。
            raw_query = str(args.get("query") or "").strip()
            catalog_response = await self._agentos.get_market_catalog(
                market_type=(
                    str(args["market_type"]) if args.get("market_type") is not None else None
                ),
                query=catalog_asset_query(raw_query) if raw_query else "",
                cursor=str(args.get("cursor") or "") or None,
                limit=int(args.get("limit") or 20),
            )
            instruments = parse_market_catalog(catalog_response)
            return {
                "source": "CORE_MARKET_CATALOG",
                "data": [
                    {
                        "instrument_id": item.instrument_id,
                        "market_type": item.market_type,
                        "venue": item.venue,
                        "environment": item.environment,
                        "display_symbol": item.display_symbol,
                        "venue_symbol": item.venue_symbol,
                        "base_asset": item.base_asset,
                        "quote_asset": item.quote_asset,
                        "trade_supported": item.trade_supported,
                        "stale": item.stale,
                        "supported_order_types": list(item.supported_order_types),
                        "maximum_leverage": item.maximum_leverage,
                        "as_of": item.as_of,
                    }
                    for item in instruments
                ],
                "meta": catalog_response.get("meta"),
                "request_id": str(catalog_response.get("request_id") or "")[:128] or None,
            }
        if self._agentos is not None and tool_name in {
            "get_trading_readiness",
            "get_account_summary",
            "get_balances",
            "get_positions",
            "get_orders",
            "get_risk_summary",
            "agentos_get_balances",
            "agentos_get_positions",
            "agentos_get_orders",
        }:
            if user_bearer_token is None:
                raise TradingReadinessError(
                    "此只读账户查询需要当前 Mobile USER 凭据；请通过 /messages 发起"
                )
            if tool_name == "get_trading_readiness":
                readiness_res = await self._agentos.get_user_trading_readiness(user_bearer_token)
                readiness = parse_trading_readiness(
                    readiness_res,
                    expected_agent_id=self._expected_agent_id,
                )
                return readiness.tool_output()

            # 优先尝试 Core 最新的标准只读资产投影 GET /api/v1/me/asset-snapshot
            if tool_name in {
                "get_account_summary",
                "get_balances",
                "get_positions",
                "get_risk_summary",
                "agentos_get_balances",
                "agentos_get_positions",
            }:
                snapshot_response: dict[str, Any] | None = None
                try:
                    snapshot_response = await self._agentos.get_user_asset_snapshot(user_bearer_token)
                except Exception as exc:
                    logger.debug("Core /api/v1/me/asset-snapshot 暂未部署或请求异常，回退现有路径: %s", exc)
                    snapshot_response = None

                if (
                    snapshot_response
                    and isinstance(snapshot_response.get("data"), dict)
                    and (
                        snapshot_response["data"].get("source") == "LIVE_CHAIN_AND_VENUE"
                        or "contract" in snapshot_response["data"]
                        or "spot" in snapshot_response["data"]
                    )
                ):
                    snap_data = snapshot_response["data"]
                    contract = (
                        snap_data.get("contract")
                        if isinstance(snap_data.get("contract"), dict)
                        else {}
                    )
                    spot = snap_data.get("spot") if isinstance(snap_data.get("spot"), dict) else {}
                    is_contract_avail = bool(contract.get("available"))
                    is_spot_avail = bool(spot.get("available"))
                    raw_positions = snap_data.get("positions") or []
                    positions_list = list(raw_positions) if isinstance(raw_positions, list) else []

                    if tool_name == "get_risk_summary":
                        status = "ACTIVE" if (is_contract_avail or is_spot_avail) else "UNAVAILABLE"
                        withdrawable_usd = str(contract.get("withdrawable") or "0")
                        withdrawable_cents = str(contract.get("withdrawable_usd_cents") or "0")
                        account_val_usd = str(contract.get("account_value") or "0")
                        account_val_cents = str(contract.get("usd_cents") or "0")
                        risk_out: dict[str, Any] = {
                            "source": "LIVE_ASSET_SNAPSHOT",
                            "status": status,
                            "available_trade_notional": withdrawable_usd,
                            "available_trade_notional_minor": withdrawable_cents,
                            "venue_margin_minor": withdrawable_cents,
                            "withdrawable_usd": withdrawable_usd,
                            "withdrawable_usd_cents": withdrawable_cents,
                            "contract_account_value": account_val_usd,
                            "contract_usd_cents": account_val_cents,
                            "contract_available": is_contract_avail,
                            "spot_available": is_spot_avail,
                            "spot_usd_cents": spot.get("usd_cents"),
                            "total_usd_cents": snap_data.get("total_usd_cents"),
                            "max_leverage": "50",
                            "attention": snap_data.get("attention") or {"required": False, "codes": []},
                            "stale": snap_data.get("stale", False),
                            "request_id": snapshot_response.get("request_id"),
                        }
                        if not (is_contract_avail or is_spot_avail):
                            risk_out["message"] = "当前用户尚未绑定链上钱包或开通交易场所，可用交易额度不可用"
                        elif not is_contract_avail:
                            risk_out["message"] = "合约场所当前不可用，未能获取合约可用额度"
                        return risk_out

                    attention = _attention_codes(snap_data)
                    if tool_name in {"get_positions", "agentos_get_positions"}:
                        filtered_positions = positions_list
                        symbol_filter = str(args.get("symbol") or "").strip().upper()
                        if symbol_filter:
                            clean_sym = normalize_symbol(symbol_filter)
                            filtered_positions = [
                                p
                                for p in positions_list
                                if str(p.get("symbol") or "").upper() in {symbol_filter, clean_sym}
                            ]
                        pos_out: dict[str, Any] = {
                            "source": "LIVE_VENUE_POSITIONS",
                            "data": filtered_positions,
                            "contract_available": is_contract_avail,
                            "trading_address": snap_data.get("trading_address"),
                            "attention": attention,
                            "request_id": snapshot_response.get("request_id"),
                        }
                        if not is_contract_avail or _has_attention_code(
                            attention, "VENUE_POSITIONS_UNAVAILABLE"
                        ):
                            pos_out["message"] = (
                                "合约仓位暂不可用，不能把空列表当成没有持仓。"
                                "BNB/USDT/Meme 等现货请改用 get_balances。"
                            )
                        elif not filtered_positions:
                            target = symbol_filter or "永续"
                            pos_out["message"] = (
                                f"当前没有 {target} 永续合约仓位。"
                                "现货资产（BNB、USDT、Meme）请用 get_balances，不要把空仓位当成查询失败。"
                            )
                        return pos_out

                    if tool_name in {"get_balances", "agentos_get_balances"}:
                        holdings = _map_spot_holdings(spot)
                        if is_contract_avail and not _is_zero_amount(
                            contract.get("withdrawable")
                        ):
                            holdings.append({
                                "asset": "USDC",
                                "balance": str(contract.get("withdrawable") or "0"),
                                "usd_cents": str(contract.get("withdrawable_usd_cents") or "0"),
                                "venue": "HYPERLIQUID",
                                "account_value": str(contract.get("account_value") or "0"),
                            })
                        asset_filter = _normalize_asset_filter(args.get("asset"))
                        projected = _filter_holdings_by_asset(holdings, asset_filter)
                        # 未指定币种时隐藏 0 余额；点名查询时保留 0，避免把「已投影但为 0」
                        # 说成「投影里没有」。
                        if asset_filter:
                            filtered = _nonzero_holdings(projected) or projected
                        else:
                            filtered = _nonzero_holdings(holdings)
                        bals_out: dict[str, Any] = {
                            "source": "LIVE_ASSET_BALANCES",
                            "data": filtered,
                            "spot": spot,
                            "contract": contract,
                            "spot_available": is_spot_avail,
                            "contract_available": is_contract_avail,
                            "total_usd_cents": snap_data.get("total_usd_cents"),
                            # 余额只来自 Core 绑定的这两个地址。带上它们，App 资产页
                            # 与 Agent 报数不一致时才能判断是不是钱包对不上。
                            "trading_address": snap_data.get("trading_address"),
                            "collection_address": snap_data.get("collection_address"),
                            "attention": attention,
                            "spot_projection_note": _SPOT_PROJECTION_NOTE,
                            "request_id": snapshot_response.get("request_id"),
                        }
                        if not is_spot_avail or _has_attention_code(
                            attention, "SPOT_UNAVAILABLE"
                        ):
                            bals_out["message"] = (
                                "现货链上余额暂不可用，不能把空列表当成余额为 0"
                            )
                        elif asset_filter and not projected:
                            bals_out["requested_asset"] = asset_filter
                            bals_out["matched"] = False
                            bals_out["message"] = (
                                f"现货投影里没有 {asset_filter}。"
                                "Core 当前主要投影 USDT/USDC；BNB 与 Meme 可能只在 App 资产页可见，"
                                "不能把缺失编成余额为 0。"
                            )
                        elif asset_filter and not _nonzero_holdings(projected):
                            bals_out["requested_asset"] = asset_filter
                            bals_out["matched"] = True
                            bals_out["message"] = (
                                f"现货 {asset_filter} 已投影，当前余额为 0。"
                                "这不是查询失败，也不代表 App 资产页没有这个币。"
                            )
                        return bals_out

                    if tool_name == "get_account_summary":
                        return {
                            "source": "LIVE_ACCOUNT_SUMMARY",
                            "account_summary": {
                                "spot": spot,
                                "contract": contract,
                                "total_usd_cents": snap_data.get("total_usd_cents"),
                                "positions": positions_list,
                                "trading_address": snap_data.get("trading_address"),
                                "collection_address": snap_data.get("collection_address"),
                                "attention": attention,
                                "stale": snap_data.get("stale", False),
                                "as_of": snap_data.get("as_of"),
                            },
                            "spot_projection_note": _SPOT_PROJECTION_NOTE,
                            "request_id": snapshot_response.get("request_id"),
                        }

            # ---------------- Fallback 兼容分支（当 Core 尚未部署 asset-snapshot 或查询 orders 时）----------------
            readiness_response: dict[str, Any] | None = None
            try:
                readiness_response = await self._agentos.get_user_trading_readiness(user_bearer_token)
            except Exception as exc:
                logger.warning("只读查询获取 trading-readiness 失败，降级处理 tool=%s err=%s", tool_name, exc)

            readiness_data = readiness_response.get("data") if isinstance(readiness_response, dict) else None
            unified_account_id = ""
            if isinstance(readiness_data, dict):
                unified_account_id = str(readiness_data.get("unified_account_id") or "").strip()

            if tool_name == "get_risk_summary":
                if readiness_response and isinstance(readiness_data, dict):
                    try:
                        parsed_readiness = parse_trading_readiness(
                            readiness_response,
                            expected_agent_id=self._expected_agent_id,
                        )
                        r_out = parsed_readiness.tool_output()
                        return {
                            "source": "CORE_RISK_SUMMARY",
                            "status": "ACTIVE",
                            "venue_margin_minor": r_out["venue_margin_minor"],
                            "daily_notional_limit_minor": r_out["daily_notional_limit_minor"],
                            "daily_notional_used_minor": r_out["daily_notional_used_minor"],
                            "daily_notional_remaining_minor": r_out["daily_notional_remaining_minor"],
                            "available_trade_notional_minor": r_out["available_trade_notional_minor"],
                            "capital_status": str(readiness_data.get("capital_status") or "AVAILABLE"),
                            "max_leverage": r_out["mandate"]["max_leverage"],
                            "per_order_limit_minor": r_out["per_order_limit_minor"],
                            "request_id": readiness_response.get("request_id"),
                        }
                    except Exception as exc:
                        logger.warning("解析 readiness 计算风控额度失败，使用原始数据兜底: %s", exc)
                        raw_mandate = readiness_data.get("mandate")
                        mandate: dict[str, Any] = raw_mandate if isinstance(raw_mandate, dict) else {}
                        return {
                            "source": "CORE_RISK_SUMMARY",
                            "status": "ACTIVE",
                            "venue_margin_minor": str(readiness_data.get("venue_margin_minor") or "0"),
                            "daily_notional_limit_minor": str(readiness_data.get("daily_notional_limit_minor") or "0"),
                            "daily_notional_used_minor": str(readiness_data.get("daily_notional_used_minor") or "0"),
                            "daily_notional_remaining_minor": str(readiness_data.get("daily_notional_remaining_minor") or "0"),
                            "available_trade_notional_minor": str(readiness_data.get("available_trade_notional_minor") or "0"),
                            "capital_status": str(readiness_data.get("capital_status") or "AVAILABLE"),
                            "max_leverage": str(mandate.get("max_leverage") or readiness_data.get("max_leverage") or "1"),
                            "per_order_limit_minor": str(
                                mandate.get("max_notional_per_order_minor")
                                or readiness_data.get("per_order_limit_minor")
                                or "0"
                            ),
                            "request_id": readiness_response.get("request_id"),
                        }
                return {
                    "source": "CORE_RISK_SUMMARY",
                    "status": "UNAVAILABLE",
                    "available_trade_notional_minor": "0",
                    "message": "当前账户未配置风控限额或托管授权",
                }

            if not unified_account_id:
                if tool_name in {"get_balances", "agentos_get_balances"}:
                    return {"source": "CORE_BALANCES", "data": [], "message": "账户尚未初始化，无资产余额"}
                if tool_name in {"get_positions", "agentos_get_positions"}:
                    return {"source": "CORE_POSITIONS", "data": [], "message": "账户尚未初始化，无当前持仓"}
                if tool_name in {"get_orders", "agentos_get_orders"}:
                    return {"source": "CORE_ORDERS", "data": [], "message": "账户尚未初始化，无委托订单"}
                return {
                    "source": "CORE_ACCOUNT_SUMMARY",
                    "account_summary": {"balance": {}, "positions": [], "orders": []},
                    "message": "账户尚未初始化",
                }

            if tool_name in {"get_positions", "agentos_get_positions"}:
                try:
                    summary_res = await self._agentos.get_user_account_summary(
                        unified_account_id, user_bearer_token
                    )
                    parsed = parse_account_summary(summary_res)
                    user_positions = parsed.get("positions") or []
                    symbol_filter = str(args.get("symbol") or "").strip().upper()
                    if symbol_filter:
                        clean_sym = normalize_symbol(symbol_filter)
                        user_positions = [
                            p
                            for p in user_positions
                            if str(p.get("symbol") or "").upper() in {symbol_filter, clean_sym}
                        ]
                    return {"source": "CORE_USER_POSITIONS", "data": user_positions}
                except Exception as exc:
                    logger.warning("从 account-summary 读取用户持仓失败，回退 get_positions: %s", exc)
                    return await self._agentos.get_positions()

            if tool_name in {"get_orders", "agentos_get_orders"}:
                try:
                    summary_res = await self._agentos.get_user_account_summary(
                        unified_account_id, user_bearer_token
                    )
                    parsed = parse_account_summary(summary_res)
                    user_orders = parsed.get("orders") or []
                    symbol_filter = str(args.get("symbol") or "").strip().upper()
                    if symbol_filter:
                        clean_sym = normalize_symbol(symbol_filter)
                        user_orders = [
                            o
                            for o in user_orders
                            if str(o.get("symbol") or "").upper() in {symbol_filter, clean_sym}
                        ]
                    return {"source": "CORE_USER_ORDERS", "data": user_orders}
                except Exception as exc:
                    logger.warning("从 account-summary 读取用户订单失败，回退 get_orders: %s", exc)
                    return await self._agentos.get_orders()

            if tool_name in {"get_balances", "agentos_get_balances"}:
                balances_res = await self._agentos.get_user_balances(unified_account_id, user_bearer_token)
                asset_filter = str(args.get("asset") or "").strip().upper()
                if asset_filter and isinstance(balances_res.get("data"), list):
                    filtered = [
                        item
                        for item in balances_res["data"]
                        if str(item.get("asset") or "").upper() == asset_filter
                    ]
                    return {**balances_res, "data": filtered}
                return balances_res

            summary_response = await self._agentos.get_user_account_summary(
                unified_account_id, user_bearer_token
            )
            summary = parse_account_summary(summary_response)
            return {
                "source": "CORE_ACCOUNT_SUMMARY",
                "account_summary": summary,
                "request_id": summary.get("request_id"),
            }
        if tool.write:
            if tool_name in {
                "create_trade_draft",
                "agentos_create_trade_draft",
                "create_perp_trade_draft",
            }:
                if self._policy is not None and self._agentos is None:
                    decision = self._policy.check_trade_draft(args, datetime.now(UTC))
                    if not decision.allowed:
                        raise PolicyViolationError(decision.reason)
                if self._agentos is not None:
                    if user_bearer_token is None:
                        raise TradingReadinessError("创建交易 Intent 需要当前 Mobile USER 凭据")
                    return await self._create_perp_intent(
                        args,
                        user_bearer_token,
                        user_id=user_id,
                        idempotency_key=idempotency_key,
                        intent_source=intent_source,
                    )
                return await self._core.create_trade_draft(user_id, args)
            if tool_name == "create_spot_swap_draft":
                if self._agentos is None:
                    raise UnknownToolError("Spot Draft 需要 AgentOsStagingClient")
                if user_bearer_token is None:
                    raise TradingReadinessError("创建 Spot Draft 需要当前 Mobile USER 凭据")
                return await self._create_spot_draft(
                    args,
                    user_bearer_token,
                    user_id=user_id,
                    idempotency_key=idempotency_key,
                )
            raise UnknownToolError(tool_name)
        return await self._core.get_resource(tool.resource or tool_name, user_id, args)

    async def _create_perp_intent(
        self,
        args: dict[str, Any],
        user_bearer_token: str,
        *,
        user_id: str,
        idempotency_key: str | None,
        intent_source: str,
    ) -> dict[str, Any]:
        assert self._agentos is not None
        allowed_args = {
            "instrument_id",
            "symbol",
            "side",
            "order_type",
            "qty",
            "notional_usdc",
            "price",
            "leverage",
            "max_slippage",
            "reduce_only",
        }
        if set(args) - allowed_args:
            raise ValueError("PERP Draft 请求包含未知字段")
        if str(args.get("order_type") or "").lower() != "limit":
            raise ValueError("Core Draft 当前只允许 LIMIT")
        side = str(args.get("side") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side 必须是 buy 或 sell")
        price = str(args.get("price") or "").strip()
        if not price:
            raise ValueError("LIMIT Draft 必须提供 price")

        instrument = await self._resolve_instrument(
            market_type="PERP",
            instrument_id="",
            query=str(args.get("symbol") or ""),
        )

        # AgentTradeIntent 只是交给 Mobile 用户确认的结构化提议，不是 Core
        # 执行请求。旧的 Trading Account / Mandate / Strategy Binding 属于平台
        # 托管执行模型，不能再作为创建用户钱包直签意图的前置门禁。
        if "LIMIT" not in instrument.supported_order_types:
            raise TradingReadinessError("Core Catalog 不允许该标的创建 LIMIT 交易意图")
        leverage = str(args.get("leverage") or self._default_leverage).strip()
        try:
            leverage_value = Decimal(leverage)
            maximum_leverage = Decimal(instrument.maximum_leverage)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise TradingReadinessError("Core Catalog 杠杆限制或请求杠杆无效") from exc
        if leverage_value <= 0 or maximum_leverage <= 0 or leverage_value > maximum_leverage:
            raise TradingReadinessError("请求杠杆超过 Core Catalog 标的限制")

        body: dict[str, Any] = {
            "agent_id": self._expected_agent_id,
            "source": intent_source,
            "venue": "HYPERLIQUID",
            "symbol": instrument.venue_symbol,
            "side": side,
            "order_type": "LIMIT",
            "limit_price": price,
            "leverage": leverage,
            "reduce_only": args.get("reduce_only") is True,
        }

        quantity = str(args.get("qty") or "").strip()
        if quantity:
            body["quantity"] = quantity
        else:
            notional = str(args.get("notional_usdc") or "").strip()
            if notional:
                body["notional"] = notional
            else:
                raise ValueError("数量或金额不能为空")

        stable_key = self._stable_draft_key("perp_intent", user_id, idempotency_key)
        try:
            response = await self._once(
                stable_key,
                self._payload_fingerprint(body),
                lambda: self._agentos.create_agent_trade_intent(
                    body,
                    idempotency_key=stable_key,
                    user_bearer_token=user_bearer_token,
                ),
            )
        except httpx.HTTPStatusError as exc:
            try:
                payload = exc.response.json()
                code = payload.get("error", {}).get("code", "")
            except Exception:
                raise exc
            if "LIMIT" in code or code == "POLICY_VIOLATION":
                raise TradingReadinessError(f"请求被拒绝：{code}")
            raise

        data = response.get("data")
        if not isinstance(data, dict):
            raise ValueError("Agent Trade Intent 响应无效")

        intent_id = data.get("id")
        if not intent_id or not str(intent_id).strip():
            raise ValueError("Agent Trade Intent 响应缺少 id")

        status = data.get("status")
        if not status or not str(status).strip():
            raise ValueError("Agent Trade Intent 响应缺少 status")

        intent_id = str(intent_id).strip()
        return {
            "status": str(status).strip(),
            "trade_intent_id": intent_id,
            "trade_draft_id": intent_id,
            "core_draft_id": intent_id,
            "draft_version": None,
            "market_type": "PERP",
            "instrument_id": instrument.instrument_id,
            "agent_id": self._expected_agent_id,
            "confirmation_mode": "ALWAYS_CONFIRM",
            "execution_requested": False,
            "request_id": str(response.get("request_id") or "")[:128] or None,
        }

    async def _create_formal_draft(
        self,
        args: dict[str, Any],
        user_bearer_token: str,
        *,
        user_id: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        assert self._agentos is not None
        allowed_args = {
            "instrument_id",
            "symbol",
            "side",
            "order_type",
            "qty",
            "notional_usdc",
            "price",
            "leverage",
            "max_slippage",
            "reduce_only",
        }
        if set(args) - allowed_args:
            raise ValueError("PERP Draft 请求包含未知字段；标的身份和场所只能来自 Core")
        if str(args.get("order_type") or "").lower() != "limit":
            raise ValueError("Core Draft 当前只允许 LIMIT")
        side = str(args.get("side") or "").strip().upper()
        if side not in {"BUY", "SELL"}:
            raise ValueError("side 必须是 buy 或 sell")
        price = str(args.get("price") or "").strip()
        if not price:
            raise ValueError("LIMIT Draft 必须提供 price")
        quantity = str(args.get("qty") or "").strip()
        if not quantity:
            notional = args.get("notional_usdc")
            try:
                quantity_decimal = Decimal(str(notional)) / Decimal(price)
            except (InvalidOperation, ZeroDivisionError, TypeError, ValueError) as exc:
                raise ValueError("必须提供 qty，或提供有效的 notional_usdc 与 price") from exc
            quantity = format(quantity_decimal.quantize(Decimal("0.00000001")), "f")
        leverage = str(args.get("leverage") or self._default_leverage)
        max_slippage = str(args.get("max_slippage") or self._default_max_slippage)

        readiness_response = await self._agentos.get_user_trading_readiness(user_bearer_token)
        readiness = parse_trading_readiness(
            readiness_response,
            expected_agent_id=self._expected_agent_id,
        )
        require_draft_ready(readiness)
        strategy_id = readiness.strategy_id
        execution = await resolve_strategy_execution_context(
            self._agentos,
            strategy_id,
            expected_agent_id=self._expected_agent_id,
        )
        if (
            execution.unified_account_id != readiness.unified_account_id
            or execution.venue_account_id != readiness.venue_account_id
            or execution.agent_trading_account_id != readiness.agent_trading_account_id
            or execution.mandate_id != readiness.mandate_id
        ):
            raise TradingReadinessError(
                "Core USER Readiness 与 AGENT Strategy Binding 账户上下文不一致"
            )

        instrument = await self._resolve_instrument(
            market_type="PERP",
            instrument_id="",
            query=str(args.get("symbol") or ""),
        )
        require_instrument_allowed(instrument, readiness, execution)
        await self._require_market_snapshot(instrument.venue_symbol)
        if "LIMIT" not in instrument.supported_order_types:
            raise TradingReadinessError("Core Catalog 不允许该标的创建 LIMIT Draft")
        try:
            leverage_value = Decimal(leverage)
            slippage_value = Decimal(max_slippage)
            maximum_leverage = min(
                Decimal(instrument.maximum_leverage),
                Decimal(readiness.max_leverage),
                Decimal(execution.max_leverage),
            )
            maximum_slippage = min(Decimal(readiness.max_slippage), Decimal(execution.max_slippage))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise TradingReadinessError("Core 交易限制或请求 Decimal 无效") from exc
        if leverage_value <= 0 or leverage_value > maximum_leverage:
            raise TradingReadinessError("请求杠杆超过 Core Mandate 或策略限制")
        if slippage_value <= 0 or slippage_value > maximum_slippage:
            raise TradingReadinessError("请求滑点超过 Core Mandate 或策略限制")
        try:
            notional_minor = int(
                (Decimal(quantity) * Decimal(price) * Decimal(1_000_000)).to_integral_value()
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("数量或限价无效") from exc
        max_order = min(
            readiness.per_order_limit_minor,
            readiness.available_trade_notional_minor,
            execution.capital_limit_minor,
            execution.max_order_notional_minor,
            execution.max_daily_notional_minor,
        )
        if notional_minor <= 0 or notional_minor > max_order:
            raise TradingReadinessError("请求金额超过 Core Mandate 或策略剩余额度")
        await self._read_current_trading_state(readiness.unified_account_id)
        if args.get("reduce_only") is True:
            positions = await self._agentos.get_positions()
            if not self._has_open_position(positions, instrument.venue_symbol):
                raise TradingReadinessError("Core 没有可关闭的对应仓位")

        request = build_draft_request(
            TradeIntent(
                symbol=instrument.venue_symbol,
                side=side,
                quantity=quantity,
                limit_price=price,
                leverage=leverage,
                max_slippage=max_slippage,
                reduce_only=args.get("reduce_only") is True,
            ),
            unified_account_id=execution.unified_account_id,
            venue_account_id=execution.venue_account_id,
            instrument_id=instrument.instrument_id,
            strategy_id=execution.strategy_id,
            mandate_id=execution.mandate_id,
            default_leverage=self._default_leverage,
            default_max_slippage=max_slippage,
        )
        stable_key = self._stable_draft_key("perp", user_id, idempotency_key)
        response = await self._once(
            stable_key,
            self._payload_fingerprint(request.to_body()),
            lambda: self._agentos.create_trade_draft(request.to_body(), idempotency_key=stable_key),
        )
        data = response.get("data")
        if not isinstance(data, dict) or not str(data.get("id") or "").strip():
            raise TradingReadinessError("Core Draft 响应缺少 draft id")
        core_draft_id = str(data["id"])
        return {
            "status": str(data.get("status") or "DRAFT"),
            "trade_draft_id": core_draft_id,
            "core_draft_id": core_draft_id,
            "draft_version": data.get("version"),
            "market_type": "PERP",
            "instrument_id": instrument.instrument_id,
            "strategy_id": execution.strategy_id,
            "agent_id": self._expected_agent_id,
            "confirmation_mode": readiness.confirmation_mode,
            "execution_requested": False,
            "request_id": str(response.get("request_id") or "")[:128] or None,
        }

    async def _create_spot_draft(
        self,
        args: dict[str, Any],
        user_bearer_token: str,
        *,
        user_id: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        assert self._agentos is not None
        allowed_args = {
            "instrument_id",
            "symbol",
            "sell_asset",
            "buy_asset",
            "sell_symbol",
            "buy_symbol",
            "sell_token",
            "buy_token",
            "amount",
            "max_slippage",
        }
        if set(args) - allowed_args:
            raise ValueError("SPOT Draft 请求包含未知字段；Token、Router 和场所只能来自 Core")
        readiness = parse_spot_trading_readiness(
            await self._agentos.get_user_trading_readiness(user_bearer_token)
        )

        sell_asset = str(
            args.get("sell_asset")
            or args.get("sell_symbol")
            or args.get("sell_token")
            or args.get("symbol")
            or ""
        ).strip().upper()
        buy_asset = str(
            args.get("buy_asset")
            or args.get("buy_symbol")
            or args.get("buy_token")
            or ""
        ).strip().upper()

        if not sell_asset or not buy_asset:
            raise ValueError("SPOT Draft 必须同时指定卖出资产和买入资产")
        if self._canonical_spot_asset(sell_asset) == self._canonical_spot_asset(buy_asset):
            raise ValueError("SPOT Draft 的卖出资产和买入资产不能相同")
        instrument = await self._resolve_directional_spot_instrument(
            sell_asset=sell_asset,
            buy_asset=buy_asset,
        )

        if "EXACT_INPUT" not in instrument.supported_order_types:
            raise TradingReadinessError("Core Catalog 不允许该标的 exact-input Swap")

        sell_token = instrument.token_in_address or ""
        buy_token = instrument.token_out_address or ""
        decimals = instrument.token_in_ledger_decimals

        if not sell_token or not buy_token or decimals is None:
            raise TradingReadinessError("Core Catalog SPOT 标的 Token 地址或账本精度不完整")

        amount = self._positive_decimal(args.get("amount"), "amount")
        scaled = amount * (Decimal(10) ** decimals)
        if scaled != scaled.to_integral_value():
            raise ValueError("输入金额超过 Core Catalog 声明的资产精度")
        sell_amount_minor = str(int(scaled))
        slippage = self._positive_decimal(
            args.get("max_slippage") or self._default_max_slippage,
            "max_slippage",
        )
        bps = slippage * Decimal(10000)
        if bps != bps.to_integral_value():
            raise ValueError("max_slippage 必须能精确换算成 basis points")
        from app.tools.agentos.models import CreateSpotSwapDraftRequest

        request = CreateSpotSwapDraftRequest(
            instrument_id=instrument.instrument_id,
            unified_account_id=readiness.unified_account_id,
            chain_id=instrument.chain_id or 97,
            sell_token=sell_token,
            buy_token=buy_token,
            sell_amount_minor=sell_amount_minor,
            max_slippage_bps=int(bps),
        )
        stable_key = self._stable_draft_key("spot", user_id, idempotency_key)
        response = await self._once(
            stable_key,
            self._payload_fingerprint(request.to_body()),
            lambda: self._agentos.create_spot_swap_draft(
                request.to_body(),
                idempotency_key=stable_key,
                user_bearer_token=user_bearer_token,
            ),
        )
        data = response.get("data")
        if not isinstance(data, dict) or not str(data.get("id") or "").strip():
            raise TradingReadinessError("Core Spot Draft 响应缺少 draft id")
        return {
            "status": str(data.get("status") or "PENDING_CONFIRMATION"),
            "trade_draft_id": str(data["id"]),
            "core_draft_id": str(data["id"]),
            "draft_version": data.get("version"),
            "market_type": "SPOT",
            "instrument_id": instrument.instrument_id,
            "strategy_id": None,
            "agent_id": self._expected_agent_id,
            "confirmation_mode": "ALWAYS_CONFIRM",
            "execution_requested": False,
            "request_id": str(response.get("request_id") or "")[:128] or None,
        }

    async def _resolve_directional_spot_instrument(
        self,
        *,
        sell_asset: str,
        buy_asset: str,
    ):
        """Resolve the exact Core-owned tokenIn -> tokenOut instrument.

        Spot catalog instruments are directional.  Runtime must never reuse an
        instrument by swapping its token addresses because Core validates the
        instrument identity against tokenIn and tokenOut.
        """

        assert self._agentos is not None
        catalog_response = await self._agentos.get_market_catalog(
            market_type="SPOT",
            query=f"{sell_asset}/{buy_asset}",
            limit=20,
            trade_supported=True,
        )
        instruments = parse_market_catalog(catalog_response)
        expected_sell = self._canonical_spot_asset(sell_asset)
        expected_buy = self._canonical_spot_asset(buy_asset)
        matches = [
            instrument
            for instrument in instruments
            if instrument.market_type == "SPOT"
            and self._canonical_spot_asset(instrument.base_asset) == expected_sell
            and self._canonical_spot_asset(instrument.quote_asset) == expected_buy
        ]
        if not matches:
            raise MarketInstrumentResolutionError(
                "MARKET_INSTRUMENT_NOT_FOUND",
                f"Core Market Catalog 未找到支持 {sell_asset} -> {buy_asset} 的 SPOT 交易对",
                400,
            )
        if len(matches) != 1:
            raise MarketInstrumentResolutionError(
                "MARKET_INSTRUMENT_AMBIGUOUS",
                f"Core Market Catalog 返回了多个 {sell_asset} -> {buy_asset} SPOT 标的",
                400,
            )
        return matches[0]

    @staticmethod
    def _canonical_spot_asset(value: str) -> str:
        normalized = value.strip().upper()
        return "WBNB" if normalized in {"BNB", "TBNB", "WBNB"} else normalized

    async def _resolve_instrument(
        self,
        *,
        market_type: str,
        instrument_id: str,
        query: str,
    ):
        assert self._agentos is not None
        if instrument_id.strip():
            requested_id = instrument_id.strip()
            value = parse_market_instrument(await self._agentos.get_market_instrument(requested_id))
            if value.instrument_id != requested_id:
                raise TradingReadinessError("Core instrument 响应与请求 id 不一致")
            if value.market_type != market_type:
                raise TradingReadinessError("Core instrument 与请求交易类型不一致")
            return value
        if not query.strip():
            raise ValueError("必须提供 Core instrument_id 或可搜索资产")
        catalog_response = await self._agentos.get_market_catalog(
            market_type=market_type,
            query=catalog_asset_query(query),
            limit=20,
            trade_supported=True,
        )
        return resolve_unique_market_instrument(
            catalog_response,
            query=query,
            market_type=market_type,
        )

    @staticmethod
    def _normalize_market_data_symbols(symbols: list[object]) -> list[str]:
        """Normalize untrusted read-only ticker hints without execution identity.

        Execution paths still resolve a unique Core Market Catalog instrument
        and fail closed on stale metadata. This helper is only for Mainnet
        market-data reads, where Core validates the final symbol allowlist.
        """

        if not symbols or len(symbols) > 50 or any(not isinstance(value, str) for value in symbols):
            raise ValueError("market_tickers symbols 必须包含 1 到 50 个字符串")
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_value in symbols:
            if not isinstance(raw_value, str):  # narrowed above; explicit for static typing
                raise ValueError("market_tickers symbols 必须包含字符串")
            symbol = catalog_asset_query(raw_value)
            if symbol not in seen:
                seen.add(symbol)
                normalized.append(symbol)
        return normalized

    async def _read_current_trading_state(self, unified_account_id: str) -> None:
        assert self._agentos is not None
        await self._agentos.get_account_summary(unified_account_id)
        await self._agentos.get_balances(unified_account_id)
        await self._agentos.get_positions()
        await self._agentos.get_orders()

    async def _require_market_snapshot(self, symbol: str) -> None:
        """Read one batched Core ticker snapshot and fail closed when stale/missing."""

        response = await self._core.get_market_tickers([symbol])
        if response.meta.stale or not response.data:
            raise TradingReadinessError("Core 行情快照不可用或已过期，禁止创建 Draft")
        ticker = response.data[0]
        if ticker.symbol != symbol.upper() or ticker.stale:
            raise TradingReadinessError("Core 行情快照与交易标的不一致或已过期")

    @staticmethod
    def _assert_context_matches(readiness, execution) -> None:
        if (
            execution.unified_account_id != readiness.unified_account_id
            or execution.venue_account_id != readiness.venue_account_id
            or execution.agent_trading_account_id != readiness.agent_trading_account_id
            or execution.mandate_id != readiness.mandate_id
        ):
            raise TradingReadinessError(
                "Core USER Readiness 与 AGENT Strategy Binding 账户上下文不一致"
            )

    @staticmethod
    def _positive_decimal(value: object, field: str) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"{field} 不是合法 Decimal") from exc
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError(f"{field} 必须大于 0")
        return parsed

    @staticmethod
    def _has_open_position(response: dict[str, Any], symbol: str) -> bool:
        data = response.get("data")
        values = data if isinstance(data, list) else []
        return any(
            isinstance(item, dict)
            and str(item.get("symbol") or "").upper() == symbol.upper()
            and str(item.get("quantity") or item.get("qty") or "0") not in {"", "0", "0.0"}
            for item in values
        )

    @staticmethod
    def _stable_draft_key(namespace: str, user_id: str, key: str | None) -> str:
        if key is None or not key.strip():
            raise ValueError("创建 Draft 必须提供稳定 Idempotency-Key")
        if not user_id.strip():
            raise ValueError("创建 Draft 缺少已认证用户上下文")
        digest = hashlib.sha256(f"{user_id.strip()}\0{key.strip()}".encode()).hexdigest()
        return f"idem_{namespace}_{digest}"

    @staticmethod
    def _payload_fingerprint(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    async def _once(self, key: str, fingerprint: str, operation) -> dict[str, Any]:
        cached = self._draft_results.get(key)
        if cached is not None:
            if cached[0] != fingerprint:
                raise ValueError("同一 Idempotency-Key 不能用于不同 Draft 意图")
            return cached[1]
        lock = self._draft_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._draft_results.get(key)
            if cached is not None:
                if cached[0] != fingerprint:
                    raise ValueError("同一 Idempotency-Key 不能用于不同 Draft 意图")
                return cached[1]
            result = await operation()
            if len(self._draft_results) >= 256:
                self._draft_results.pop(next(iter(self._draft_results)))
            self._draft_results[key] = (fingerprint, result)
            return result
