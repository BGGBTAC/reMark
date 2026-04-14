"""Thin async httpx wrapper around the Notion REST API.

We don't pull in ``notion-client`` because the official package is
sync and couples to its own models — a 70-line httpx client is
sufficient for the calls we need.

References:
- https://developers.notion.com/reference/post-page
- https://developers.notion.com/reference/patch-block-children
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2022-06-28"


class NotionError(Exception):
    """Raised when the Notion API returns a non-success response."""


class NotionClient:
    """Minimal async Notion API client."""

    def __init__(self, token: str, timeout: float = 30.0):
        if not token:
            raise ValueError("Notion integration token is empty")
        self._token = token
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_API_VERSION,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
    ) -> dict[str, Any]:
        url = f"{NOTION_API_BASE}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.request(
                method, url, headers=self._headers(), json=json_body,
            )
        if resp.status_code >= 400:
            raise NotionError(
                f"Notion {method} {path} → HTTP {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        return resp.json()

    async def create_page(
        self,
        parent_page_id: str,
        title: str,
        blocks: list[dict],
    ) -> str:
        """Create a child page under ``parent_page_id`` with the given blocks.

        Returns the new page id.
        """
        body = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {
                "title": {
                    "title": [{"type": "text", "text": {"content": title}}],
                },
            },
            "children": blocks[:100],  # Notion caps children in one create
        }
        result = await self._request("POST", "/pages", body)
        page_id = result.get("id", "")
        # Notion caps `children` at 100 per create call; append the rest.
        if len(blocks) > 100:
            await self.append_blocks(page_id, blocks[100:])
        return page_id

    async def append_blocks(
        self, block_id: str, blocks: list[dict],
    ) -> None:
        """Append blocks to an existing block/page, paging in 100s."""
        for i in range(0, len(blocks), 100):
            chunk = blocks[i : i + 100]
            await self._request(
                "PATCH",
                f"/blocks/{block_id}/children",
                {"children": chunk},
            )

    async def list_database_rows(
        self,
        database_id: str,
        filter_: dict | None = None,
        page_size: int = 50,
    ) -> list[dict]:
        """Query a database. Returns the raw ``results`` list."""
        body: dict[str, Any] = {"page_size": page_size}
        if filter_:
            body["filter"] = filter_
        result = await self._request(
            "POST", f"/databases/{database_id}/query", body,
        )
        return result.get("results", [])
