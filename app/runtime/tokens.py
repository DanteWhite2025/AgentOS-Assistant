"""rt_token 签发与校验。

MVP 用 HMAC-SHA256 签名的紧凑 token（payload.signature），语义等价 JWT，
无新依赖；后续可平滑换成标准 JWT（PyJWT）。时间以参数传入，便于测试与可复现。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class RtTokenClaims:
    session_id: str
    user_id: str
    tools: tuple[str, ...]
    expires_at: datetime


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class TokenService:
    def __init__(self, secret: str, ttl_seconds: int = 300) -> None:
        self._secret = secret.encode()
        self._ttl = ttl_seconds

    def _sign(self, payload_b64: str) -> str:
        digest = hmac.new(self._secret, payload_b64.encode(), hashlib.sha256).digest()
        return _b64e(digest)

    def issue_rt_token(
        self,
        session_id: str,
        user_id: str,
        tools: tuple[str, ...],
        now: datetime,
    ) -> tuple[str, datetime]:
        expires_at = now + timedelta(seconds=self._ttl)
        payload = {
            "sid": session_id,
            "uid": user_id,
            "tools": list(tools),
            "exp": int(expires_at.timestamp()),
        }
        payload_b64 = _b64e(json.dumps(payload, separators=(",", ":")).encode())
        token = f"{payload_b64}.{self._sign(payload_b64)}"
        return token, expires_at

    def verify_rt_token(self, token: str, now: datetime) -> RtTokenClaims | None:
        try:
            payload_b64, signature = token.split(".", 1)
            if not hmac.compare_digest(signature, self._sign(payload_b64)):
                return None
            payload = json.loads(_b64d(payload_b64))
            expires_at = datetime.fromtimestamp(payload["exp"], tz=UTC)
            session_id = payload["sid"]
            user_id = payload["uid"]
            tools = payload["tools"]
            if (
                not isinstance(session_id, str)
                or not isinstance(user_id, str)
                or not isinstance(tools, list)
            ):
                return None
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None
        if now >= expires_at:
            return None
        return RtTokenClaims(
            session_id=session_id,
            user_id=user_id,
            tools=tuple(str(tool) for tool in tools),
            expires_at=expires_at,
        )
