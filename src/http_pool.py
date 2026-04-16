"""Module-level shared httpx.AsyncClient for short one-off integrations.

SyncEngine owns the pool and passes it to subsystems that do per-request
work (auth refresh, Teams webhooks, etc.). Keeping one keep-alive client
instead of spawning per-request clients removes the 100-200 ms TLS
handshake on every call.

Long-lived protocol clients (RemarkableCloud, Notion) already manage
their own pools and should NOT switch to this one — they have
provider-specific interceptors and lifecycles.
"""
from __future__ import annotations

import httpx


class SharedHttpPool:
    """Lazy singleton httpx.AsyncClient with sensible defaults."""

    def __init__(
        self,
        timeout_seconds: float = 30.0,
        http2: bool = False,
    ):
        self._client: httpx.AsyncClient | None = None
        self._timeout = timeout_seconds
        self._http2 = http2

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                http2=self._http2,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
