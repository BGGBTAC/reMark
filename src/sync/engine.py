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
from src.llm.client import LLMClient
from src.llm.factory import build_llm_client
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
        self._llm_client: LLMClient | None = None
        # Kept separately for ResponseGenerator, which still uses the raw
        # Anthropic SDK surface (messages.create, tool_use, etc.).
        self._anthropic: anthropic.AsyncAnthropic | None = None
        self._indexer = None  # Lazy — only built if search is enabled
        self._plugins = None  # Lazy plugin registry
        # Multi-device + multi-user context: the active (user, device)
        # for the current cycle. Set via set_device(); used when
        # writing sync_state rows.
        self._current_device_id: str = "default"
        self._current_vault_subfolder: str = ""
        self._current_user_id: int = 1  # pre-0.7 single-user default

    def set_device(
        self,
        device_id: str,
        vault_subfolder: str = "",
        user_id: int = 1,
    ) -> None:
        """Switch the engine's active device context.

        Multi-device installs call this before each sync_once() run so the
        state DB records which tablet the documents came from and writes
        land in the correct vault subfolder. ``user_id`` scopes rows for
        multi-user installs — defaults to 1 (the admin) for pre-0.7
        behaviour.
        """
        self._current_device_id = device_id
        self._current_vault_subfolder = vault_subfolder
        self._current_user_id = user_id

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

    def _get_llm_client(self) -> LLMClient:
        """Return the shared LLMClient, constructing it on first call.

        All processing consumers (structurer, tagger, summarizer, extractor)
        receive this rather than a raw vendor SDK client, so switching
        providers is a config-only operation.
        """
        if self._llm_client is None:
            import os
            self._llm_client = build_llm_client(
                self._config.llm,
                anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
            )
        return self._llm_client

    def _get_anthropic(self) -> anthropic.AsyncAnthropic:
        """Raw Anthropic client for ResponseGenerator, which still uses the
        SDK directly (messages.create, streaming, etc.). Processing consumers
        should use _get_llm_client() instead."""
        if self._anthropic is None:
            import os
            api_key = os.environ.get(self._config.processing.api_key_env, "")
            client = anthropic.AsyncAnthropic(api_key=api_key)
            self._anthropic = _wrap_client_for_usage_tracking(
                client, self._config.processing.model, self.state,
            )
        return self._anthropic

    @property
    def plugins(self):
        """Lazy-load the plugin registry."""
        if self._plugins is None:
            from src.plugins.registry import PluginRegistry
            self._plugins = PluginRegistry(self._config.plugins)
            self._plugins.discover()
        return self._plugins

    def _get_indexer(self):
        """Lazy-build the search indexer. Returns None if search is disabled."""
        if self._indexer is not None:
            return self._indexer
        if not self._config.search.enabled:
            return None

        try:
            from src.search.backends import build_backend
            from src.search.index import VectorIndex
            from src.search.indexer import Indexer

            backend = build_backend(
                self._config.search.backend,
                model=self._config.search.model,
                api_key_env=self._config.search.api_key_env,
            )
            index = VectorIndex(
                db_path=resolve_path(self._config.sync.state_db),
                dimension=backend.dimension,
            )
            self._indexer = Indexer(
                backend=backend,
                index=index,
                vault=self.vault,
                chunk_size=self._config.search.chunk_size,
                chunk_overlap=self._config.search.chunk_overlap,
            )
            return self._indexer
        except Exception as e:
            logger.warning("Failed to initialize search indexer: %s", e)
            return None

    def _archive_deleted(self, state_entry: dict) -> int:
        """Archive a note whose source document was deleted on reMarkable.

        Moves the vault file to Archive/, removes search index entries,
        and updates state. Returns 1 if archived, 0 otherwise.
        """
        doc_id = state_entry.get("doc_id", "")
        vault_path_str = state_entry.get("vault_path", "")
        doc_name = state_entry.get("doc_name") or doc_id

        if not vault_path_str:
            self.state.mark_archived(doc_id)
            return 1

        try:
            path = Path(vault_path_str)
            if path.exists():
                self.vault.archive_note(path)

            # Remove from search index if enabled
            indexer = self._get_indexer()
            if indexer:
                indexer.remove_document(doc_id)

            self.state.mark_archived(doc_id)
            logger.info("Archived deleted document: %s", doc_name)
            return 1
        except Exception as e:
            logger.warning("Failed to archive %s: %s", doc_name, e)
            return 0

    async def _drain_queue(
        self,
        docs,
        doc_manager: DocumentManager,
        ocr_pipeline: OCRPipeline,
        report: SyncReport,
    ) -> None:
        """Retry every due sync_queue entry once per cycle.

        Currently we only retry ``process_document`` entries since
        that's the only operation the engine enqueues. Plugins or
        future ops can extend this switch.
        """
        due = self.state.dequeue_ready(limit=20)
        if not due:
            return

        by_id = {doc.id: doc for doc in docs}
        logger.info("Retrying %d queued operation(s)", len(due))

        for entry in due:
            queue_id = int(entry["id"])
            op = entry["op_type"]

            if op == "process_document":
                doc_id = entry["doc_id"]
                doc = by_id.get(doc_id)
                if doc is None:
                    # Cloud no longer has the document — drop the
                    # retry, it's never going to succeed.
                    self.state.mark_queue_done(queue_id)
                    continue
                try:
                    result = await self.process_document(
                        doc, doc_manager, ocr_pipeline,
                    )
                    if result.success:
                        self.state.mark_queue_done(queue_id)
                        report.processed.append(result)
                    else:
                        self.state.mark_queue_failed(
                            queue_id, result.error or "unknown",
                        )
                except Exception as e:  # noqa: BLE001
                    self.state.mark_queue_failed(queue_id, str(e))
            else:
                logger.warning("Unknown queue op %r — marking failed", op)
                self.state.mark_queue_failed(queue_id, f"unknown op {op}")

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

        # Fire pre-sync plugin hooks
        if self._config.plugins.enabled:
            from src.plugins.hooks import SyncHook
            for plugin in self.plugins.hooks(SyncHook):
                try:
                    await plugin.before_sync({"docs_found": len(docs)})
                except Exception as e:
                    logger.warning("SyncHook '%s' before_sync failed: %s", plugin.metadata.name, e)

        # 3. Detect deletions — docs in state but no longer on Cloud
        cloud_ids = {doc.id for doc in docs if not doc.is_folder}
        archived_count = 0
        for entry in self.state.list_active_docs():
            if entry["doc_id"] not in cloud_ids:
                archived_count += self._archive_deleted(entry)
        if archived_count:
            logger.info("Archived %d deleted documents", archived_count)

        # 4a. Drain due retries first — a failing Cloud call on a
        # previous cycle enqueued an entry; we give it another shot
        # before picking up fresh work. Success = done, failure bumps
        # the attempts counter with exponential backoff.
        await self._drain_queue(docs, doc_manager, ocr_pipeline, report)

        # 4. Process each document
        for doc in to_process:
            result = await self.process_document(doc, doc_manager, ocr_pipeline)
            report.processed.append(result)
            if not result.success:
                report.errors += 1

        # 5. Push pending responses
        if self._config.sync.push_responses:
            await self.push_pending_responses(cloud)

        # 5b. Reverse-sync: push vault notes back to the tablet
        if self._config.reverse_sync.enabled:
            try:
                from src.sync.reverse_sync import ReverseSyncer
                syncer = ReverseSyncer(
                    self._config.reverse_sync, self.vault, self.state,
                )
                rev = await syncer.run(cloud)
                if rev.total > 0:
                    logger.info(
                        "Reverse-sync: %d pushed, %d failed",
                        len(rev.pushed), len(rev.failed),
                    )
            except Exception as e:
                logger.warning("Reverse-sync failed: %s", e)

        # 6. Git commit + push
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

        # Fire post-sync plugin hooks
        if self._config.plugins.enabled:
            from src.plugins.hooks import SyncHook
            report_dict = {
                "total": report.total,
                "success": report.success_count,
                "skipped": report.skipped,
                "errors": report.errors,
                "duration_ms": report.duration_ms,
            }
            for plugin in self.plugins.hooks(SyncHook):
                try:
                    await plugin.after_sync({"docs_found": len(docs)}, report_dict)
                except Exception as e:
                    logger.warning(
                        "SyncHook '%s' after_sync failed: %s",
                        plugin.metadata.name, e,
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

            # Processing via configured LLM provider
            llm = self._get_llm_client()
            model = self._config.processing.model

            structurer = NoteStructurer(llm=llm, model=model)
            structured = await structurer.structure(raw_text, notebook.name)

            actions = []
            if self._config.processing.extract_actions:
                extractor = ActionExtractor(llm=llm, model=model)
                color_annotations = None
                action_colors = self._config.processing.actions.action_colors
                if action_colors:
                    color_annotations = extract_strokes_by_color(
                        resolved.local_dir, doc.id, resolved.page_ids, action_colors
                    )
                actions = await extractor.extract(structured.content_md, color_annotations)

            tags = []
            if self._config.processing.extract_tags:
                tagger = NoteTagger(
                    llm=llm, model=model,
                    hierarchical=self._config.processing.hierarchical_tags,
                )
                tags = await tagger.tag(structured.content_md, notebook.name)

            summary = None
            if self._config.processing.generate_summary:
                summarizer = NoteSummarizer(llm=llm, model=model)
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

            # Template detection — if the synced doc was pushed as a template,
            # extract structured fields into frontmatter.
            if self._config.templates.enabled:
                template_entry = self.state.get_template_for_doc(doc.id)
                if template_entry:
                    try:
                        from src.templates.engine import TemplateEngine
                        engine = TemplateEngine(self._config.templates.user_templates_dir)
                        template = engine.get(template_entry["template_name"])
                        if template is not None:
                            extracted = engine.extract_fields(
                                template.name, structured.content_md,
                            )
                            if extracted:
                                frontmatter["template"] = template.name
                                frontmatter["template_fields"] = extracted
                    except Exception as e:
                        logger.warning("Template extraction failed for %s: %s", doc.name, e)

            # Plugin note-processors (last chance to mutate content/frontmatter)
            if self._config.plugins.enabled:
                from src.plugins.hooks import NoteProcessorHook
                for plugin in self.plugins.hooks(NoteProcessorHook):
                    try:
                        content, frontmatter = await plugin.process(content, frontmatter)
                    except Exception as e:
                        logger.warning(
                            "NoteProcessor plugin '%s' failed: %s",
                            plugin.metadata.name, e,
                        )

            # Write to vault — prefix with the active device's subfolder
            # so multi-device installs keep their notes separated.
            folder_path = resolved.folder_path
            if self._current_vault_subfolder:
                folder_path = f"{self._current_vault_subfolder}/{folder_path}"
            vault_path = self.vault.resolve_path(folder_path, notebook.name)
            self.vault.write_note(vault_path, frontmatter, content)

            # Write action items
            if actions:
                self.vault.write_action_items(actions, notebook.name, vault_path)

            # Index for semantic search
            indexer = self._get_indexer()
            if indexer:
                try:
                    await indexer.index_note(doc.id, vault_path, content)
                except Exception as e:
                    logger.warning("Indexing failed for %s: %s", doc.name, e)

            # Microsoft integration: push action items to To Do / Calendar / OneNote
            if self._config.microsoft.enabled:
                from src.integrations.microsoft.service import MicrosoftService
                ms_service = MicrosoftService(self._config.microsoft)
                if ms_service.enabled and actions:
                    ms_result = await ms_service.sync_actions(actions, source_note=notebook.name)
                    for task_id in ms_result.tasks_created:
                        self.state.record_external_link(
                            doc.id, "microsoft_todo", "task", task_id,
                        )
                    for event_id in ms_result.events_created:
                        self.state.record_external_link(
                            doc.id, "microsoft_calendar", "event", event_id,
                        )

                # OneNote parallel write (if enabled)
                if self._config.microsoft.onenote.enabled:
                    try:
                        page_id = await ms_service.write_to_onenote(
                            title=notebook.name,
                            content=content,
                            folder=resolved.folder_path,
                            tags=tags,
                        )
                        if page_id:
                            self.state.record_external_link(
                                doc.id, "microsoft_onenote", "page", page_id,
                            )
                    except Exception as e:
                        logger.warning("OneNote mirror failed for %s: %s", doc.name, e)

            # Notion mirror (if enabled)
            if self._config.notion.enabled:
                try:
                    from src.integrations.notion import NotionService
                    notion_service = NotionService(self._config.notion)
                    if notion_service.enabled:
                        result_notion = await notion_service.write_note(
                            title=notebook.name,
                            content=content,
                            tags=tags,
                        )
                        if result_notion is not None:
                            self.state.record_external_link(
                                doc.id, "notion", "page", result_notion.page_id,
                            )
                except Exception as e:
                    logger.warning("Notion mirror failed for %s: %s", doc.name, e)

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
                device_id=self._current_device_id,
                user_id=self._current_user_id,
            )

            # Evaluate auto-response trigger
            if self._config.sync.push_responses and self._config.response.auto_trigger:
                from src.response.generator import should_auto_trigger

                question_colors = self._config.processing.actions.question_colors
                has_blue_questions = False
                if question_colors:
                    color_groups = extract_strokes_by_color(
                        resolved.local_dir, doc.id, resolved.page_ids, question_colors,
                    )
                    has_blue_questions = any(color_groups.values())
                if should_auto_trigger(
                    self._config.response,
                    structured.content_md,
                    has_color_questions=has_blue_questions,
                    action_count=len(actions),
                ):
                    self.state.mark_response_pending(doc.id)
                    logger.info("Auto-trigger: queued response for %s", doc.name)

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
            # Enqueue a retry so a later cycle can pick the doc up. We
            # pass the cloud hash as payload so the retry can skip if
            # the document has been updated in the meantime.
            try:
                self.state.enqueue(
                    op_type="process_document",
                    doc_id=doc.id,
                    payload=doc.hash,
                )
            except Exception as enqueue_err:
                logger.warning(
                    "Couldn't enqueue retry for %s: %s", doc.name, enqueue_err,
                )
            return ProcessResult(
                doc_id=doc.id,
                doc_name=doc.name,
                success=False,
                error=str(e),
                duration_ms=duration,
            )

    async def push_pending_responses(self, cloud: RemarkableCloud) -> int:
        """Generate and push pending responses back to reMarkable.

        Walks the state table for documents marked 'pending_response',
        builds a response document (PDF or native notebook based on
        config.response.format), uploads it to the configured response
        folder on the tablet, and updates the state on success.

        Returns the number of responses successfully pushed.
        """
        from src.response.generator import ResponseGenerator
        from src.response.uploader import ResponseUploader

        pending = self.state.get_pending_responses()
        if not pending:
            return 0

        response_config = self._config.response
        generator = ResponseGenerator(
            vault=self.vault,
            config=response_config,
            anthropic_client=self._get_anthropic() if response_config.include_analysis else None,
            model=self._config.processing.model,
        )
        uploader = ResponseUploader(cloud, response_folder=response_config.response_folder)

        pushed = 0
        for entry in pending:
            doc_id = entry["doc_id"]
            doc_name = entry.get("doc_name") or doc_id
            vault_path_str = entry.get("vault_path")

            if not vault_path_str:
                logger.warning("Skipping response for %s: no vault path in state", doc_id[:8])
                continue

            try:
                response = await generator.generate_from_note(Path(vault_path_str))
                if response is None:
                    logger.info("No response content for %s, skipping", doc_name)
                    self.state.mark_response_sent(doc_id)
                    continue

                if response.format == "pdf" and response.pdf_bytes:
                    await uploader.upload_pdf(response.pdf_bytes, response.title)
                elif response.format == "notebook" and response.notebook_files:
                    await uploader.upload_notebook(response.notebook_files, response.title)
                else:
                    logger.warning("Response for %s has no payload, skipping", doc_name)
                    continue

                self.state.mark_response_sent(doc_id)
                pushed += 1
                logger.info(
                    "Pushed response for %s (%d questions, %d actions)",
                    doc_name, response.question_count, response.action_count,
                )

            except Exception as e:
                logger.warning(
                    "Failed to push response for %s: %s",
                    doc_id[:8], e, exc_info=True,
                )

        return pushed

    async def generate_response_for_note(
        self, note_path: Path, cloud: RemarkableCloud,
    ) -> bool:
        """Manually generate and push a response for a specific vault note.

        Used by the CLI 'respond' command and the MCP 'generate_response' tool.
        Returns True if the response was successfully uploaded.
        """
        from src.response.generator import ResponseGenerator
        from src.response.uploader import ResponseUploader

        response_config = self._config.response
        generator = ResponseGenerator(
            vault=self.vault,
            config=response_config,
            anthropic_client=self._get_anthropic() if response_config.include_analysis else None,
            model=self._config.processing.model,
        )
        uploader = ResponseUploader(cloud, response_folder=response_config.response_folder)

        response = await generator.generate_from_note(note_path)
        if response is None:
            return False

        try:
            if response.format == "pdf" and response.pdf_bytes:
                await uploader.upload_pdf(response.pdf_bytes, response.title)
            elif response.format == "notebook" and response.notebook_files:
                await uploader.upload_notebook(response.notebook_files, response.title)
            else:
                return False
            logger.info("Generated response for %s", note_path.name)
            return True
        except Exception as e:
            logger.error("Response upload failed for %s: %s", note_path.name, e)
            return False


def _wrap_client_for_usage_tracking(
    client: anthropic.AsyncAnthropic,
    default_model: str,
    state,
) -> anthropic.AsyncAnthropic:
    """Wrap an Anthropic client so every messages.create() call logs usage.

    We monkey-patch the bound method on the messages resource — it's the
    least invasive way to track usage across all processors without
    passing the state through every class.
    """
    import contextlib

    from src.processing.usage import log_anthropic_response

    original_create = client.messages.create

    async def tracked_create(*args, **kwargs):
        response = await original_create(*args, **kwargs)
        model = kwargs.get("model", default_model)
        operation = kwargs.get("_operation", "messages.create")
        with contextlib.suppress(Exception):
            log_anthropic_response(
                state,
                response,
                model=model,
                operation=operation,
            )
        return response

    client.messages.create = tracked_create
    return client
