"""Microsoft Graph authentication via MSAL device code flow.

Uses the same UX pattern as the reMarkable device auth: user opens
a URL on another device, enters a code, grants consent. The access
and refresh tokens are cached to disk for subsequent runs.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import msal

logger = logging.getLogger(__name__)

# Scopes needed for To Do + Calendar
DEFAULT_SCOPES = [
    "Tasks.ReadWrite",
    "Calendars.ReadWrite",
    "User.Read",
]


class MicrosoftAuthError(Exception):
    """Raised when Microsoft auth fails."""


class MicrosoftAuth:
    """Manages Microsoft Graph access tokens via MSAL device code flow."""

    def __init__(
        self,
        client_id: str,
        tenant: str = "common",
        token_cache_path: str | Path = "~/.remark-bridge/msal_cache.bin",
        scopes: list[str] | None = None,
    ):
        if not client_id:
            raise MicrosoftAuthError(
                "Microsoft client_id is required. Register an app at "
                "https://entra.microsoft.com and set microsoft.client_id in config.yaml."
            )
        self._client_id = client_id
        self._authority = f"https://login.microsoftonline.com/{tenant}"
        self._cache_path = Path(token_cache_path).expanduser()
        self._scopes = scopes or DEFAULT_SCOPES
        self._cache = msal.SerializableTokenCache()
        self._app: msal.PublicClientApplication | None = None

        self._load_cache()

    def _load_cache(self) -> None:
        if self._cache_path.exists():
            try:
                self._cache.deserialize(self._cache_path.read_text())
            except Exception as e:
                logger.warning("Failed to load MSAL cache: %s", e)

    def _save_cache(self) -> None:
        if self._cache.has_state_changed:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(self._cache.serialize())
            os.chmod(self._cache_path, 0o600)

    @property
    def app(self) -> msal.PublicClientApplication:
        if self._app is None:
            self._app = msal.PublicClientApplication(
                client_id=self._client_id,
                authority=self._authority,
                token_cache=self._cache,
            )
        return self._app

    def has_cached_token(self) -> bool:
        """Check if there's a cached account we can silently refresh from."""
        try:
            accounts = self.app.get_accounts()
            return len(accounts) > 0
        except Exception:
            return False

    async def get_access_token(self) -> str:
        """Return a valid access token, using cached refresh token if possible."""
        # MSAL is sync; run the relevant calls. They're fast.
        accounts = self.app.get_accounts()

        if accounts:
            result = self.app.acquire_token_silent(
                scopes=self._scopes,
                account=accounts[0],
            )
            if result and "access_token" in result:
                self._save_cache()
                return result["access_token"]
            logger.info("Silent token acquisition failed, device flow required")

        raise MicrosoftAuthError(
            "No valid Microsoft token cached. Run `remark-bridge setup-microsoft` first."
        )

    def start_device_flow(self) -> dict:
        """Initiate the device code flow.

        Returns the flow dict, which contains the user code and verification URL.
        The caller should display these to the user, then call complete_device_flow().
        """
        flow = self.app.initiate_device_flow(scopes=self._scopes)
        if "user_code" not in flow:
            raise MicrosoftAuthError(f"Failed to start device flow: {json.dumps(flow, indent=2)}")
        return flow

    def complete_device_flow(self, flow: dict) -> dict:
        """Poll for device flow completion. Blocks until the user authorizes or times out.

        Returns the token result, or raises MicrosoftAuthError on failure.
        """
        result = self.app.acquire_token_by_device_flow(flow)

        if "access_token" not in result:
            error = result.get("error_description", result.get("error", "unknown"))
            raise MicrosoftAuthError(f"Device flow failed: {error}")

        self._save_cache()
        return result
