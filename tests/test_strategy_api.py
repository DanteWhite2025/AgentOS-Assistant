import httpx

from app.config import Settings
from app.container import build_container
from app.main import create_app
from app.strategy.manager import OverAllocationError

_HDR = {"X-User-Id": "u1", "X-Agent-Id": "a1"}


def _app():
    app = create_app()
    app.state.container = build_container(Settings(provider_mode="fake"))
    return app


def _api(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _body(**over):
    base = {
        "name": "趋势1", "template_id": "tpl_trend", "runtime_env": "paper",
        "allocation": "100", "capital_capacity": "500", "strategy_capacity": "1000", "max_leverage": "3",
    }
    base.update(over)
    return base


async def test_create_and_get_and_list():
    async with _api(_app()) as api:
        r = await api.post("/v1/agent/strategies", json=_body(), headers=_HDR)
        assert r.status_code == 201, r.text
        sid = r.json()["strategy_id"]
        assert r.json()["state"] == "ACTIVE"
        assert (await api.get(f"/v1/agent/strategies/{sid}", headers=_HDR)).status_code == 200
        lst = await api.get("/v1/agent/strategies", headers=_HDR)
        assert [s["strategy_id"] for s in lst.json()["items"]] == [sid]


async def test_create_invalid_invariant_400():
    async with _api(_app()) as api:
        r = await api.post("/v1/agent/strategies", json=_body(allocation="600"), headers=_HDR)
        assert r.status_code == 400
        assert r.json()["code"] == "INVALID_REQUEST"


async def test_get_cross_agent_404():
    async with _api(_app()) as api:
        sid = (await api.post("/v1/agent/strategies", json=_body(), headers=_HDR)).json()["strategy_id"]
        r = await api.get(f"/v1/agent/strategies/{sid}", headers={"X-User-Id": "u1", "X-Agent-Id": "OTHER"})
        assert r.status_code == 404


async def test_lifecycle_endpoints():
    async with _api(_app()) as api:
        sid = (await api.post("/v1/agent/strategies", json=_body(), headers=_HDR)).json()["strategy_id"]
        assert (await api.post(f"/v1/agent/strategies/{sid}/pause", headers=_HDR)).json()["state"] == "PAUSED"
        # 重复 pause → 409
        assert (await api.post(f"/v1/agent/strategies/{sid}/pause", headers=_HDR)).status_code == 409
        assert (await api.post(f"/v1/agent/strategies/{sid}/resume", headers=_HDR)).json()["state"] == "ACTIVE"
        assert (await api.post(f"/v1/agent/strategies/{sid}/terminate", headers=_HDR)).json()["state"] == "TERMINATED"


async def test_allocate_and_over_capacity_400():
    async with _api(_app()) as api:
        sid = (await api.post("/v1/agent/strategies", json=_body(), headers=_HDR)).json()["strategy_id"]
        assert (await api.post(f"/v1/agent/strategies/{sid}/allocate", json={"allocation": "250"}, headers=_HDR)).json()["allocation"] == "250"
        r = await api.post(f"/v1/agent/strategies/{sid}/allocate", json={"allocation": "600"}, headers=_HDR)
        assert r.status_code == 400


async def test_allocate_on_paused_strategy_409():
    async with _api(_app()) as api:
        sid = (await api.post("/v1/agent/strategies", json=_body(), headers=_HDR)).json()["strategy_id"]
        assert (await api.post(f"/v1/agent/strategies/{sid}/pause", headers=_HDR)).status_code == 200
        r = await api.post(f"/v1/agent/strategies/{sid}/allocate", json={"allocation": "250"}, headers=_HDR)
        assert r.status_code == 409
        assert r.json()["code"] == "INVALID_REQUEST"


async def test_allocate_missing_strategy_404():
    async with _api(_app()) as api:
        r = await api.post("/v1/agent/strategies/nope/allocate", json={"allocation": "10"}, headers=_HDR)
        assert r.status_code == 404
        assert r.json()["code"] == "SESSION_NOT_FOUND"


async def test_lifecycle_transitions_missing_strategy_404():
    async with _api(_app()) as api:
        for action in ("pause", "resume", "terminate"):
            r = await api.post(f"/v1/agent/strategies/nope/{action}", headers=_HDR)
            assert r.status_code == 404, action
            assert r.json()["code"] == "SESSION_NOT_FOUND"


async def test_mgr_raises_when_strategy_manager_not_assembled():
    """_mgr() 防御分支：容器未装配 strategy_manager 时应显式报错而非 assert（-O 下会被剥离）。"""
    app = _app()
    app.state.container.strategy_manager = None
    async with _api(app) as api:
        try:
            await api.get("/v1/agent/strategies", headers=_HDR)
        except RuntimeError as exc:
            assert "strategy_manager" in str(exc)
        else:
            raise AssertionError("expected RuntimeError to propagate")


async def test_create_over_agent_capital_409():
    """agent_capital 聚合校验目前未经 API 参数暴露，用桩 manager 覆盖该异常映射分支。"""
    app = _app()
    container = app.state.container

    async def _boom(**_kwargs):
        raise OverAllocationError("额度之和超过 Agent 可用本金")

    container.strategy_manager.create = _boom  # type: ignore[method-assign]
    async with _api(app) as api:
        r = await api.post("/v1/agent/strategies", json=_body(), headers=_HDR)
        assert r.status_code == 409
        assert r.json()["code"] == "POLICY_VIOLATION"


def test_format_decimal_str_no_scientific_notation():
    from decimal import Decimal

    from app.api.strategy import _format_decimal_str

    assert _format_decimal_str(Decimal(20000)) == "20000"
    assert _format_decimal_str(Decimal("2E+4")) == "20000"
    assert _format_decimal_str("2E+4") == "20000"
    assert _format_decimal_str(Decimal("1000.500")) == "1000.5"
    assert _format_decimal_str(Decimal("0.0001")) == "0.0001"
    assert _format_decimal_str("invalid_decimal") == "invalid_decimal"
    assert _format_decimal_str(None) == "0"

