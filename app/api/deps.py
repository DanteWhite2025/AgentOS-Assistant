"""FastAPI 依赖。

get_container 惰性构造：lifespan 已构造则直接用；测试用 ASGITransport（不触发
lifespan）时首次调用构造并缓存到 app.state，保证同一 app 跨请求共享内存 store。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from app.auth.core_user import AuthenticatedUser, UserAuthenticationError
from app.config import get_settings
from app.container import Container, build_container
from app.runtime.session import Session


def get_container(request: Request) -> Container:
    state = request.app.state
    container = getattr(state, "container", None)
    if container is None:
        container = build_container(get_settings())
        state.container = container
    return container


def bearer_token(authorization: str | None) -> str | None:
    """Extract a bearer token without decoding, logging, or persisting it."""

    if not authorization:
        return None
    scheme, separator, value = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


async def get_authenticated_user(
    container: Annotated[Container, Depends(get_container)],
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> AuthenticatedUser:
    token = bearer_token(authorization)
    if token is None:
        raise UserAuthenticationError("UNAUTHENTICATED", "缺少有效的 USER Bearer Token", 401)
    return await container.user_authenticator.authenticate(token)


async def owned_session(
    session_id: str, user: AuthenticatedUser, container: Container
) -> Session | None:
    """Return only a session owned by this canonical Core USER.

    Missing and cross-user sessions intentionally collapse to the same 404 to
    avoid leaking identifiers across users.
    """

    session = await container.session_store.get(session_id)
    if session is None or session.user_id != user.user_id:
        return None
    return session
