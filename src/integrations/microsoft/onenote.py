"""Microsoft OneNote integration via Graph API.

Creates pages in a configured OneNote notebook, mirroring (a subset of)
what the Obsidian integration does. Can run in parallel to ObsidianVault
so notes are written to both targets.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass

from src.config import OneNoteConfig
from src.integrations.microsoft.graph import GraphClient, GraphError

logger = logging.getLogger(__name__)


@dataclass
class OneNotePage:
    """Summary of a created or fetched OneNote page."""

    id: str
    title: str
    section_id: str


class OneNoteClient:
    """High-level OneNote client using Graph API."""

    def __init__(self, graph: GraphClient, config: OneNoteConfig):
        self._graph = graph
        self._config = config
        self._notebook_id: str | None = None
        self._section_cache: dict[str, str] = {}  # section_name -> section_id

    async def get_or_create_notebook(self) -> str:
        """Return the notebook ID, creating it if missing."""
        if self._notebook_id:
            return self._notebook_id

        data = await self._graph.get("/me/onenote/notebooks")
        for nb in data.get("value", []):
            if nb.get("displayName") == self._config.notebook_name:
                self._notebook_id = nb["id"]
                return self._notebook_id

        created = await self._graph.post(
            "/me/onenote/notebooks",
            body={"displayName": self._config.notebook_name},
        )
        self._notebook_id = created["id"]
        logger.info(
            "Created OneNote notebook '%s' (%s)",
            self._config.notebook_name,
            self._notebook_id[:8],
        )
        return self._notebook_id

    async def get_or_create_section(self, name: str) -> str:
        """Return the section ID for the given name, creating if needed."""
        if name in self._section_cache:
            return self._section_cache[name]

        notebook_id = await self.get_or_create_notebook()
        data = await self._graph.get(f"/me/onenote/notebooks/{notebook_id}/sections")
        for sec in data.get("value", []):
            if sec.get("displayName") == name:
                self._section_cache[name] = sec["id"]
                return sec["id"]

        if not self._config.create_missing_sections:
            raise GraphError(f"OneNote section '{name}' not found (auto-create disabled)")

        created = await self._graph.post(
            f"/me/onenote/notebooks/{notebook_id}/sections",
            body={"displayName": name},
        )
        section_id = created["id"]
        self._section_cache[name] = section_id
        logger.info("Created OneNote section '%s' (%s)", name, section_id[:8])
        return section_id

    async def write_page(
        self,
        title: str,
        content_md: str,
        folder: str = "",
        tags: list[str] | None = None,
    ) -> OneNotePage:
        """Create a new OneNote page with the given content.

        content_md is converted to HTML that OneNote understands.
        """
        section_name = self._resolve_section(folder)
        section_id = await self.get_or_create_section(section_name)

        html_body = _markdown_to_onenote_html(title, content_md, tags or [])

        # OneNote expects a multipart-style HTML body
        headers = {
            "Content-Type": "application/xhtml+xml",
        }
        # Use direct httpx call since Graph expects non-JSON body here
        token = await self._graph._auth.get_access_token()
        resp = await self._graph.client.post(
            f"https://graph.microsoft.com/v1.0/me/onenote/sections/{section_id}/pages",
            content=html_body.encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                **headers,
            },
        )
        if resp.status_code >= 400:
            raise GraphError(f"OneNote page creation failed: {resp.status_code} {resp.text[:200]}")

        data = resp.json()
        page = OneNotePage(
            id=data["id"],
            title=data.get("title", title),
            section_id=section_id,
        )
        logger.info("Wrote OneNote page '%s' (%s)", page.title, page.id[:8])
        return page

    async def list_pages(self, section_name: str) -> list[OneNotePage]:
        """List pages in a given section."""
        section_id = await self.get_or_create_section(section_name)
        data = await self._graph.get(f"/me/onenote/sections/{section_id}/pages")
        return [
            OneNotePage(
                id=p["id"],
                title=p.get("title", ""),
                section_id=section_id,
            )
            for p in data.get("value", [])
        ]

    def _resolve_section(self, folder: str) -> str:
        """Map a reMarkable folder name to a OneNote section name."""
        mapping = self._config.folder_map
        return mapping.get(folder, mapping.get("_default", self._config.default_section))


# -- Markdown → OneNote HTML conversion --

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+)$")
_CHECKBOX_RE = re.compile(r"^\s*-\s*\[([ x])\]\s+(.+)$", re.IGNORECASE)
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
_WIKI_LINK_RE = re.compile(r"\[\[([^\]]+?)\]\]")


def _markdown_to_onenote_html(title: str, markdown: str, tags: list[str]) -> str:
    """Produce minimalist HTML that OneNote accepts.

    Covers: headings, bullets, checkboxes, bold, italics, wiki-links, paragraphs.
    """
    body_parts: list[str] = []
    in_list = False

    for raw_line in markdown.split("\n"):
        line = raw_line.rstrip()

        if not line.strip():
            if in_list:
                body_parts.append("</ul>")
                in_list = False
            continue

        # Heading
        h = _HEADING_RE.match(line)
        if h:
            if in_list:
                body_parts.append("</ul>")
                in_list = False
            level = min(len(h.group(1)), 6)
            body_parts.append(f"<h{level}>{_inline_format(h.group(2))}</h{level}>")
            continue

        # Checkbox
        cb = _CHECKBOX_RE.match(line)
        if cb:
            if not in_list:
                body_parts.append("<ul>")
                in_list = True
            checked = "checked" if cb.group(1).lower() == "x" else ""
            body_parts.append(
                f'<li><input type="checkbox" {checked} disabled> {_inline_format(cb.group(2))}</li>'
            )
            continue

        # Bullet
        b = _BULLET_RE.match(line)
        if b:
            if not in_list:
                body_parts.append("<ul>")
                in_list = True
            body_parts.append(f"<li>{_inline_format(b.group(1))}</li>")
            continue

        # Paragraph
        if in_list:
            body_parts.append("</ul>")
            in_list = False
        body_parts.append(f"<p>{_inline_format(line.strip())}</p>")

    if in_list:
        body_parts.append("</ul>")

    tag_meta = ""
    if tags:
        tag_str = ", ".join(html.escape(t) for t in tags)
        tag_meta = f"<p><em>Tags: {tag_str}</em></p>"

    body = "\n".join(body_parts)
    return (
        "<!DOCTYPE html>\n"
        f"<html><head><title>{html.escape(title)}</title></head>\n"
        f"<body>{tag_meta}{body}</body></html>"
    )


def _inline_format(text: str) -> str:
    """Apply inline markdown formatting and escape other HTML."""
    escaped = html.escape(text)
    # Note: inline patterns operate on the escaped string so tags we emit are safe
    escaped = _BOLD_RE.sub(r"<strong>\1</strong>", escaped)
    escaped = _ITALIC_RE.sub(r"<em>\1</em>", escaped)
    escaped = _WIKI_LINK_RE.sub(
        lambda m: f'<span title="wiki-link">[{m.group(1)}]</span>',
        escaped,
    )
    return escaped
