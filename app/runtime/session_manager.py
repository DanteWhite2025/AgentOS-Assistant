"""会话管理 —— 会话生命周期（创建/查询/更新/关闭）的编排入口。

当前状态：MVP 已实现，能力分布在：
  - app/runtime/session.py         会话领域模型（不可变）
  - app/runtime/session_store.py   存储接口 + 内存实现
  - app/runtime/redis_session_store.py  Redis 实现
  - app/runtime/tokens.py          rt_token 签发/校验
  - app/api/agent.py               端点里编排上述组件
扩展点：当会话编排逻辑变复杂（并发会话、迁移、跨设备）时，收敛到此模块统一管理。
"""

from __future__ import annotations
