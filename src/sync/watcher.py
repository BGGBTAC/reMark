"""Real-time sync via WebSocket notifications.

Listens for document changes and triggers immediate processing,
with exponential backoff reconnection on disconnect.
"""

from __future__ import annotations

import asyncio
import logging

from src.config import AppConfig
from src.ocr.pipeline import OCRPipeline
from src.remarkable.auth import AuthManager
from src.remarkable.cloud import DocumentMetadata, RemarkableCloud
from src.remarkable.documents import DocumentManager
from src.remarkable.websocket import RemarkableWebSocket
from src.sync.engine import SyncEngine

logger = logging.getLogger(__name__)


class RealtimeWatcher:
    """Watch for real-time document changes and trigger sync."""

    def __init__(
        self,
        engine: SyncEngine,
        auth: AuthManager,
        config: AppConfig,
    ):
        self._engine = engine
        self._auth = auth
        self._config = config
        self._ws_config = config.sync.websocket
        self._running = False

    async def watch(self, ocr_pipeline: OCRPipeline) -> None:
        """Connect to WebSocket and process events with reconnection."""
        self._running = True
        backoff = self._ws_config.reconnect_delay

        while self._running:
            try:
                await self._watch_loop(ocr_pipeline)
            except Exception as e:
                if not self._running:
                    break
                logger.warning("WebSocket disconnected: %s. Reconnecting in %ds...", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._ws_config.max_reconnect_delay)
            else:
                # Clean exit
                break

        logger.info("Watcher stopped")

    async def _watch_loop(self, ocr_pipeline: OCRPipeline) -> None:
        """Single WebSocket session — process events until disconnect."""
        async with RemarkableCloud(self._auth) as cloud:
            ws = RemarkableWebSocket(self._auth, cloud)
            await ws.connect()

            logger.info("Real-time watcher connected")

            # Start ping task
            ping_task = asyncio.create_task(
                self._ping_loop(ws, self._ws_config.ping_interval)
            )

            try:
                while self._running and ws.connected:
                    event = await ws.receive()
                    if event is None:
                        continue

                    # Skip events from our own uploads
                    if ws.is_self_event(event):
                        logger.debug("Skipping self-event for %s", event.doc_id[:8])
                        continue

                    logger.info(
                        "Event: %s %s (%s)",
                        event.event_type, event.name or event.doc_id[:8], event.doc_type,
                    )

                    if event.event_type in ("DocAdded", "DocChanged"):
                        await self._handle_document_change(
                            event, cloud, ocr_pipeline,
                        )
                    elif event.event_type == "DocDeleted":
                        logger.info("Document deleted: %s", event.doc_id[:8])

            finally:
                ping_task.cancel()
                await ws.disconnect()

    async def _handle_document_change(
        self,
        event,
        cloud: RemarkableCloud,
        ocr_pipeline: OCRPipeline,
    ) -> None:
        """Process a document change event."""
        doc = DocumentMetadata(
            id=event.doc_id,
            name=event.name,
            parent=event.parent,
            doc_type=event.doc_type or "DocumentType",
            version=event.version,
            hash="",  # WebSocket events don't include hash, force re-check
            modified="",
        )

        download_dir = self._config.sync.state_db.replace("sync_state.db", "downloads")
        doc_manager = DocumentManager(cloud, download_dir)

        result = await self._engine.process_document(doc, doc_manager, ocr_pipeline)

        if result.success:
            logger.info(
                "Processed %s: %d pages, %d actions",
                result.doc_name, result.page_count, result.action_count,
            )
        else:
            logger.warning("Failed to process %s: %s", result.doc_name, result.error)

    async def _ping_loop(self, ws: RemarkableWebSocket, interval: int) -> None:
        """Send periodic pings to keep the connection alive."""
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.ping()
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        """Signal the watcher to stop."""
        self._running = False
