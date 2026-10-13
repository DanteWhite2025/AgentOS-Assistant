"""回合阶段的展示文案。

沿用 tool.invoked 那套约定（见 app/tools/registry.py 的 describe_tool_call）：
服务端渲染好中文一起下发，同时给一个 i18n key —— 客户端有自己的翻译就用自己
的，没有就回落到 title。这样后端新增阶段时客户端不发版也不会显示成空白。

阶段名是对外契约（客户端拿它拼 key），文案不是；文案随时可以改。
"""

from __future__ import annotations

from app.agents.runner import STAGE_COMPOSING, STAGE_THINKING

# 未知阶段的兜底。runner 将来加了新阶段而这张表没跟上时，宁可显示一句通用
# 文案，也不能让那一行变成空白或者让整轮挂掉。
_FALLBACK_TITLE = "正在处理"

_STAGE_TITLES = {
    STAGE_THINKING: "正在思考",
    STAGE_COMPOSING: "正在整理结果",
}


def describe_stage(stage: str) -> dict[str, str]:
    """渲染一个阶段的展示字段。"""

    return {
        "stage": stage,
        "title": _STAGE_TITLES.get(stage, _FALLBACK_TITLE),
        "title_key": f"stage.{stage}",
    }
