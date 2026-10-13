"""语音字幕处理/归档。

当前状态：字幕归档由 App 异步上报到 /v1/agent/sessions/{id}/history（见接口 README §7）。
扩展点：需服务端实时 transcript 处理（关键词触发、合规过滤）时在此实现。
"""

from __future__ import annotations
