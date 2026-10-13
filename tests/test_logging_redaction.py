"""日志脱敏规则测试。

Followin 的 key 是 32 字节无前缀 hex，走 x-api-key header 而非 Bearer；
仓库原有两条规则（Bearer / sk-ek 前缀）一条都匹配不到它。
"""

from __future__ import annotations

from app.telemetry.logging import redact_secrets

# 合成占位值，与任何真实 API key 无关 —— 仅用于测试脱敏规则
_FAKE_KEY = "deadbeefdeadbeefdeadbeefdeadbeef"


def test_redacts_x_api_key_header_value() -> None:
    line = f"POST https://mcp.followin.io/v2/mcp headers={{'x-api-key': '{_FAKE_KEY}'}}"
    assert _FAKE_KEY not in redact_secrets(line)


def test_redacts_x_api_key_in_env_style_assignment() -> None:
    assert _FAKE_KEY not in redact_secrets(f"AGENT_FOLLOWIN_API_KEY={_FAKE_KEY}")


def test_keeps_existing_bearer_rule_working() -> None:
    assert "abc123def456" not in redact_secrets("Authorization: Bearer abc123def456")


def test_leaves_ordinary_text_untouched() -> None:
    assert redact_secrets("news collected count=50") == "news collected count=50"
