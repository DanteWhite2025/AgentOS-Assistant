"""发给模型的消息结构检查。

这些不变量与用哪个模型、用什么提示词都无关，坏掉就一定是 bug：

1. **交替**：ModelRequest 与 ModelResponse 交替出现，不能连着两条 ModelRequest
   （落到 OpenAI wire 上就是连续两条 role:"user"，部分兼容网关会直接 400）。
2. **配对**：每个 ToolCallPart 都有一个 tool_call_id 相同的 ToolReturnPart，
   反之亦然。孤儿任一侧都会让 provider 拒绝整个请求。
3. **无伪装**：工具结果不得出现在 UserPromptPart 里。改造前正是这么干的，
   模型因此看不到「是我自己调的工具」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ShapeViolation:
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.detail}"


def describe(messages: list[Any]) -> list[str]:
    """渲染成一行一条的结构签名，便于在报告里直接对比。"""

    return [
        f"{type(m).__name__}({', '.join(type(p).__name__ for p in m.parts)})" for m in messages
    ]


def check(messages: list[Any]) -> list[ShapeViolation]:
    """返回所有结构违规。空列表表示结构合法。"""

    violations: list[ShapeViolation] = []
    calls: dict[str, str] = {}
    returns: dict[str, str] = {}
    previous_kind = ""

    for index, message in enumerate(messages):
        kind = type(message).__name__
        if kind == previous_kind:
            # 两个方向都要查：连续 ModelRequest 会变成并排的 user 消息，连续
            # ModelResponse 则是背靠背的 assistant 回合，两者在 wire 上都是无效结构。
            role = "user" if kind == "ModelRequest" else "assistant"
            violations.append(
                ShapeViolation(
                    "not-alternating",
                    f"第 {index} 条又是 {kind}，连续两条会变成并排的 {role} 消息",
                )
            )
        previous_kind = kind

        for part in message.parts:
            part_kind = type(part).__name__
            if part_kind == "ToolCallPart":
                calls[part.tool_call_id] = part.tool_name
            elif part_kind == "ToolReturnPart":
                returns[part.tool_call_id] = part.tool_name
            elif part_kind == "UserPromptPart" and "[工具结果]" in str(part.content):
                violations.append(
                    ShapeViolation(
                        "disguised-tool-result",
                        f"第 {index} 条的工具结果伪装成了用户消息",
                    )
                )

    for call_id, tool_name in calls.items():
        if call_id not in returns:
            violations.append(
                ShapeViolation("orphan-call", f"{tool_name} 的调用 {call_id} 没有对应结果")
            )
    for call_id, tool_name in returns.items():
        if call_id not in calls:
            violations.append(
                ShapeViolation("orphan-return", f"{tool_name} 的结果 {call_id} 没有对应调用")
            )

    return violations
