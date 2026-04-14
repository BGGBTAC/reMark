"""reMarkable Cloud authentication.

Handles the JWT-based auth flow:
1. Register device with a one-time code → get long-lived device token
2. Exchange device token for short-lived user token (~24h)
3. Auto-refresh user token when expired
"""

from __future__ import annotations

import json
import logging
import os
import time
from base64 import b64decode
from pathlib import Path
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)

AUTH_BASE = "https://webapp-production-dot-remarkable-production.appspot.com"
DEVICE_TOKEN_ENDPOINT = f"{AUTH_BASE}/token/json/2/device/new"
USER_TOKEN_ENDPOINT = f"{AUTH_BASE}/token/json/2/user/new"


class AuthError(Exception):
    """Raised when authentication fails."""


class AuthManager:
    """Manages reMarkable Cloud authentication tokens.

    Device tokens are long-lived and stored on disk.
    User tokens expire after ~24h and are refreshed automatically.
    """

    def __init__(self, device_token_path: str | Path):
        self._device_token_path = Path(device_token_path).expanduser()
        self._device_token: str | None = None
        self._user_token: str | None = None
        self._user_token_expiry: float = 0

    @property
    def device_token(self) -> str:
        if self._device_token is None:
            self._device_token = self._load_device_token()
        return self._device_token

    async def get_user_token(self) -> str:
        """Return a valid user token, refreshing if expired."""
        if self._user_token and time.time() < self._user_token_expiry - 300:
            return self._user_token

        logger.debug("User token expired or missing, refreshing...")
        self._user_token = await self._refresh_user_token()
        self._user_token_expiry = _parse_jwt_expiry(self._user_token)
        return self._user_token

    async def register_device(self, code: str) -> str:
        """Register a new device with a one-time code from my.remarkable.com.

        Returns the device token and saves it to disk.
        """
        device_id = str(uuid4())
        payload = {
            "code": code,
            "deviceDesc": "desktop-linux",
            "deviceID": device_id,
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                DEVICE_TOKEN_ENDPOINT,
                json=payload,
                timeout=30,
            )

        if resp.status_code != 200:
            raise AuthError(
                f"Device registration failed (HTTP {resp.status_code}): {resp.text}"
            )

        token = resp.text.strip()
        if not token:
            raise AuthError("Empty device token received")

        self._save_device_token(token)
        self._device_token = token
        logger.info("Device registered successfully (ID: %s...)", device_id[:8])
        return token

    async def _refresh_user_token(self) -> str:
        """Exchange device token for a fresh user token."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                USER_TOKEN_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self.device_token}",
                    "Content-Length": "0",
                },
                content=b"",
                timeout=30,
            )

        if resp.status_code != 200:
            raise AuthError(
                f"User token refresh failed (HTTP {resp.status_code}). "
                "Device token may be revoked — try re-registering."
            )

        token = resp.text.strip()
        if not token:
            raise AuthError("Empty user token received")

        logger.debug("User token refreshed, expires at %s", self._user_token_expiry)
        return token

    def _load_device_token(self) -> str:
        """Load device token from disk."""
        if not self._device_token_path.exists():
            raise AuthError(
                f"No device token found at {self._device_token_path}. "
                "Run `remark-bridge setup` to register your device."
            )

        token = self._device_token_path.read_text().strip()
        if not token:
            raise AuthError(f"Device token file is empty: {self._device_token_path}")

        logger.debug("Loaded device token from %s", self._device_token_path)
        return token

    def _save_device_token(self, token: str) -> None:
        """Save device token to disk with restrictive permissions."""
        self._device_token_path.parent.mkdir(parents=True, exist_ok=True)
        self._device_token_path.write_text(token)
        # chmod 600 — owner read/write only
        os.chmod(self._device_token_path, 0o600)
        logger.info("Device token saved to %s", self._device_token_path)

    def has_device_token(self) -> bool:
        """Check if a device token exists on disk."""
        return self._device_token_path.exists()


def device_token_path_for(
    device_id: str, base_dir: str | Path = "~/.remark-bridge",
) -> Path:
    """Return the conventional token path for a named device.

    Single-device ("default") installs keep using the flat
    ``<base>/device_token`` file so nothing breaks on upgrade. Named
    devices get their own directory under ``<base>/devices/<id>/`` so
    multiple tablets can be registered side-by-side.
    """
    base = Path(base_dir).expanduser()
    if device_id == "default":
        return base / "device_token"
    return base / "devices" / device_id / "device_token"


def _parse_jwt_expiry(token: str) -> float:
    """Extract the 'exp' claim from a JWT without verifying signature.

    We don't need to verify — we're the legitimate client and the server
    will reject expired tokens anyway. We just need to know when to refresh.
    """
    try:
        payload_b64 = token.split(".")[1]
        # JWT base64url encoding: pad to multiple of 4
        padding = 4 - len(payload_b64) % 4
        if padding != 4:
            payload_b64 += "=" * padding
        payload = json.loads(b64decode(payload_b64))
        return float(payload.get("exp", 0))
    except (IndexError, ValueError, json.JSONDecodeError):
        logger.warning("Couldn't parse JWT expiry, assuming expired")
        return 0
