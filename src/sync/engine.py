"""Sync orchestrator — the main pipeline coordinator.

Ties together all modules: Cloud API, OCR, processing, Obsidian vault,
and state tracking into a single sync cycle.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from src.config import AppConfig, resolve_path
from src.obsidian.frontmatter import generate_frontmatter
from src.obsidian.git_sync import GitSync
from src.obsidian.templates import format_note_content
from src.obsidian.vault import ObsidianVault
from src.ocr.pipeline import OCRPipeline
from src.processing.actions import ActionExtractor
from src.processing.structurer import NoteStructurer
from src.processing.summarizer import NoteSummarizer
from src.processing.tagger import NoteTagger
from src.remarkable.cloud import DocumentMetadata, RemarkableCloud
from src.remarkable.documents import DocumentManager
from src.remarkable.formats import Notebook, extract_strokes_by_color, parse_notebook
from src.sync.state import SyncState

logger = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    """Result of processing a single document."""

    doc_id: str
    doc_name: str
    success: bool
    vault_path: str = ""
    page_count: int = 0
    action_count: int = 0
    ocr_engine: str = ""
    error: str = ""
    duration_ms: int = 0


@dataclass
class SyncReport:
    """Summary of a full sync cycle."""

    processed: list[ProcessResult] = field(default_factory=list)
    skipped: int = 0
    errors: int = 0
    duration_ms: int = 0

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.processed if r.success)

    @property
    def total(self) -> int:
        return len(self.processed)


class SyncEngine:
    """Orchestrates the full sync pipeline.

    One call to sync_once() runs a complete cycle:
    1. List documents from Cloud
    2. Filter to new/changed documents
    3. Download, OCR, process, write to vault
    4. Push pending responses
    5. Git commit + push
    """

    def __init__(self, config: AppConfig):
        self._config = config
        self._state: SyncState | None = None
        self._vault: ObsidianVault | None = None
        self._git: GitSync | None = None
        self._anthropic: anthropic.AsyncAnthropic | None = None

    @property
    def state(self) -> SyncState:
        if self._state is None:
            self._state = SyncState(resolve_path(self._config.sync.state_db))
        return self._state

    @property
    def vault(self) -> ObsidianVault:
        if self._vault is None:
            self._vault = ObsidianVault(
                Path(self._config.obsidian.vault_path).expanduser(),
                self._config.obsidian.folder_map,
            )
        return self._vault

    @property
    def git(self) -> GitSync | None:
        if self._git is None and self._config.obsidian.git.enabled:
            self._git = GitSync(
                self._config.obsidian.vault_path,
                remote=self._config.obsidian.git.remote,
                branch=self._config.obsidian.git.branch,
                commit_template=self._config.obsidian.git.commit_message_template,
            )
        return self._git

    def _get_anthropic(self) -> anthropic.AsyncAnthropic:
        if self._anthropic is None:
            import os
            api_key = os.environ.get(self._config.processing.api_key_env, "")
            self._anthropic = anthropic.AsyncAnthropic(api_key=api_key)
        return self._anthropic

    async def sync_once(
        self,
        cloud: RemarkableCloud,
        doc_manager: DocumentManager,
        ocr_pipeline: OCRPipeline,
    ) -> SyncReport:
        """Run one full sync cycle."""
        start = time.monotonic()
        report = SyncReport()

        # 1. List documents and filter
        logger.info("Starting sync cycle...")
        rm_config = self._config.remarkable
        docs = await doc_manager.list_documents(
            sync_folders=rm_config.sync_folders or None,
            ignore_folders=rm_config.ignore_folders or None,
        )

        # 2. Find new/changed documents
        to_process = []
        for doc in docs:
            if doc.is_folder:
                continue
            if self.state.needs_sync(doc.id, doc.hash):
                to_process.append(doc)
            else:
                report.skipped += 1

        logger.info(
            "Found %d documents: %d to process, %d up-to-date",
            len(docs), len(to_process), report.skipped,
        )

        # 3. Process each document
        for doc in to_process:
            result = await self.process_document(doc, doc_manager, ocr_pipeline)
            report.processed.append(result)
            if not result.success:
                report.errors += 1

        # 4. Push pending responses
        if self._config.sync.push_responses:
            await self.push_pending_responses(cloud)

        # 5. Git commit + push
        if report.success_count > 0 and self.git:
            if self._config.obsidian.git.auto_commit:
                self.git.commit(report.success_count)
            if self._config.obsidian.git.auto_push:
                self.git.push()

        report.duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "Sync complete: %d processed, %d skipped, %d errors (%dms)",
            report.success_count, report.skipped, report.errors, report.duration_ms,
        )

        return report

    async def process_document(
        self,
        doc: DocumentMetadata,
        doc_manager: DocumentManager,
        ocr_pipeline: OCRPipeline,
    ) -> ProcessResult:
        """Process a single document through the full pipeline."""
        start = time.monotonic()
        logger.info("Processing: %s (%s)", doc.name, doc.id[:8])

        try:
            # Download
            resolved = await doc_manager.download(doc)

            # Parse .rm files
            pages = parse_notebook(resolved.local_dir, doc.id, resolved.page_ids)

            # Build Notebook dataclass
            notebook = Notebook(
                id=doc.id,
                name=resolved.meta.name,
                folder=resolved.folder_path,
                modified=doc.modified,
                pages=pages,
            )

            # OCR
            ocr_results = await ocr_pipeline.recognize(
                pages=pages,
                doc_dir=resolved.local_dir,
                doc_id=doc.id,
                page_ids=resolved.page_ids,
            )

            # Combine all text
            raw_text = "\n\n".join(r.text for r in ocr_results if r.text.strip())

            if not raw_text.strip():
                logger.info("No text extracted from %s, skipping processing", doc.name)
                duration = int((time.monotonic() - start) * 1000)
                return ProcessResult(
                    doc_id=doc.id, doc_name=doc.name, success=True,
                    page_count=len(pages), duration_ms=duration,
                )

            # Processing via Anthropic API
            client = self._get_anthropic()
            model = self._config.processing.model

            structurer = NoteStructurer(client, model)
            structured = await structurer.structure(raw_text, notebook.name)

            actions = []
            if self._config.processing.extract_actions:
                extractor = ActionExtractor(client, model)
                color_annotations = None
                action_colors = self._config.processing.actions.action_colors
                if action_colors:
                    color_annotations = extract_strokes_by_color(
                        resolved.local_dir, doc.id, resolved.page_ids, action_colors
                    )
                actions = await extractor.extract(structured.content_md, color_annotations)

            tags = []
            if self._config.processing.extract_tags:
                tagger = NoteTagger(client, model)
                tags = await tagger.tag(structured.content_md, notebook.name)

            summary = None
            if self._config.processing.generate_summary:
                summarizer = NoteSummarizer(client, model)
                summary = await summarizer.summarize(structured.content_md, notebook.name)

            # Generate frontmatter
            frontmatter = generate_frontmatter(
                notebook, ocr_results, actions, tags,
                summary_one_line=summary.one_line if summary else "",
            )

            # Format content
            content = format_note_content(
                structured.content_md,
                summary=summary,
                actions=actions,
                page_texts=ocr_results,
            )

            # Write to vault
            vault_path = self.vault.resolve_path(resolved.folder_path, notebook.name)
            self.vault.write_note(vault_path, frontmatter, content)

            # Write action items
            if actions:
                self.vault.write_action_items(actions, notebook.name, vault_path)

            # Update state
            engines = list({r.engine_used for r in ocr_results if r.engine_used != "none"})
            self.state.mark_synced(
                doc_id=doc.id,
                doc_name=doc.name,
                parent_folder=resolved.folder_path,
                cloud_hash=doc.hash,
                vault_path=str(vault_path),
                ocr_engine=",".join(engines),
                page_count=len(pages),
                action_count=len(actions),
            )

            # Cleanup downloaded files
            doc_manager.cleanup(doc.id)

            duration = int((time.monotonic() - start) * 1000)
            logger.info(
                "Processed %s: %d pages, %d actions, %d tags (%dms)",
                doc.name, len(pages), len(actions), len(tags), duration,
            )

            return ProcessResult(
                doc_id=doc.id,
                doc_name=doc.name,
                success=True,
                vault_path=str(vault_path),
                page_count=len(pages),
                action_count=len(actions),
                ocr_engine=",".join(engines),
                duration_ms=duration,
            )

        except Exception as e:
            duration = int((time.monotonic() - start) * 1000)
            logger.error("Failed to process %s: %s", doc.name, e, exc_info=True)
            self.state.mark_error(doc.id, str(e))
            return ProcessResult(
                doc_id=doc.id,
                doc_name=doc.name,
                success=False,
                error=str(e),
                duration_ms=duration,
            )

    async def push_pending_responses(self, cloud: RemarkableCloud) -> int:
        """Push pending response PDFs back to reMarkable.

        Returns the number of responses pushed.
        """
        pending = self.state.get_pending_responses()
        if not pending:
            return 0

        pushed = 0
        for entry in pending:
            try:
                vault_path = Path(entry["vault_path"])
                result = self.vault.read_note(vault_path)
                if result is None:
                    continue

                fm, content = result
                if fm.get("status") != "response_ready":
                    continue

                # Response push is implemented in Phase 7
                # For now, just mark as sent
                logger.info("Response push placeholder for %s", entry["doc_name"])
                self.state.mark_response_sent(entry["doc_id"])
                pushed += 1

            except Exception as e:
                logger.warning("Failed to push response for %s: %s", entry["doc_id"][:8], e)

        return pushed
