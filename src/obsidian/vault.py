"""Obsidian vault read/write operations.

All file operations on the Obsidian vault go through this module.
Notes are written as Markdown with YAML frontmatter.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from src.processing.actions import ActionItem

logger = logging.getLogger(__name__)


class ObsidianVault:
    """Read/write Markdown files in the Obsidian vault."""

    def __init__(self, vault_path: Path | str, folder_map: dict[str, str]):
        self._vault_path = Path(vault_path).expanduser().resolve()
        self._folder_map = folder_map

    @property
    def path(self) -> Path:
        return self._vault_path

    def resolve_path(self, rm_folder: str, note_name: str) -> Path:
        """Map a reMarkable folder + note name to an Obsidian vault path.

        Uses folder_map config to determine the target subfolder.
        Sanitizes the filename for filesystem safety.
        """
        mapped = self._folder_map.get(rm_folder, self._folder_map.get("_default", "Inbox"))
        safe_name = _sanitize_filename(note_name)
        return self._vault_path / mapped / f"{safe_name}.md"

    def write_note(self, path: Path, frontmatter: dict, content: str) -> None:
        """Write a note with YAML frontmatter + Markdown content.

        Creates parent directories if needed. If the file already exists,
        updates the frontmatter while preserving manually added fields.
        """
        path.parent.mkdir(parents=True, exist_ok=True)

        # If updating, merge frontmatter with existing
        if path.exists():
            existing_fm, _ = self.read_note(path) or ({}, "")
            if existing_fm:
                # Preserve manually added fields
                for key, val in existing_fm.items():
                    if key not in frontmatter:
                        frontmatter[key] = val

        md = _format_note(frontmatter, content)
        path.write_text(md, encoding="utf-8")
        logger.info("Wrote note: %s", path.relative_to(self._vault_path))

    def read_note(self, path: Path) -> tuple[dict, str] | None:
        """Parse frontmatter and content from an existing note.

        Returns (frontmatter_dict, content_str) or None if the file doesn't exist.
        """
        if not path.exists():
            return None

        text = path.read_text(encoding="utf-8")
        return _parse_note(text)

    def write_action_items(
        self, actions: list[ActionItem], source_note: str, source_path: Path
    ) -> Path:
        """Write action items to the Actions folder.

        Creates an action file with Obsidian task syntax and wiki-links
        back to the source note.
        """
        actions_dir = self._vault_path / "Actions"
        actions_dir.mkdir(parents=True, exist_ok=True)

        safe_name = _sanitize_filename(source_note)
        action_path = actions_dir / f"{safe_name}-actions.md"

        # Build relative wiki-link to source
        try:
            rel = source_path.relative_to(self._vault_path)
            wiki_link = f"[[{rel.with_suffix('').as_posix()}]]"
        except ValueError:
            wiki_link = f"[[{source_note}]]"

        lines = [
            f"# Action Items — {source_note}\n",
            f"Source: {wiki_link}\n",
            "",
        ]

        for action in actions:
            checkbox = "- [ ]" if action.type != "question" else "- [?]"
            priority_tag = ""
            if action.priority == "high":
                priority_tag = " #priority-high"
            elif action.priority == "low":
                priority_tag = " #priority-low"

            line = f"{checkbox} {action.task}"
            if action.assignee:
                line += f" @{action.assignee}"
            if action.deadline:
                line += f" 📅 {action.deadline}"
            line += priority_tag

            lines.append(line)

            if action.source_context and action.source_context != action.task:
                lines.append(f"  > {action.source_context}")
            lines.append("")

        action_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Wrote %d actions to %s", len(actions), action_path.name)
        return action_path

    def list_notes_by_source(self, source: str = "remarkable") -> list[Path]:
        """Find all notes with a specific source in frontmatter."""
        results = []
        for md_file in self._vault_path.rglob("*.md"):
            result = self.read_note(md_file)
            if result is None:
                continue
            fm, _ = result
            if fm.get("source") == source:
                results.append(md_file)
        return results

    def ensure_structure(self) -> None:
        """Create the vault directory structure if it doesn't exist."""
        for folder in self._folder_map.values():
            (self._vault_path / folder).mkdir(parents=True, exist_ok=True)

        for special in ["Actions", "Templates"]:
            (self._vault_path / special).mkdir(parents=True, exist_ok=True)

        logger.info("Vault structure verified at %s", self._vault_path)


def _sanitize_filename(name: str) -> str:
    """Make a string safe for use as a filename.

    Removes special chars, limits length, preserves readability.
    """
    # Replace path separators and other unsafe chars
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    # Collapse whitespace
    safe = re.sub(r"\s+", " ", safe).strip()
    # Limit length (leave room for suffix)
    if len(safe) > 200:
        safe = safe[:200].rsplit(" ", 1)[0]
    return safe or "Untitled"


def _format_note(frontmatter: dict, content: str) -> str:
    """Format a note as YAML frontmatter + Markdown content."""
    fm_str = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return f"---\n{fm_str}---\n\n{content}\n"


def _parse_note(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter and Markdown content from a note."""
    if not text.startswith("---"):
        return {}, text

    # Find the closing ---
    end = text.find("---", 3)
    if end == -1:
        return {}, text

    fm_raw = text[3:end].strip()
    content = text[end + 3:].strip()

    try:
        fm = yaml.safe_load(fm_raw) or {}
    except yaml.YAMLError:
        fm = {}

    return fm, content
