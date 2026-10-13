"""工作流 —— 显式状态机编排多步/多 agent 流程。

当前状态：MVP 未实现（自研状态机逻辑在 AgentRunner 单轮循环，够用）。
扩展点：跨 agent handoff、长流程、需持久化/断点续跑时实现
（届时评估 LangGraph，见 docs/research/tech-stack-assessment.md §2）。
"""

from __future__ import annotations
