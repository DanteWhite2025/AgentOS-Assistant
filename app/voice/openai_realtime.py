"""OpenAI Realtime 实时会话（服务端代理音频）—— 方案 A 用。

当前状态：**不需要**。采用方案 B（移动端直连 OpenAI，服务端只签发 client_secret，
见 app/voice/provider.py 与 docs/api/agent-mobile/voice-webrtc.md）。
扩展点：若未来切方案 A（服务端代理音频，做实时监控/录音审计），在此实现 WS 音频代理。
"""

from __future__ import annotations
