"""响应组装 —— 把结构化结果转成适合语音/文本的回答 + 数字人 emotion/state 标签。

当前状态：MVP 简化，文本回复由 AgentRunner 直接返回。
扩展点：response 组装时附带轻量 emotion/state 标签（见接口 ws-events.md 的 avatar.event）。
"""

from __future__ import annotations
