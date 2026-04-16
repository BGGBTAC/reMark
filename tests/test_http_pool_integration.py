"""Verify auth and SyncEngine are wired to the shared pool."""
from __future__ import annotations

import inspect

import pytest

from src.http_pool import SharedHttpPool


async def test_auth_uses_shared_pool_when_provided(monkeypatch, tmp_path):
    from src.remarkable import auth as auth_mod

    # At least get_user_token, register_device, and _refresh_user_token
    # should accept the pool so callers can opt in to connection reuse.
    sig_params = []
    for name, fn in inspect.getmembers(auth_mod.AuthManager, predicate=inspect.isfunction):
        if "http_pool" in inspect.signature(fn).parameters:
            sig_params.append(name)
    assert sig_params, "expected at least one AuthManager method to accept http_pool"


async def test_sync_engine_owns_pool():
    """SyncEngine must expose a SharedHttpPool and a close() coroutine."""
    from src.config import AppConfig
    from src.sync.engine import SyncEngine

    engine = SyncEngine(AppConfig())
    assert isinstance(engine._http_pool, SharedHttpPool)
    # close() is a coroutine and safely no-ops when pool is fresh
    await engine.close()
    await engine.close()  # idempotent


async def test_auth_pool_routes_request(monkeypatch, tmp_path):
    """When http_pool is provided, AuthManager._refresh_user_token uses it."""
    from src.remarkable.auth import AuthError, AuthManager

    posts: list[str] = []

    class _FakeClient:
        is_closed = False

        async def post(self, url, **kw):
            posts.append(url)

            class _R:
                status_code = 200
                text = "fake-user-token.eyJleHAiOjk5OTk5OTk5OTl9.sig"

            return _R()

    pool = SharedHttpPool()
    pool._client = _FakeClient()  # type: ignore[assignment]

    token_path = tmp_path / "device_token"
    token_path.write_text("fake-device-token", encoding="utf-8")

    manager = AuthManager(token_path)
    # Calling get_user_token with the pool must route through our fake client.
    token = await manager.get_user_token(http_pool=pool)
    assert token.startswith("fake-user-token")
    assert posts, "no POST was made through the shared pool"
    assert posts[0].endswith("/user/new")
