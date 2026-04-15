"""Web-UI auth helpers for v0.7's multi-user support.

Passwords are hashed with bcrypt via ``passlib`` (never persisted in
plaintext, never logged, never sent back to the browser). Sessions are
signed cookies managed by Starlette's ``SessionMiddleware`` — we store
``user_id`` and ``username`` only, so session invalidation equals
deactivating the user row.

Admin bootstrap: on first boot after the 0.7 upgrade the state DB has
no ``users`` rows. :func:`bootstrap_admin` seeds an ``admin`` account
with the password provided via the ``REMARK_ADMIN_PASSWORD`` env var
(or a random one printed once to the log). That's the only path where
plaintext touches the process — after that everything lives as a
bcrypt hash.
"""

from __future__ import annotations

import logging
import os
import secrets as _secrets
from typing import Any

from fastapi import HTTPException, Request, status

from src.sync.state import SyncState

logger = logging.getLogger(__name__)


def _context():
    from passlib.context import CryptContext

    return CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _context().hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _context().verify(plain, hashed)
    except Exception:
        # Defensive — passlib raises on malformed hashes; callers treat
        # that as "authentication failed" rather than surface the error.
        return False


def bootstrap_admin(state: SyncState) -> str | None:
    """Ensure there is at least one user on first boot.

    Returns the plaintext admin password if a new user was created
    (so the caller can log it once), ``None`` if users already exist.
    """
    env_pw = os.environ.get("REMARK_ADMIN_PASSWORD", "").strip()
    plain = env_pw or _secrets.token_urlsafe(24)
    created = state.ensure_default_admin(hash_password(plain))
    if created is None:
        return None
    if env_pw:
        logger.info(
            "Bootstrapped admin user from REMARK_ADMIN_PASSWORD "
            "(user_id=%d)", created,
        )
        return None  # operator already knows the password
    logger.warning(
        "No users configured — created admin with a random password. "
        "Copy it now; it will NOT be shown again.",
    )
    print(f"\n  === Admin bootstrap ===\n  username: admin\n  password: {plain}\n")
    return plain


def authenticate(
    state: SyncState, username: str, password: str,
) -> dict[str, Any] | None:
    """Verify credentials against the users table.

    Returns the matching user dict on success, ``None`` otherwise.
    Touches ``last_login_at`` on a successful match.
    """
    if not username or not password:
        return None
    user = state.get_user(username)
    if not user or not user.get("active"):
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    state.touch_user_login(int(user["id"]))
    return user


def current_user(request: Request) -> dict[str, Any] | None:
    """Resolve the session cookie to a user dict, or ``None``."""
    user_id = request.session.get("user_id") if hasattr(request, "session") else None
    if user_id is None:
        return None
    state = request.app.state.sync_state
    return state.get_user_by_id(int(user_id))


def require_user(request: Request) -> dict[str, Any]:
    """FastAPI dependency — 401 (or redirect) if no session."""
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"Location": "/login"},
        )
    return user


def require_admin(request: Request) -> dict[str, Any]:
    user = require_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin required")
    return user


__all__ = [
    "hash_password", "verify_password", "authenticate",
    "bootstrap_admin", "current_user", "require_user", "require_admin",
]
