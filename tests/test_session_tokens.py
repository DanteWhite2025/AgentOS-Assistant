"""会话存储与 rt_token 测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.runtime.session import Scope, Session
from app.runtime.session_store import InMemorySessionStore
from app.runtime.tokens import TokenService


def _now() -> datetime:
    return datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)


def _session() -> Session:
    return Session(
        session_id="sess_1",
        user_id="u1",
        modality="both",
        scope=Scope(agents=("avatar",), tools=("get_positions",)),
        state="created",
        created_at=_now(),
        last_activity_at=_now(),
    )


async def test_session_store_crud() -> None:
    store = InMemorySessionStore()
    await store.create(_session())
    got = await store.get("sess_1")
    assert got is not None and got.user_id == "u1"
    await store.update(got.touch(_now(), state="text_active"))
    assert (await store.get("sess_1")).state == "text_active"
    await store.delete("sess_1")
    assert await store.get("sess_1") is None


def test_token_roundtrip() -> None:
    svc = TokenService("secret", ttl_seconds=300)
    now = _now()
    token, exp = svc.issue_rt_token("sess_1", "u1", ("get_positions",), now)
    claims = svc.verify_rt_token(token, now)
    assert claims is not None
    assert claims.session_id == "sess_1"
    assert claims.tools == ("get_positions",)
    assert claims.expires_at == exp


def test_token_expired() -> None:
    svc = TokenService("secret", ttl_seconds=300)
    now = _now()
    token, _ = svc.issue_rt_token("sess_1", "u1", (), now)
    assert svc.verify_rt_token(token, now + timedelta(seconds=301)) is None


def test_token_tampered() -> None:
    svc = TokenService("secret")
    now = _now()
    token, _ = svc.issue_rt_token("s", "u", (), now)
    tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    assert svc.verify_rt_token(tampered, now) is None
