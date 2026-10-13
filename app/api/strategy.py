"""策略容器 REST 端点。前缀 /v1/agent/strategies 由 main.py 挂载。

user_id/agent_id 从请求头派生（MVP：X-User-Id / X-Agent-Id），不入 body。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, Query, status

from app.api import schemas
from app.api.deps import get_container
from app.api.errors import _map_upstream_error, error_response, new_trace_id
from app.audit.hashing import args_hash
from app.audit.store import AuditRecord
from app.container import Container
from app.policies.engine import PolicyEngine
from app.strategy.analytics import (
    ZERO_METRICS,
    StrategyMetrics,
    build_curve,
    compute_metrics,
    merge_by_timestamp,
)
from app.strategy.container import IllegalStateTransition, StrategyContainer
from app.strategy.equity import DEFAULT_RANGE, EquitySnapshot, parse_range
from app.strategy.manager import (
    OverAllocationError,
    StrategyManager,
    StrategyNotFound,
    derive_mandate,
)
from app.strategy.templates import list_templates, to_plain
from app.tools.agentos.trade_flow import (
    IntentParseError,
    StrategyBindingError,
    build_draft_request,
    parse_trade_intent,
    render_confirmation_card,
    resolve_strategy_execution_context,
)

router = APIRouter(tags=["strategy"])
_ERR = {"model": schemas.ErrorEnvelope}
Ctn = Annotated[Container, Depends(get_container)]
UserId = Annotated[str, Header(alias="X-User-Id")]
AgentId = Annotated[str, Header(alias="X-Agent-Id")]
IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
_AGENTOS_DRAFT_TOOL = "agentos_create_trade_draft"


def _now() -> datetime:
    return datetime.now(UTC)


def _mgr(container: Container) -> StrategyManager:
    """取容器内 StrategyManager；build_container 恒初始化，此处仅为类型收窄（不用 assert，避免 -O 下被剥离）。"""
    if container.strategy_manager is None:
        raise RuntimeError("strategy_manager 未装配")
    return container.strategy_manager


def _format_decimal_str(val: Any) -> str:
    """格式化数值为非科学计数法的标准十进制字符串（如 20000 -> '20000'，而非 '2E+4'）。"""
    if val is None:
        return "0"
    from decimal import InvalidOperation

    try:
        dec = val if isinstance(val, Decimal) else Decimal(str(val))
        s = f"{dec:f}"
    except (InvalidOperation, ValueError):
        s = str(val)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def _optional_decimal_str(val: Decimal | None) -> str | None:
    """可选金额字段：未设置保持 None，不落成 "0"。"""
    return None if val is None else _format_decimal_str(val)


def _metrics_view(m: StrategyMetrics) -> schemas.StrategyMetricsView:
    """Decimal → 前端友好的 float，各字段按展示精度定量。"""
    return schemas.StrategyMetricsView(
        total_pnl=float(m.total_pnl.quantize(Decimal("0.01"))),
        total_pnl_pct=float(m.total_pnl_pct.quantize(Decimal("0.01"))),
        win_rate=float(m.win_rate.quantize(Decimal("0.001"))),
        max_drawdown=float(m.max_drawdown.quantize(Decimal("0.0001"))),
    )


async def _snapshots(
    container: Container, user_id: str, agent_id: str, **kwargs: Any
) -> list[EquitySnapshot]:
    """取净值快照；未装配净值存储时按「暂无数据」处理，不让大屏 500。"""
    if container.equity_store is None:
        return []
    return await container.equity_store.list_range(user_id, agent_id, **kwargs)


async def _metrics_by_strategy(
    container: Container, user_id: str, agent_id: str
) -> tuple[dict[str, StrategyMetrics], StrategyMetrics]:
    """一次取全量快照，算出每策略指标 + Agent 级汇总指标。"""
    snapshots = await _snapshots(container, user_id, agent_id)
    grouped: dict[str, list[EquitySnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        grouped[snapshot.strategy_id].append(snapshot)
    per_strategy = {sid: compute_metrics(series) for sid, series in grouped.items()}
    return per_strategy, compute_metrics(merge_by_timestamp(snapshots))


def _view(c: StrategyContainer, metrics: StrategyMetrics = ZERO_METRICS) -> schemas.StrategyView:
    return schemas.StrategyView(
        **_metrics_view(metrics).model_dump(),
        strategy_id=c.strategy_id,
        user_id=c.user_id,
        agent_id=c.agent_id,
        name=c.name,
        template_id=c.template_id,
        runtime_env=c.runtime_env,
        allocation=_format_decimal_str(c.allocation),
        capital_capacity=_format_decimal_str(c.capital_capacity),
        strategy_capacity=_format_decimal_str(c.strategy_capacity),
        max_leverage=_format_decimal_str(c.max_leverage),
        allowed_assets=list(c.allowed_assets) if c.allowed_assets is not None else None,
        expires_at=c.expires_at,
        # 未设置就保持 null —— 不能借 _format_decimal_str 变成 "0"（0 意味着立刻平仓）
        take_profit_pct=_optional_decimal_str(c.take_profit_pct),
        stop_loss_pct=_optional_decimal_str(c.stop_loss_pct),
        state=c.state.value,
        version=c.version,
    )


@router.get(
    "/strategy-templates",
    response_model=schemas.StrategyTemplateList,
    summary="官方策略模板库（供前端初始化模板卡片/下拉菜单）",
)
async def list_strategy_templates():
    return schemas.StrategyTemplateList(
        items=[
            schemas.StrategyTemplateView(
                template_id=t.template_id,
                name=t.name,
                description=t.description,
                recommended_leverage=_format_decimal_str(t.recommended_leverage),
                default_allocation=_format_decimal_str(t.default_allocation),
                risk_level=t.risk_level.value,
                params_schema=to_plain(t.params_schema),
            )
            for t in list_templates()
        ]
    )


@router.post(
    "/strategies",
    response_model=schemas.StrategyView,
    status_code=status.HTTP_201_CREATED,
    responses={400: _ERR, 409: _ERR},
)
async def create_strategy(
    body: schemas.StrategyCreateRequest,
    container: Ctn,
    x_user_id: UserId = "u_dev",
    x_agent_id: AgentId = "agent-test-001",
):
    try:
        c = await _mgr(container).create(
            user_id=x_user_id,
            agent_id=x_agent_id,
            name=body.name,
            template_id=body.template_id,
            runtime_env=body.runtime_env,
            allocation=body.allocation,
            capital_capacity=body.capital_capacity,
            strategy_capacity=body.strategy_capacity,
            max_leverage=body.max_leverage,
            allowed_assets=tuple(body.allowed_assets) if body.allowed_assets else None,
            expires_at=body.expires_at,
            now=_now(),
            take_profit_pct=body.take_profit_pct,
            stop_loss_pct=body.stop_loss_pct,
        )
    except ValueError as exc:
        return error_response("INVALID_REQUEST", f"策略参数非法：{exc}", 400)
    except OverAllocationError as exc:
        return error_response("POLICY_VIOLATION", str(exc), 409)
    return _view(c)


@router.get("/strategies", response_model=schemas.StrategyList)
async def list_strategies(
    container: Ctn, x_user_id: UserId = "u_dev", x_agent_id: AgentId = "agent-test-001"
):
    items = await _mgr(container).list(x_user_id, x_agent_id)
    per_strategy, summary = await _metrics_by_strategy(container, x_user_id, x_agent_id)
    return schemas.StrategyList(
        items=[_view(c, per_strategy.get(c.strategy_id, ZERO_METRICS)) for c in items],
        summary=_metrics_view(summary),
    )


# 必须注册在 /strategies/{strategy_id} 之前，否则 equity-history 会被当成 strategy_id
@router.get(
    "/strategies/equity-history",
    response_model=schemas.EquityHistory,
    summary="策略净值历史曲线（默认该 Agent 全部策略合并，可按 strategy_id 过滤）",
    responses={400: _ERR, 404: _ERR},
)
async def strategy_equity_history(
    container: Ctn,
    range_: Annotated[str, Query(alias="range", description="1h / 6h / 24h")] = DEFAULT_RANGE,
    strategy_id: Annotated[str | None, Query(description="不传则返回该 Agent 的合并曲线")] = None,
    x_user_id: UserId = "u_dev",
    x_agent_id: AgentId = "agent-test-001",
):
    try:
        window = parse_range(range_)
    except ValueError as exc:
        return error_response("INVALID_REQUEST", str(exc), 400)

    if strategy_id is not None:
        try:
            await _mgr(container).get_owned(strategy_id, x_user_id, x_agent_id)
        except StrategyNotFound:
            return error_response("SESSION_NOT_FOUND", "策略不存在", 404)

    now = _now()
    snapshots = await _snapshots(
        container, x_user_id, x_agent_id, start=now - window, end=now, strategy_id=strategy_id
    )
    series = snapshots if strategy_id is not None else merge_by_timestamp(snapshots)
    return schemas.EquityHistory(
        points=[
            schemas.EquityPointView(
                timestamp=p.timestamp,
                equity=float(p.equity.quantize(Decimal("0.01"))),
                pnl=float(p.pnl.quantize(Decimal("0.01"))),
                drawdown_pct=float(p.drawdown_pct.quantize(Decimal("0.01"))),
            )
            for p in build_curve(series)
        ]
    )


@router.get("/strategies/{strategy_id}", response_model=schemas.StrategyView, responses={404: _ERR})
async def get_strategy(
    strategy_id: str,
    container: Ctn,
    x_user_id: UserId = "u_dev",
    x_agent_id: AgentId = "agent-test-001",
):
    try:
        c = await _mgr(container).get_owned(strategy_id, x_user_id, x_agent_id)
    except StrategyNotFound:
        return error_response("SESSION_NOT_FOUND", "策略不存在", 404)
    snapshots = await _snapshots(container, x_user_id, x_agent_id, strategy_id=strategy_id)
    return _view(c, compute_metrics(snapshots))


async def _do_transition(
    container: Container, strategy_id: str, user_id: str, agent_id: str, method: str
):
    try:
        c = await getattr(_mgr(container), method)(strategy_id, user_id, agent_id, _now())
    except StrategyNotFound:
        return error_response("SESSION_NOT_FOUND", "策略不存在", 404)
    except IllegalStateTransition as exc:
        return error_response("INVALID_REQUEST", str(exc), 409)
    return _view(c)


@router.post(
    "/strategies/{strategy_id}/pause",
    response_model=schemas.StrategyView,
    responses={404: _ERR, 409: _ERR},
)
async def pause_strategy(
    strategy_id: str,
    container: Ctn,
    x_user_id: UserId = "u_dev",
    x_agent_id: AgentId = "agent-test-001",
):
    return await _do_transition(container, strategy_id, x_user_id, x_agent_id, "pause")


@router.post(
    "/strategies/{strategy_id}/resume",
    response_model=schemas.StrategyView,
    responses={404: _ERR, 409: _ERR},
)
async def resume_strategy(
    strategy_id: str,
    container: Ctn,
    x_user_id: UserId = "u_dev",
    x_agent_id: AgentId = "agent-test-001",
):
    return await _do_transition(container, strategy_id, x_user_id, x_agent_id, "resume")


@router.post(
    "/strategies/{strategy_id}/terminate",
    response_model=schemas.StrategyView,
    responses={404: _ERR, 409: _ERR},
)
async def terminate_strategy(
    strategy_id: str,
    container: Ctn,
    x_user_id: UserId = "u_dev",
    x_agent_id: AgentId = "agent-test-001",
):
    return await _do_transition(container, strategy_id, x_user_id, x_agent_id, "terminate")


@router.post(
    "/strategies/{strategy_id}/allocate",
    response_model=schemas.StrategyView,
    responses={400: _ERR, 404: _ERR, 409: _ERR},
)
async def allocate_strategy(
    strategy_id: str,
    body: schemas.StrategyAllocateRequest,
    container: Ctn,
    x_user_id: UserId = "u_dev",
    x_agent_id: AgentId = "agent-test-001",
):
    try:
        c = await _mgr(container).allocate(
            strategy_id, x_user_id, x_agent_id, body.allocation, _now()
        )
    except StrategyNotFound:
        return error_response("SESSION_NOT_FOUND", "策略不存在", 404)
    except ValueError as exc:
        return error_response("INVALID_REQUEST", f"额度非法：{exc}", 400)
    except IllegalStateTransition as exc:
        return error_response("INVALID_REQUEST", str(exc), 409)
    return _view(c)


@router.post(
    "/strategies/{strategy_id}/trade-draft",
    response_model=schemas.TradeDraftCard,
    status_code=status.HTTP_201_CREATED,
    summary="策略上下文内：文本下单意图 → Core StrategyBinding 解析 + 本地预检 + 建交易草稿",
    responses={400: _ERR, 403: _ERR, 404: _ERR, 409: _ERR, 502: _ERR, 503: _ERR},
)
async def strategy_trade_draft(
    strategy_id: str,
    body: schemas.StrategyTradeDraftRequest,
    container: Ctn,
    x_user_id: UserId = "u_dev",
    x_agent_id: AgentId = "agent-test-001",
    idempotency_key: IdempotencyKey = None,
):
    now = _now()
    trace_id = new_trace_id()
    try:
        strat = await _mgr(container).get_owned(strategy_id, x_user_id, x_agent_id)
    except StrategyNotFound:
        return error_response("SESSION_NOT_FOUND", "策略不存在", 404)
    try:
        _mgr(container).ensure_tradable(strat, now)
    except IllegalStateTransition as exc:
        return error_response("INVALID_REQUEST", str(exc), 409)
    if container.agentos_client is None:
        return error_response("INTERNAL", "交易真链路未配置（缺 AGENT_CORE_ACTOR_TOKEN）", 503)

    # Core 先按 AGENT Token 校验 StrategyBinding 归属；Runtime 只能使用其精确绑定，
    # 不能从 ACTIVE 账户列表任选一个，也不信任客户端传入任何账户关系。
    try:
        execution = await resolve_strategy_execution_context(container.agentos_client, strategy_id)
    except StrategyBindingError as exc:
        return error_response("POLICY_VIOLATION", f"策略绑定不可执行：{exc}", 409)
    except httpx.HTTPError as exc:
        return _map_upstream_error(
            exc,
            observer=container.core_credential_readiness,
            trace_id=trace_id,
        )

    try:
        intent = await parse_trade_intent(container.llm, body.text)
    except IntentParseError as exc:
        return error_response("INVALID_REQUEST", f"无法解析交易意图：{exc}", 400)

    request = build_draft_request(
        intent,
        unified_account_id=execution.unified_account_id,
        venue_account_id=execution.venue_account_id,
        strategy_id=execution.strategy_id,
        mandate_id=execution.mandate_id,
        default_leverage=container.settings.agentos_default_leverage,
        default_max_slippage=container.settings.agentos_default_max_slippage,
    )
    body_dict = request.to_body()
    # 策略级 runtime 预检（Core 仍权威）：名义额度 = qty * price，与策略 allocation 比较
    decision = PolicyEngine(derive_mandate(strat)).check_trade_draft(
        {
            **body_dict,
            "notional": None,
            "qty": body_dict["quantity"],
            "price": body_dict["limit_price"],
        },
        now,
    )
    if not decision.allowed:
        return error_response("POLICY_VIOLATION", decision.reason, 403)

    try:
        stable_idempotency_key = idempotency_key or (
            "idem_strategy_" + args_hash({"strategy_id": strategy_id, "text": body.text})
        )
        draft = await container.agentos_client.create_trade_draft(
            body_dict, idempotency_key=stable_idempotency_key
        )
    except httpx.HTTPError as exc:
        return _map_upstream_error(
            exc,
            observer=container.core_credential_readiness,
            trace_id=trace_id,
        )

    data = draft.get("data") or {}
    draft_id = data.get("id")
    await container.audit_store.record_tool_call(
        AuditRecord(
            trace_id=trace_id,
            session_id=strategy_id,
            tool_name=_AGENTOS_DRAFT_TOOL,
            args_hash=args_hash({"text": body.text, "strategy_id": strategy_id}),
            result_ref=draft_id or "no_draft",
            created_at=now,
            status="ok",
        )
    )
    return schemas.TradeDraftCard(
        card=render_confirmation_card(draft),
        draft_id=draft_id,
        draft_version=data.get("version"),
        status=data.get("status"),
        draft=draft,
        trace_id=trace_id,
    )
