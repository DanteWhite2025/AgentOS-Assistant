"""Authenticate Mobile USER bearer tokens against AgentOS Core.

The Runtime deliberately does not parse Core tokens or possess Core's signing
secret.  The raw bearer token exists only for the duration of the upstream
``actor-context`` request and is never persisted or logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import httpx

AuthErrorCode = Literal["UNAUTHENTICATED", "FORBIDDEN", "AUTH_UPSTREAM_UNAVAILABLE"]


@dataclass(frozen=True)
class AuthenticatedUser:
    """Canonical USER identity returned by Core."""

    user_id: str
    subject_id: str
    scopes: tuple[str, ...]
    token_id: str


class UserAuthenticationError(Exception):
    """Safe authentication failure that can be mapped to the public envelope."""

    def __init__(self, code: AuthErrorCode, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class CoreUserAuthenticator(Protocol):
    async def authenticate(self, bearer_token: str) -> AuthenticatedUser: ...


class HttpCoreUserAuthenticator:
    """Validate a USER token by forwarding it unchanged to Core."""

    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self._url = f"{base_url.rstrip('/')}/api/v1/me/actor-context"
        self._client = client

    async def authenticate(self, bearer_token: str) -> AuthenticatedUser:
        try:
            response = await self._client.get(
                self._url,
                headers={"Authorization": f"Bearer {bearer_token}"},
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            raise UserAuthenticationError(
                "AUTH_UPSTREAM_UNAVAILABLE", "Core 身份服务暂不可用", 503
            ) from None

        if response.status_code == 401:
            raise UserAuthenticationError("UNAUTHENTICATED", "USER Token 无效或已过期", 401)
        if response.status_code == 403:
            raise UserAuthenticationError("FORBIDDEN", "该 Token 不允许访问 Runtime", 403)
        if response.status_code >= 500:
            raise UserAuthenticationError(
                "AUTH_UPSTREAM_UNAVAILABLE", "Core 身份服务暂不可用", 503
            )
        if response.status_code != 200:
            raise UserAuthenticationError("UNAUTHENTICATED", "USER Token 验证失败", 401)

        try:
            payload = response.json()
            data = payload.get("data", payload)
            actor_type = data.get("actor_type", "USER")
            user_id = str(data["user_id"]).strip()
            subject_id = str(data["subject_id"]).strip()
            token_id = str(data["token_id"]).strip()
            scopes_value = data.get("scopes", [])
            if actor_type != "USER" or not isinstance(scopes_value, list):
                raise ValueError("not a USER actor context")
            scopes = tuple(str(scope) for scope in scopes_value if str(scope))
            if not user_id or not subject_id or not token_id:
                raise ValueError("incomplete actor context")
        except (TypeError, ValueError, KeyError):
            raise UserAuthenticationError(
                "AUTH_UPSTREAM_UNAVAILABLE", "Core 身份响应格式无效", 503
            ) from None

        return AuthenticatedUser(
            user_id=user_id,
            subject_id=subject_id,
            scopes=scopes,
            token_id=token_id,
        )


class FakeCoreUserAuthenticator:
    """Explicit offline/demo authenticator; never accepts anonymous requests.

    Tokens use ``test-user:<user_id>`` and exist only to make automated tests
    and explicitly fake local demos exercise the same ownership path.
    """

    _PREFIX = "test-user:"

    async def authenticate(self, bearer_token: str) -> AuthenticatedUser:
        if not bearer_token.startswith(self._PREFIX):
            raise UserAuthenticationError("UNAUTHENTICATED", "USER Token 无效或已过期", 401)
        user_id = bearer_token.removeprefix(self._PREFIX).strip()
        if not user_id:
            raise UserAuthenticationError("UNAUTHENTICATED", "USER Token 无效或已过期", 401)
        return AuthenticatedUser(
            user_id=user_id,
            subject_id=user_id,
            scopes=(),
            token_id=f"test-token:{user_id}",
        )
