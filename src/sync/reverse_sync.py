"""Obsidian → reMarkable reverse sync.

Pushes vault notes back to the tablet based on three trigger kinds:
  A) notes with `push_to_tablet: true` in frontmatter
  B) notes inside a configured folder (e.g. To-Tablet/)
  C) on-demand entries added to the reverse_push_queue state table

The actual rendering uses the existing PDF generator; upload uses the
existing ResponseUploader. This module only handles discovery, scheduling,
and bookkeeping.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from src.config import ReverseSyncConfig
from src.obsidian.vault import ObsidianVault, _dump_frontmatter
from src.remarkable.cloud import RemarkableCloud
from src.response.notebook_writer import NotebookWriter
from src.response.pdf_generator import ResponseContent, ResponsePDFGenerator
from src.response.uploader import ResponseUploader
from src.sync.state import SyncState

logger = logging.getLogger(__name__)


@dataclass
class ReverseResult:
    """Outcome of a reverse-sync pass."""
    pushed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (path, error)

    @property
    def total(self) -> int:
        return len(self.pushed) + len(self.failed)


class ReverseSyncer:
    """Collects candidate notes and pushes them as PDFs to reMarkable."""

    def __init__(
        self,
        config: ReverseSyncConfig,
        vault: ObsidianVault,
        state: SyncState,
    ):
        self._config = config
        self._vault = vault
        self._state = state
        self._pdf = ResponsePDFGenerator()
        self._notebook = NotebookWriter()

    def collect_candidates(self) -> list[Path]:
        """Scan the vault + queue for notes to push."""
        if not self._config.enabled:
            return []

        candidates: dict[Path, None] = {}  # preserve order, dedupe
        vault_root = self._vault.path

        # Trigger A: frontmatter flag
        if self._config.trigger_on_frontmatter:
            for md in vault_root.rglob("*.md"):
                result = self._vault.read_note(md)
                if result is None:
                    continue
                fm, _ = result
                if fm.get("push_to_tablet") is True and not fm.get("pushed_to_tablet_at"):
                    candidates[md] = None

        # Trigger B: dedicated folder
        if self._config.trigger_on_folder:
            folder = vault_root / self._config.folder
            if folder.exists():
                for md in folder.rglob("*.md"):
                    result = self._vault.read_note(md)
                    if result is None:
                        candidates[md] = None
                        continue
                    fm, _ = result
                    if not fm.get("pushed_to_tablet_at"):
                        candidates[md] = None

        # Trigger C: on-demand queue
        if self._config.trigger_on_demand:
            for entry in self._state.get_reverse_queue(status="pending"):
                path = Path(entry["vault_path"])
                if path.exists():
                    candidates[path] = None

        return list(candidates.keys())

    async def run(self, cloud: RemarkableCloud) -> ReverseResult:
        """Collect candidates and push them all."""
        result = ReverseResult()
        candidates = self.collect_candidates()
        if not candidates:
            return result

        uploader = ResponseUploader(cloud, response_folder=self._config.target_folder)

        for note_path in candidates:
            # Ensure every pushed candidate has a queue row for bookkeeping
            self._state.enqueue_reverse_push(str(note_path))
            try:
                rm_doc_id = await self._push_note(note_path, uploader)
                result.pushed.append(str(note_path))
                self._state.mark_reverse_pushed(str(note_path), rm_doc_id)

                if self._config.stamp_frontmatter:
                    self._stamp_note(note_path)

            except Exception as e:
                logger.warning(
                    "Reverse-push failed for %s: %s", note_path.name, e,
                )
                result.failed.append((str(note_path), str(e)))
                self._state.mark_reverse_failed(str(note_path), str(e))

        logger.info(
            "Reverse-sync complete: %d pushed, %d failed",
            len(result.pushed), len(result.failed),
        )
        return result

    async def push_single(
        self, note_path: Path, cloud: RemarkableCloud,
    ) -> str | None:
        """Push a single note on demand (used by CLI / dashboard)."""
        uploader = ResponseUploader(cloud, response_folder=self._config.target_folder)
        try:
            rm_doc_id = await self._push_note(note_path, uploader)
            self._state.enqueue_reverse_push(str(note_path))
            self._state.mark_reverse_pushed(str(note_path), rm_doc_id)
            if self._config.stamp_frontmatter:
                self._stamp_note(note_path)
            return rm_doc_id
        except Exception as e:
            logger.warning("push_single failed for %s: %s", note_path.name, e)
            self._state.enqueue_reverse_push(str(note_path))
            self._state.mark_reverse_failed(str(note_path), str(e))
            return None

    async def _push_note(
        self, note_path: Path, uploader: ResponseUploader,
    ) -> str:
        """Render a note to the configured format and upload."""
        result = self._vault.read_note(note_path)
        if result is None:
            raise FileNotFoundError(f"Note missing or unreadable: {note_path}")

        frontmatter, content = result
        title = frontmatter.get("title") or note_path.stem

        if self._config.format == "notebook":
            files = self._notebook.generate(title, content)
            return await uploader.upload_notebook(files, title)

        # Default: PDF. Build a ResponseContent from the note body.
        key_points = []
        analysis_parts = []
        related: list[str] = []
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("- "):
                key_points.append(stripped[2:])
            elif stripped:
                analysis_parts.append(stripped)
        analysis = "\n\n".join(analysis_parts[:10])  # cap

        response_content = ResponseContent(
            note_title=title,
            summary=frontmatter.get("summary", ""),
            key_points=key_points[:10],
            analysis=analysis,
            related_notes=related,
            metadata={"vault_path": str(note_path)},
        )

        pdf_bytes = self._pdf.generate(response_content)
        return await uploader.upload_pdf(pdf_bytes, title)

    def _stamp_note(self, note_path: Path) -> None:
        """Add pushed_to_tablet_at timestamp to the note's frontmatter."""
        result = self._vault.read_note(note_path)
        if result is None:
            return

        fm, content = result
        fm["pushed_to_tablet_at"] = datetime.now(UTC).isoformat()
        fm_str = _dump_frontmatter(fm)
        note_path.write_text(f"---\n{fm_str}---\n\n{content}\n", encoding="utf-8")
