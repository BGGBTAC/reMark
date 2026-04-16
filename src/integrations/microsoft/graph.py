"""Thin async client for the Microsoft Graph API.

Handles the HTTP layer so individual feature modules (To Do, Calendar)
can focus on their endpoints. All requests go through this client
for consistent auth handling and retry behavior.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.integrations.microsoft.auth import MicrosoftAuth

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
MAX_RETRIES = 3
RETRY_BACKOFF = 2


class GraphError(Exception):
    """Raised when a Graph API call fails."""


class GraphClient:
    """Async client for Microsoft Graph."""

    def __init__(self, auth: MicrosoftAuth):
        self._auth = auth
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> GraphClient:
        self._client = httpx.AsyncClient(timeout=30)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise GraphError("Use 'async with GraphClient(auth)' as context manager")
        return self._client

    async def _headers(self) -> dict[str, str]:
        token = await self._auth.get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def get(self, endpoint: str, params: dict | None = None) -> Any:
        return await self._request("GET", endpoint, params=params)

    async def post(self, endpoint: str, body: dict | None = None) -> Any:
        return await self._request("POST", endpoint, json=body)

    async def patch(self, endpoint: str, body: dict | None = None) -> Any:
        return await self._request("PATCH", endpoint, json=body)

    async def delete(self, endpoint: str) -> None:
        await self._request("DELETE", endpoint, expect_json=False)

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict | None = None,
        json: Any = None,
        expect_json: bool = True,
    ) -> Any:
        import asyncio

        url = endpoint if endpoint.startswith("http") else f"{GRAPH_BASE}{endpoint}"

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                headers = await self._headers()
                resp = await self.client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json,
                )

                if resp.status_code == 429 or resp.status_code >= 500:
                    retry_after = int(resp.headers.get("Retry-After", RETRY_BACKOFF**attempt))
                    logger.warning(
                        "Graph %s %s -> %d, retrying in %ds",
                        method,
                        endpoint,
                        resp.status_code,
                        retry_after,
                    )
                    await asyncio.sleep(retry_after)
                    continue

                if resp.status_code >= 400:
                    try:
                        error = resp.json().get("error", {}).get("message", resp.text)
                    except Exception:
                        error = resp.text
                    raise GraphError(f"{method} {endpoint} failed: {resp.status_code} {error}")

                if not expect_json or resp.status_code == 204:
                    return None
                return resp.json()

            except httpx.TransportError as e:
                last_error = e
                await asyncio.sleep(RETRY_BACKOFF**attempt)

        raise GraphError(f"{method} {endpoint} failed after {MAX_RETRIES} retries: {last_error}")
