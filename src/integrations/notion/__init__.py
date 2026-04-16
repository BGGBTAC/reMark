"""Notion integration — mirror reMarkable notes into a Notion workspace.

The pairing is deliberately minimal:

* Authentication via a single **integration token** (internal
  integration, not OAuth). The user creates the integration at
  https://www.notion.so/my-integrations, shares the target parent
  page with it, then drops the ``secret_...`` token into the
  ``NOTION_TOKEN`` environment variable.
* Page writes go under a single ``vault_mirror_page_id``; each synced
  note becomes a child page with a handful of block types mapped from
  Markdown (heading, paragraph, bulleted list, to-do).
* Task pull is deferred to a future release — the client has a
  ``list_database_rows`` helper that makes it easy to wire later.

This module mirrors ``src.integrations.microsoft`` so the engine can
pick it up with the same pattern.
"""

from src.integrations.notion.client import NotionClient, NotionError
from src.integrations.notion.service import NotionService

__all__ = ["NotionClient", "NotionError", "NotionService"]
