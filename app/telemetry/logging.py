"""结构化日志初始化（MVP：stdlib logging；生产可换结构化 JSON handler）。"""

from __future__ import annotations

import logging
import re

_CONFIGURED = False

_SECRET_PATTERNS = (
    re.compile(r"(?i)Bearer\s+[^\s,;]+"),
    re.compile(r"\b(?:sk|ek)[-_][A-Za-z0-9_-]{6,}\b"),
    # Followin 的 key 是 32 字节无前缀 hex，走 x-api-key header，
    # 上面两条一条都匹配不到。按「键名 + 分隔符 + 值」整体替换。
    re.compile(r"(?i)(x[-_]api[-_]key|FOLLOWIN_API_KEY)([\"'\s:=]+)[A-Za-z0-9]{16,}"),
)


def redact_secrets(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


class SecretRedactionFilter(logging.Filter):
    """Last-line defense against bearer/API/ephemeral secrets in logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        rendered = redact_secrets(record.getMessage())
        record.msg = rendered
        record.args = ()
        return True


def setup_logging(level: int = logging.INFO) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s trace_id=%(otelTraceID)s %(message)s"
        if False  # OTel logging instrumentation 接入后启用 trace 关联
        else "%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(SecretRedactionFilter())
    _CONFIGURED = True
