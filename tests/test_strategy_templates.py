"""官方策略模板注册表 —— 内容契约 + 查询接口。"""

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from app.strategy.templates import (
    OFFICIAL_TEMPLATE_IDS,
    RiskLevel,
    StrategyTemplate,
    TemplateNotFound,
    get_template,
    list_templates,
)

_EXPECTED_IDS = (
    "grid_trading_v1",
    "dca_accumulator_v1",
    "trend_following_v1",
    "martingale_v1",
    "news_sentiment_v1",
)


def test_first_batch_ships_exactly_five_templates():
    assert tuple(t.template_id for t in list_templates()) == _EXPECTED_IDS
    assert OFFICIAL_TEMPLATE_IDS == frozenset(_EXPECTED_IDS)


@pytest.mark.parametrize("template", list_templates(), ids=lambda t: t.template_id)
def test_every_template_is_complete(template: StrategyTemplate):
    """前端卡片直接渲染这些字段，缺一个就是空白位。"""
    assert template.name.strip()
    assert template.description.strip()
    assert template.recommended_leverage > 0
    assert template.default_allocation > 0
    assert isinstance(template.risk_level, RiskLevel)


@pytest.mark.parametrize("template", list_templates(), ids=lambda t: t.template_id)
def test_every_params_schema_is_usable_json_schema(template: StrategyTemplate):
    schema = template.params_schema
    assert schema["type"] == "object"
    assert schema["required"], "至少要有一个必填参数，否则前端无从生成表单"
    properties = schema["properties"]
    # required 里列的参数必须在 properties 里有定义，且都带类型与说明
    for name in schema["required"]:
        assert name in properties, f"{template.template_id} 的 required 引用了未定义的 {name}"
    for name, spec in properties.items():
        assert spec.get("type"), f"{template.template_id}.{name} 缺 type"
        assert spec.get("description"), f"{template.template_id}.{name} 缺 description"


def test_params_schema_is_immutable():
    """模板是全局共享的单例，任何一处误改都会污染所有请求。"""
    schema = get_template("grid_trading_v1").params_schema
    with pytest.raises(TypeError):
        schema["type"] = "tampered"  # type: ignore[index]


def test_template_dataclass_is_frozen():
    with pytest.raises(FrozenInstanceError):
        get_template("grid_trading_v1").name = "改名"  # type: ignore[misc]


def test_risk_levels_span_the_spectrum():
    """5 个模板要覆盖高中低三档，否则前端的风险筛选没有意义。"""
    levels = {t.risk_level for t in list_templates()}
    assert levels == {RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH}


def test_martingale_is_marked_high_risk():
    """马丁格尔是加仓摊平型策略，风险等级标错会直接误导用户。"""
    assert get_template("martingale_v1").risk_level is RiskLevel.HIGH


def test_dca_is_low_risk_and_unleveraged():
    dca = get_template("dca_accumulator_v1")
    assert dca.risk_level is RiskLevel.LOW
    assert dca.recommended_leverage == Decimal(1)


def test_get_template_returns_the_matching_entry():
    assert get_template("grid_trading_v1").template_id == "grid_trading_v1"


def test_get_template_rejects_unknown_id():
    with pytest.raises(TemplateNotFound, match="nope"):
        get_template("nope")


def test_list_templates_returns_a_stable_tuple():
    """返回值不可变，调用方拿不到内部列表的引用。"""
    assert isinstance(list_templates(), tuple)
    assert list_templates() == list_templates()
