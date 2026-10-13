"""火山 RTC Token 生成器。

二进制格式与火山官方 rtc-aigc-demo/Server/token.js 保持一致。AppKey 只在
Runtime 内存中参与 HMAC，不会下发到客户端。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from datetime import datetime

_VERSION = "001"
_APP_ID_LENGTH = 24


def _bytes(value: bytes) -> bytes:
    return struct.pack("<H", len(value)) + value


def _text(value: str) -> bytes:
    return _bytes(value.encode())


def generate_rtc_token(
    *,
    app_id: str,
    app_key: str,
    room_id: str,
    user_id: str,
    expires_at: datetime,
    issued_at: datetime,
    nonce: int | None = None,
) -> str:
    """生成同时具备发布音视频/数据流与订阅权限的短期 RTC Token。"""

    if len(app_id) != _APP_ID_LENGTH:
        raise ValueError("Volc RTC AppId must be exactly 24 characters")
    if not app_key or not room_id or not user_id:
        raise ValueError("Volc RTC token inputs must not be empty")

    privilege_expiry = int(expires_at.timestamp())
    privileges = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    message = b"".join(
        (
            struct.pack("<I", nonce if nonce is not None else secrets.randbits(32)),
            struct.pack("<I", int(issued_at.timestamp())),
            struct.pack("<I", privilege_expiry),
            _text(room_id),
            _text(user_id),
            struct.pack("<H", len(privileges)),
            b"".join(
                struct.pack("<HI", privilege, expiry)
                for privilege, expiry in sorted(privileges.items())
            ),
        )
    )
    signature = hmac.new(app_key.encode(), message, hashlib.sha256).digest()
    content = _bytes(message) + _bytes(signature)
    return _VERSION + app_id + base64.b64encode(content).decode()
