"""策略收益字段与净值曲线端点。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from app.config import Settings
from app.container import build_container
from app.main import create_app
from app.strategy.equity import EquitySnapshot

_HDR = {"X-User-Id": "u1", "X-Agent-Id": "a1"}
_OTHER = {"X-User-Id": "u1", "X-Agent-Id": "OTHER"}
_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _app():
    app = create_app()
    app.state.container = build_container(Settings(provider_mode="fake"))
    return app


def _api(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _body(**over):
    base = {
        "name": "趋势1", "template_id": "tpl_trend", "runtime_env": "paper",
        "allocation": "1000", "capital_capacity": "5000", "strategy_capacity": "10000",
        "max_leverage": "3",
    }
    base.update(over)
    return base


async def _seed(app, api, *, minutes_ago_equity, strategy_body=None, base=None):
    """建一个策略并按 (距今分钟数, 净值) 写入快照。返回 strategy_id。

    需要跨策略合并的用例要传同一个 `base`，否则各自的 `datetime.now()` 差几微秒，
    快照落在不同时间戳上就合并不起来。
    """
    sid = (
        await api.post("/v1/agent/strategies", json=strategy_body or _body(), headers=_HDR)
    ).json()["strategy_id"]
    now = base or datetime.now(UTC)
    store = app.state.container.equity_store
    await store.record_many(
        [
            EquitySnapshot(
                strategy_id=sid, user_id="u1", agent_id="a1",
                taken_at=now - timedelta(minutes=minutes),
                equity=Decimal(str(equity)),
            )
            for minutes, equity in minutes_ago_equity
        ]
    )
    return sid


# --- 列表/详情上的收益字段 ------------------------------------------------


async def test_list_exposes_pnl_fields_per_strategy():
    app = _app()
    async with _api(app) as api:
        await _seed(app, api, minutes_ago_equity=[(30, 22000), (20, 24000), (10, 22805.50)])
        item = (await api.get("/v1/agent/strategies", headers=_HDR)).json()["items"][0]
        assert item["total_pnl"] == 805.5
        assert item["total_pnl_pct"] == 3.66
        assert item["win_rate"] == 0.5
        assert item["max_drawdown"] == 0.0498


async def test_list_summary_aggregates_all_strategies():
    app = _app()
    base = datetime.now(UTC)
    async with _api(app) as api:
        await _seed(app, api, minutes_ago_equity=[(20, 10000), (10, 11000)], base=base)
        await _seed(
            app, api,
            minutes_ago_equity=[(20, 10000), (10, 9500)],
            strategy_body=_body(name="趋势2"),
            base=base,
        )
        body = (await api.get("/v1/agent/strategies", headers=_HDR)).json()
        # 汇总按同一时刻求和：20000 → 20500
        assert body["summary"]["total_pnl"] == 500.0
        assert body["summary"]["total_pnl_pct"] == 2.5


async def test_metrics_are_zero_without_snapshots():
    app = _app()
    async with _api(app) as api:
        r = await api.post("/v1/agent/strategies", json=_body(), headers=_HDR)
        assert r.json()["total_pnl"] == 0.0
        body = (await api.get("/v1/agent/strategies", headers=_HDR)).json()
        assert body["items"][0]["win_rate"] == 0.0
        assert body["summary"] == {
            "total_pnl": 0.0, "total_pnl_pct": 0.0, "win_rate": 0.0, "max_drawdown": 0.0
        }


async def test_detail_exposes_pnl_fields():
    app = _app()
    async with _api(app) as api:
        sid = await _seed(app, api, minutes_ago_equity=[(20, 10000), (10, 10500)])
        item = (await api.get(f"/v1/agent/strategies/{sid}", headers=_HDR)).json()
        assert item["total_pnl"] == 500.0
        assert item["total_pnl_pct"] == 5.0


# --- equity-history ------------------------------------------------------


async def test_equity_history_shape_matches_contract():
    app = _app()
    async with _api(app) as api:
        await _seed(app, api, minutes_ago_equity=[(30, 22000), (20, 24000), (10, 23712)])
        body = (await api.get("/v1/agent/strategies/equity-history", headers=_HDR)).json()
        assert list(body) == ["points"]
        assert len(body["points"]) == 3
        first, last = body["points"][0], body["points"][-1]
        assert set(first) == {"timestamp", "equity", "pnl", "drawdown_pct"}
        assert isinstance(first["timestamp"], int)
        assert first["equity"] == 22000.0 and first["pnl"] == 0.0
        assert last["equity"] == 23712.0
        assert last["pnl"] == 1712.0
        assert last["drawdown_pct"] == 1.2


async def test_equity_history_range_window_filters_points():
    app = _app()
    async with _api(app) as api:
        await _seed(app, api, minutes_ago_equity=[(200, 10000), (30, 11000), (10, 12000)])
        one_hour = (
            await api.get("/v1/agent/strategies/equity-history?range=1h", headers=_HDR)
        ).json()
        assert [p["equity"] for p in one_hour["points"]] == [11000.0, 12000.0]
        six_hour = (
            await api.get("/v1/agent/strategies/equity-history?range=6h", headers=_HDR)
        ).json()
        assert len(six_hour["points"]) == 3


async def test_equity_history_defaults_to_24h():
    app = _app()
    async with _api(app) as api:
        await _seed(app, api, minutes_ago_equity=[(60 * 30, 10000), (10, 12000)])
        body = (await api.get("/v1/agent/strategies/equity-history", headers=_HDR)).json()
        assert [p["equity"] for p in body["points"]] == [12000.0]


async def test_equity_history_merges_strategies_by_default():
    app = _app()
    base = datetime.now(UTC)
    async with _api(app) as api:
        sid1 = await _seed(app, api, minutes_ago_equity=[(10, 10000)], base=base)
        await _seed(
            app, api, minutes_ago_equity=[(10, 5000)], strategy_body=_body(name="趋势2"), base=base
        )
        merged = (await api.get("/v1/agent/strategies/equity-history", headers=_HDR)).json()
        # 两条策略共享同一采样时刻 → 合并成一个点
        assert [p["equity"] for p in merged["points"]] == [15000.0]
        single = (
            await api.get(
                f"/v1/agent/strategies/equity-history?strategy_id={sid1}", headers=_HDR
            )
        ).json()
        assert [p["equity"] for p in single["points"]] == [10000.0]


async def test_equity_history_empty_when_no_snapshots():
    async with _api(_app()) as api:
        body = (await api.get("/v1/agent/strategies/equity-history", headers=_HDR)).json()
        assert body == {"points": []}


async def test_equity_history_invalid_range_400():
    async with _api(_app()) as api:
        r = await api.get("/v1/agent/strategies/equity-history?range=7d", headers=_HDR)
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_REQUEST"


async def test_equity_history_unknown_strategy_404():
    async with _api(_app()) as api:
        r = await api.get(
            "/v1/agent/strategies/equity-history?strategy_id=strat_nope", headers=_HDR
        )
        assert r.status_code == 404


async def test_equity_history_cross_agent_strategy_404():
    app = _app()
    async with _api(app) as api:
        sid = await _seed(app, api, minutes_ago_equity=[(10, 10000)])
        r = await api.get(
            f"/v1/agent/strategies/equity-history?strategy_id={sid}", headers=_OTHER
        )
        assert r.status_code == 404


async def test_equity_history_isolated_across_agents():
    app = _app()
    async with _api(app) as api:
        await _seed(app, api, minutes_ago_equity=[(10, 10000)])
        body = (await api.get("/v1/agent/strategies/equity-history", headers=_OTHER)).json()
        assert body == {"points": []}


async def test_endpoints_degrade_gracefully_without_equity_store():
    """净值存储未装配时按「暂无数据」返回，不能让大屏拿到 500。"""
    app = _app()
    app.state.container.equity_store = None
    async with _api(app) as api:
        sid = (
            await api.post("/v1/agent/strategies", json=_body(), headers=_HDR)
        ).json()["strategy_id"]
        lst = await api.get("/v1/agent/strategies", headers=_HDR)
        assert lst.status_code == 200
        assert lst.json()["items"][0]["total_pnl"] == 0.0
        assert (await api.get(f"/v1/agent/strategies/{sid}", headers=_HDR)).json()["win_rate"] == 0.0
        history = await api.get("/v1/agent/strategies/equity-history", headers=_HDR)
        assert history.json() == {"points": []}


async def test_equity_history_route_not_shadowed_by_detail_route():
    """/strategies/equity-history 必须先于 /strategies/{strategy_id} 匹配。"""
    async with _api(_app()) as api:
        r = await api.get("/v1/agent/strategies/equity-history", headers=_HDR)
        assert r.status_code == 200, r.text
