"""High-level Notion service — markdown → Notion blocks, page writes.

The block conversion is intentionally shallow: good enough that a note
lands in Notion with readable structure, without trying to be a full
Markdown renderer. Users who want parity keep their Obsidian vault.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from src.integrations.notion.client import NotionClient, NotionError

logger = logging.getLogger(__name__)


@dataclass
class NotionPushResult:
    page_id: str
    blocks_written: int


class NotionService:
    """High-level operations on a Notion workspace."""

    def __init__(self, config) -> None:
        """``config`` is a ``NotionConfig`` instance from ``src.config``."""
        self._config = config
        self._client: NotionClient | None = None

    @property
    def enabled(self) -> bool:
        if not getattr(self._config, "enabled", False):
            return False
        return bool(self._token())

    def _token(self) -> str:
        env = getattr(self._config, "integration_token_env", "NOTION_TOKEN")
        return os.environ.get(env, "")

    def _get_client(self) -> NotionClient:
        if self._client is None:
            self._client = NotionClient(self._token())
        return self._client

    async def write_note(
        self,
        title: str,
        content: str,
        tags: list[str] | None = None,
    ) -> NotionPushResult | None:
        """Create a child page under ``vault_mirror_page_id``.

        Returns ``None`` when the integration is disabled or the parent
        page is unset — callers treat that as "skip silently".
        """
        if not self.enabled:
            return None
        parent = getattr(self._config, "vault_mirror_page_id", "").strip()
        if not parent:
            logger.warning("Notion enabled but vault_mirror_page_id unset")
            return None

        blocks = markdown_to_blocks(content)
        if tags:
            # Prepend a one-line "Tags: ..." paragraph so tag context
            # stays visible in Notion without mapping frontmatter.
            blocks.insert(0, _paragraph("Tags: " + ", ".join(tags)))

        try:
            page_id = await self._get_client().create_page(
                parent_page_id=parent,
                title=title,
                blocks=blocks,
            )
        except NotionError as exc:
            logger.warning("Notion push failed for %s: %s", title, exc)
            return None

        logger.info(
            "Notion: wrote page '%s' (%d blocks) under %s",
            title,
            len(blocks),
            parent,
        )
        return NotionPushResult(page_id=page_id, blocks_written=len(blocks))


# ---------------------------------------------------------------------------
# Markdown → Notion block converter
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.+)$")
_TODO_RE = re.compile(r"^[-*]\s+\[( |x|X)\]\s+(.+)$")
_NUMBERED_RE = re.compile(r"^\d+\.\s+(.+)$")


def markdown_to_blocks(content: str) -> list[dict]:
    """Convert a Markdown string to a list of Notion block dicts.

    Supported block types: heading_1/2/3, paragraph, bulleted_list_item,
    numbered_list_item, to_do. Anything else becomes a paragraph. No
    inline formatting (bold/italic/links) — the goal is structural
    fidelity, not pixel-parity with Obsidian.
    """
    blocks: list[dict] = []
    paragraph_buffer: list[str] = []

    def _flush_paragraph() -> None:
        if paragraph_buffer:
            blocks.append(_paragraph("\n".join(paragraph_buffer)))
            paragraph_buffer.clear()

    for raw_line in content.splitlines():
        line = raw_line.rstrip()

        if not line:
            _flush_paragraph()
            continue

        # Order matters: check to-do before bullet so "- [ ]" wins.
        todo_match = _TODO_RE.match(line)
        if todo_match:
            _flush_paragraph()
            checked = todo_match.group(1).lower() == "x"
            blocks.append(_todo(todo_match.group(2), checked=checked))
            continue

        bullet_match = _BULLET_RE.match(line)
        if bullet_match:
            _flush_paragraph()
            blocks.append(_bulleted(bullet_match.group(1)))
            continue

        numbered_match = _NUMBERED_RE.match(line)
        if numbered_match:
            _flush_paragraph()
            blocks.append(_numbered(numbered_match.group(1)))
            continue

        heading_match = _HEADING_RE.match(line)
        if heading_match:
            _flush_paragraph()
            level = len(heading_match.group(1))
            blocks.append(_heading(level, heading_match.group(2)))
            continue

        paragraph_buffer.append(line)

    _flush_paragraph()

    # Notion rejects pages with no children; emit an empty placeholder.
    if not blocks:
        blocks.append(_paragraph(""))
    return blocks


def _text_rich(content: str) -> list[dict]:
    # Notion caps a single rich_text entry at 2000 chars.
    if len(content) <= 2000:
        return [{"type": "text", "text": {"content": content}}]
    return [
        {"type": "text", "text": {"content": content[i : i + 2000]}}
        for i in range(0, len(content), 2000)
    ]


def _paragraph(content: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _text_rich(content)},
    }


def _heading(level: int, content: str) -> dict:
    key = f"heading_{min(max(level, 1), 3)}"
    return {"object": "block", "type": key, key: {"rich_text": _text_rich(content)}}


def _bulleted(content: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": _text_rich(content)},
    }


def _numbered(content: str) -> dict:
    return {
        "object": "block",
        "type": "numbered_list_item",
        "numbered_list_item": {"rich_text": _text_rich(content)},
    }


def _todo(content: str, checked: bool) -> dict:
    return {
        "object": "block",
        "type": "to_do",
        "to_do": {"rich_text": _text_rich(content), "checked": checked},
    }
