"""模板查询端点 + 创建策略时的 TP/SL 往返。"""

import httpx

from app.config import Settings
from app.container import build_container
from app.main import create_app
from app.strategy.templates import list_templates

_HDR = {"X-User-Id": "u1", "X-Agent-Id": "a1"}


def _app():
    app = create_app()
    app.state.container = build_container(Settings(provider_mode="fake"))
    return app


def _api(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _body(**over):
    base = {
        "name": "网格1", "template_id": "grid_trading_v1", "runtime_env": "paper",
        "allocation": "1000", "capital_capacity": "5000", "strategy_capacity": "10000",
        "max_leverage": "3",
    }
    base.update(over)
    return base


# --- GET /v1/agent/strategy-templates ------------------------------------


async def test_lists_all_official_templates():
    async with _api(_app()) as api:
        r = await api.get("/v1/agent/strategy-templates", headers=_HDR)
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert [t["template_id"] for t in items] == [
            t.template_id for t in list_templates()
        ]


async def test_template_item_shape_is_renderable():
    async with _api(_app()) as api:
        item = (await api.get("/v1/agent/strategy-templates", headers=_HDR)).json()["items"][0]
        assert set(item) == {
            "template_id", "name", "description", "recommended_leverage",
            "default_allocation", "risk_level", "params_schema",
        }
        assert item["template_id"] == "grid_trading_v1"
        assert item["risk_level"] in {"LOW", "MEDIUM", "HIGH"}
        # 金额/杠杆按既有约定走字符串，且不能是科学计数法
        assert isinstance(item["recommended_leverage"], str)
        assert "E" not in item["default_allocation"]
        assert item["params_schema"]["type"] == "object"


async def test_templates_endpoint_needs_no_prior_state():
    """前端初始化下拉菜单时还没有任何策略，端点必须能独立返回。"""
    async with _api(_app()) as api:
        assert len((await api.get("/v1/agent/strategy-templates", headers=_HDR)).json()["items"]) == 5


# --- 创建策略时的 TP/SL --------------------------------------------------


async def test_create_accepts_and_echoes_tp_sl():
    async with _api(_app()) as api:
        r = await api.post(
            "/v1/agent/strategies",
            json=_body(take_profit_pct="0.15", stop_loss_pct="0.05"),
            headers=_HDR,
        )
        assert r.status_code == 201, r.text
        assert r.json()["take_profit_pct"] == "0.15"
        assert r.json()["stop_loss_pct"] == "0.05"


async def test_tp_sl_absent_serializes_as_null():
    async with _api(_app()) as api:
        body = (await api.post("/v1/agent/strategies", json=_body(), headers=_HDR)).json()
        assert body["take_profit_pct"] is None
        assert body["stop_loss_pct"] is None


async def test_tp_sl_survive_get_and_list():
    async with _api(_app()) as api:
        sid = (
            await api.post(
                "/v1/agent/strategies", json=_body(take_profit_pct="0.2", stop_loss_pct="0.1"),
                headers=_HDR,
            )
        ).json()["strategy_id"]
        got = (await api.get(f"/v1/agent/strategies/{sid}", headers=_HDR)).json()
        assert (got["take_profit_pct"], got["stop_loss_pct"]) == ("0.2", "0.1")
        listed = (await api.get("/v1/agent/strategies", headers=_HDR)).json()["items"][0]
        assert (listed["take_profit_pct"], listed["stop_loss_pct"]) == ("0.2", "0.1")


async def test_invalid_stop_loss_rejected_with_400():
    async with _api(_app()) as api:
        r = await api.post("/v1/agent/strategies", json=_body(stop_loss_pct="1.5"), headers=_HDR)
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_REQUEST"


async def test_invalid_take_profit_rejected_with_400():
    async with _api(_app()) as api:
        r = await api.post("/v1/agent/strategies", json=_body(take_profit_pct="0"), headers=_HDR)
        assert r.status_code == 400
