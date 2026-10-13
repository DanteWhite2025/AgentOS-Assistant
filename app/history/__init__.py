"""历史对话的持久化层。

与 `app/runtime/session.py` 的分工：Session 是运行时对象（Redis，TTL 1 小时，
带语音遥测）；这里是持久化记录（保留 30 天），两者生命周期不同，故分开建模。

对外 API 一律称 `conversation_id`，对内保留 `session_id` —— 取值相同，但
客户端拿到的是历史记录，不该以为自己握着一个可用的运行时会话。
"""
