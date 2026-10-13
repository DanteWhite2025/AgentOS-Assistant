"""官方策略模板注册表 —— 首期 5 个模板，供前端初始化卡片/下拉菜单。

模板是**只读的进程内常量**（无 IO、无 DB）：全局共享同一批实例，因此
`params_schema` 用 MappingProxyType 冻结，避免任一处误改污染所有请求。

`params_schema` 是标准 JSON Schema 片段，前端据此生成参数表单；后端当前不校验
用户填入的 params（策略参数的执行语义在策略引擎侧，不在容器层）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Final


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class TemplateNotFound(Exception):
    """template_id 不是官方模板（对应端点 404）。"""


def _schema(required: tuple[str, ...], properties: dict[str, dict[str, Any]]) -> Mapping[str, Any]:
    """组装并冻结一份 JSON Schema。"""
    return MappingProxyType(
        {
            "type": "object",
            "required": list(required),
            "properties": MappingProxyType(
                {k: MappingProxyType(v) for k, v in properties.items()}
            ),
        }
    )


@dataclass(frozen=True)
class StrategyTemplate:
    template_id: str
    name: str
    description: str
    recommended_leverage: Decimal
    default_allocation: Decimal
    risk_level: RiskLevel
    params_schema: Mapping[str, Any]


_GRID: Final = StrategyTemplate(
    template_id="grid_trading_v1",
    name="网格交易",
    description="在设定价格区间内等分挂单，震荡行情中低买高卖赚取网格差价。单边行情会持续累积浮亏。",
    recommended_leverage=Decimal(2),
    default_allocation=Decimal(1000),
    risk_level=RiskLevel.MEDIUM,
    params_schema=_schema(
        ("lower_price", "upper_price", "grid_count"),
        {
            "lower_price": {"type": "string", "description": "网格区间下沿价格"},
            "upper_price": {"type": "string", "description": "网格区间上沿价格"},
            "grid_count": {
                "type": "integer", "minimum": 2, "maximum": 200,
                "description": "网格数量，区间被等分成的格数",
            },
            "order_size": {"type": "string", "description": "每格下单数量，留空则按额度均分"},
        },
    ),
)

_DCA: Final = StrategyTemplate(
    template_id="dca_accumulator_v1",
    name="DCA 定投",
    description="按固定周期分批买入摊平成本，不择时、不加杠杆，适合长期建仓。",
    recommended_leverage=Decimal(1),
    default_allocation=Decimal(500),
    risk_level=RiskLevel.LOW,
    params_schema=_schema(
        ("interval_hours", "buy_amount"),
        {
            "interval_hours": {
                "type": "integer", "minimum": 1, "maximum": 720,
                "description": "定投间隔小时数",
            },
            "buy_amount": {"type": "string", "description": "每期买入金额"},
            "max_rounds": {
                "type": "integer", "minimum": 1,
                "description": "最多定投期数，留空则不限",
            },
        },
    ),
)

_TREND: Final = StrategyTemplate(
    template_id="trend_following_v1",
    name="趋势突破",
    description="价格突破近期高低点区间时顺势入场，以移动止损跟随趋势。震荡行情中易被反复触发。",
    recommended_leverage=Decimal(3),
    default_allocation=Decimal(2000),
    risk_level=RiskLevel.MEDIUM,
    params_schema=_schema(
        ("lookback_periods", "breakout_pct"),
        {
            "lookback_periods": {
                "type": "integer", "minimum": 5, "maximum": 500,
                "description": "回看多少个周期来确定突破区间",
            },
            "breakout_pct": {
                "type": "string",
                "description": "突破幅度阈值，0~1 比例，如 0.02 表示超出区间 2% 才入场",
            },
            "trailing_stop_pct": {
                "type": "string", "description": "移动止损比例，0~1，如 0.05 表示回撤 5% 离场",
            },
        },
    ),
)

_MARTINGALE: Final = StrategyTemplate(
    template_id="martingale_v1",
    name="马丁格尔",
    description="每次亏损后按倍数加仓以摊平成本。连续逆行时仓位呈指数增长，须严格设置最大加仓次数。",
    recommended_leverage=Decimal(2),
    default_allocation=Decimal(500),
    risk_level=RiskLevel.HIGH,
    params_schema=_schema(
        ("base_order_size", "multiplier", "max_steps"),
        {
            "base_order_size": {"type": "string", "description": "首单下单金额"},
            "multiplier": {
                "type": "string", "description": "每次加仓的倍数，如 2 表示逐次翻倍",
            },
            "max_steps": {
                "type": "integer", "minimum": 1, "maximum": 10,
                "description": "最大加仓次数，超过则停止加仓（防止仓位失控）",
            },
        },
    ),
)

_NEWS: Final = StrategyTemplate(
    template_id="news_sentiment_v1",
    name="News Trade",
    description="解析新闻与社交媒体情绪信号，在情绪强度越过阈值时短线入场。依赖信息源质量，波动剧烈。",
    recommended_leverage=Decimal(2),
    default_allocation=Decimal(1000),
    risk_level=RiskLevel.HIGH,
    params_schema=_schema(
        ("sentiment_threshold", "sources"),
        {
            "sentiment_threshold": {
                "type": "string",
                "description": "触发入场的情绪强度阈值，0~1，越高越保守",
            },
            "sources": {
                "type": "array", "items": {"type": "string"},
                "description": "情绪信号来源，如 twitter / news_wire",
            },
            "max_position_pct": {
                "type": "string", "description": "单次入场占策略额度的比例上限，0~1",
            },
        },
    ),
)

# 顺序即前端展示顺序：低风险在前，高风险在后不做强制，但按上线批次固定
OFFICIAL_TEMPLATES: Final[tuple[StrategyTemplate, ...]] = (
    _GRID,
    _DCA,
    _TREND,
    _MARTINGALE,
    _NEWS,
)
OFFICIAL_TEMPLATE_IDS: Final = frozenset(t.template_id for t in OFFICIAL_TEMPLATES)
_BY_ID: Final = MappingProxyType({t.template_id: t for t in OFFICIAL_TEMPLATES})


def to_plain(value: Any) -> Any:
    """把冻结的 schema 递归转成普通 dict/list。

    返回全新对象：调用方（如序列化层）拿到的副本随便改，都不会污染模板单例。
    """
    if isinstance(value, Mapping):
        return {k: to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    return value


def list_templates() -> tuple[StrategyTemplate, ...]:
    """全部官方模板，顺序固定。"""
    return OFFICIAL_TEMPLATES


def get_template(template_id: str) -> StrategyTemplate:
    template = _BY_ID.get(template_id)
    if template is None:
        raise TemplateNotFound(f"未知的官方模板：{template_id}")
    return template
