"""工具注册表 —— 受控工具的唯一来源，含服务端白名单二次校验。

安全红线（见接口文档 voice-webrtc.md §2.2 / openapi /tool-calls）：
工具旁挂请求到达时，必须以会话 scope.tools 二次强校验工具名，
绝不信任客户端上报的名字。本模块的 is_allowed 是该校验的落点。

不可变：注册表构建后只读；with_tool 返回新实例，不原地修改。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Tool:
    """一个受控工具。

    write=True 表示受控写工具（只允许创建 TradeDraft，不执行真实交易）。
    resource：只读工具对应的 yesup-core 资源名（写工具为 None）。
    external=True 表示由外部 provider（如 AgentOsStagingClient）执行，不走 core executor。
    description：工具的功能描述，供 LLM / Function Calling 识别。
    parameters：JSON Schema 格式的参数定义。

    display_title / display_params 是给**用户**看的，与上面给模型看的那两个
    字段完全分开：description 是让模型判断该不该调用的说明书，display_title
    是「正在搜索…」那一行。两者混用会让其中一头变得难读。

    display_params 是正向白名单：只有列出来的参数才会出现在前端，工具将来新增
    内部字段（幂等键、内部游标）不会自动流出去。
    """

    name: str
    write: bool = False
    resource: str | None = None
    external: bool = False
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    display_title: str = ""
    display_params: tuple[str, ...] = ()

    def to_function_spec(self) -> dict[str, Any]:
        """转化为标准 OpenAI / Function Calling 格式的函数定义声明。"""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description or f"YesUP controlled tool: {self.name}",
            "parameters": self.parameters or {"type": "object", "properties": {}},
        }


# 展示用的一行字，不是完整内容。查询词可能很长，前端那一行放不下也不该放。
_MAX_DISPLAY_VALUE_LENGTH = 120

# 这些字符本身表达「断开」，直接删掉会把两个词粘成一个（"BTC\nETH" → "BTCETH"），
# 看起来像一个新标的。换成空格既保留了分隔语义，又不会破坏单行展示。
_WHITESPACE_LIKE = frozenset({"\t", "\n", "\r", "\v", "\f", " ", " "})

# Cc 控制字符 / Cf 格式字符（零宽、双向覆盖、Unicode Tags 区）/ Zl、Zp 行段分隔。
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _clean_display_char(ch: str) -> str:
    if ch in _WHITESPACE_LIKE:
        return " "
    if unicodedata.category(ch) in _INVISIBLE_CATEGORIES:
        return ""
    return ch


def _sanitize_display_value(value: Any) -> str:
    """把参数值渲染成可安全展示的一行文本。

    展示即复核：交易草案的构造参数会展示给用户，而最终签名权在用户手上，
    这一行渲染出来的东西就是他据以确认的依据。所以必须清掉所有能让「看到的」
    和「签下的」不一致的字符——双向覆盖符（U+202E 能把 "0.1BTC" 显示成反的）、
    零宽字符、Unicode Tags 区（完全不可见，专门用来藏内容）、控制字符。
    这些在 Unicode 里都属于 Cf（格式）或 Cc（控制）类别，一并按类别剔除，
    而不是维护一张永远会漏的黑名单。
    """

    if isinstance(value, (list, tuple)):
        text = ", ".join(str(item) for item in value)
    else:
        text = str(value)
    cleaned = "".join(_clean_display_char(ch) for ch in text)
    if len(cleaned) > _MAX_DISPLAY_VALUE_LENGTH:
        return cleaned[:_MAX_DISPLAY_VALUE_LENGTH] + "…"
    return cleaned


def describe_tool_call(tool: Tool, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """渲染一次工具调用的展示信息，供 WS/REST 下发给客户端。

    同时给出 ``title``（服务端的中文默认串）和 ``title_key``（i18n key）：
    客户端有对应翻译就用自己的，没有就回落到 title。新增工具时客户端不发版
    也能正确显示，避免「后端合了、卡在移动端发包」那类空窗。
    """

    return {
        "title": tool.display_title or tool.name,
        "title_key": f"tool.{tool.name}.running",
        "params": {
            key: _sanitize_display_value(arguments[key])
            for key in tool.display_params
            if key in arguments
        },
    }


class ToolRegistry:
    """不可变工具注册表。"""

    def __init__(self, tools: Mapping[str, Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = dict(tools or {})

    def with_tool(self, tool: Tool) -> ToolRegistry:
        """返回追加了 tool 的新注册表（原实例不变）。"""
        return ToolRegistry({**self._tools, tool.name: tool})

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def all_tools(self) -> tuple[Tool, ...]:
        return tuple(self._tools.values())

    def get_function_specs(
        self, scope_tools: list[str] | tuple[str, ...] | None = None
    ) -> list[dict[str, Any]]:
        """获取指定白名单（或全部）工具的 Function Calling 规范。"""
        candidates: list[Tool] = list(self._tools.values())
        if scope_tools is not None:
            allowed = set(scope_tools)
            candidates = [t for t in candidates if t.name in allowed]
        return [t.to_function_spec() for t in candidates]

    def is_allowed(self, name: str, scope_tools: list[str]) -> bool:
        """服务端二次校验：既要在注册表登记，也要在会话 scope 白名单内。"""
        return name in self._tools and name in set(scope_tools)
