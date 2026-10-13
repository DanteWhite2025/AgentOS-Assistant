"""Core USER authentication contract tests."""

from __future__ import annotations

import httpx
import pytest

from app.auth.core_user import (
    AuthenticatedUser,
    HttpCoreUserAuthenticator,
    UserAuthenticationError,
)
from app.config import Settings
from app.container import build_container
from app.main import create_app


class _CanonicalAuth:
    async def authenticate(self, _bearer_token: str) -> AuthenticatedUser:
        return AuthenticatedUser("u_from_core", "sub_core", ("agent:session",), "tok_core")


class _UnavailableAuth:
    async def authenticate(self, _bearer_token: str) -> AuthenticatedUser:
        raise UserAuthenticationError(
            "AUTH_UPSTREAM_UNAVAILABLE", "Core 身份服务暂不可用", 503
        )


async def test_forwards_same_bearer_and_uses_canonical_core_identity() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "actor_type": "USER",
                    "user_id": "u_core",
                    "subject_id": "sub_core",
                    "scopes": ["agent:session"],
                    "token_id": "tok_core",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        user = await HttpCoreUserAuthenticator("https://core.example", client).authenticate(
            "opaque-user-token"
        )

    assert seen[0].url.path == "/api/v1/me/actor-context"
    assert seen[0].headers["Authorization"] == "Bearer opaque-user-token"
    assert user.user_id == "u_core"
    assert user.subject_id == "sub_core"
    assert user.scopes == ("agent:session",)
    assert user.token_id == "tok_core"


@pytest.mark.parametrize(
    ("core_status", "runtime_status", "code"),
    [(401, 401, "UNAUTHENTICATED"), (403, 403, "FORBIDDEN")],
)
async def test_core_auth_status_mapping(core_status: int, runtime_status: int, code: str) -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(core_status))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(UserAuthenticationError) as caught:
            await HttpCoreUserAuthenticator("https://core.example", client).authenticate("bad")
    assert caught.value.status_code == runtime_status
    assert caught.value.code == code


async def test_core_unavailable_maps_to_503() -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        with pytest.raises(UserAuthenticationError) as caught:
            await HttpCoreUserAuthenticator("https://core.example", client).authenticate("token")
    assert caught.value.status_code == 503
    assert caught.value.code == "AUTH_UPSTREAM_UNAVAILABLE"


async def test_invalid_core_actor_context_maps_to_503() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"data": {}}))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(UserAuthenticationError) as caught:
            await HttpCoreUserAuthenticator("https://core.example", client).authenticate("token")
    assert caught.value.code == "AUTH_UPSTREAM_UNAVAILABLE"


async def test_session_owner_is_canonical_core_user_not_client_header() -> None:
    container = build_container(Settings(provider_mode="fake"))
    container.user_authenticator = _CanonicalAuth()
    app = create_app()
    app.state.container = container
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/agent/sessions",
            json={"modality": "text"},
            headers={"Authorization": "Bearer opaque", "X-User-ID": "spoofed"},
        )
    assert response.status_code == 201
    session = await container.session_store.get(response.json()["session_id"])
    assert session is not None
    assert session.user_id == "u_from_core"


async def test_core_unavailable_is_runtime_503_response() -> None:
    container = build_container(Settings(provider_mode="fake"))
    container.user_authenticator = _UnavailableAuth()
    app = create_app()
    app.state.container = container
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/agent/sessions",
            json={"modality": "text"},
            headers={"Authorization": "Bearer opaque"},
        )
    assert response.status_code == 503
    assert response.json()["code"] == "AUTH_UPSTREAM_UNAVAILABLE"
