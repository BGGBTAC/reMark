"""Sync scheduling — cron-based and interval-based sync triggers.

Runs the sync engine on a configurable schedule using asyncio.
Supports cron expressions and simple interval-based scheduling.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from src.config import AppConfig
from src.ocr.pipeline import OCRPipeline
from src.remarkable.auth import AuthManager
from src.remarkable.cloud import RemarkableCloud
from src.remarkable.documents import DocumentManager
from src.sync.engine import SyncEngine

logger = logging.getLogger(__name__)


class SyncScheduler:
    """Run the sync engine on a schedule."""

    def __init__(self, engine: SyncEngine, config: AppConfig):
        self._engine = engine
        self._config = config
        self._running = False

    async def run(self, auth: AuthManager, ocr_pipeline: OCRPipeline) -> None:
        """Run sync on a schedule until stopped.

        Parses the cron expression from config and sleeps between runs.
        """
        self._running = True
        interval = _parse_interval(self._config.sync.schedule)

        logger.info("Scheduler started (interval: %ds)", interval)

        while self._running:
            await self._run_cycle(auth, ocr_pipeline)

            logger.debug("Next sync in %ds", interval)
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

        logger.info("Scheduler stopped")

    async def run_once(self, auth: AuthManager, ocr_pipeline: OCRPipeline) -> None:
        """Run a single sync cycle."""
        await self._run_cycle(auth, ocr_pipeline)

    async def _run_cycle(self, auth: AuthManager, ocr_pipeline: OCRPipeline) -> None:
        """Execute one sync cycle with full resource management."""
        start = datetime.now(timezone.utc)
        logger.info("Sync cycle starting at %s", start.isoformat())

        try:
            download_dir = self._config.sync.state_db.replace("sync_state.db", "downloads")

            async with RemarkableCloud(auth) as cloud:
                doc_manager = DocumentManager(cloud, download_dir)
                report = await self._engine.sync_once(cloud, doc_manager, ocr_pipeline)

            logger.info(
                "Cycle complete: %d/%d processed, %d skipped, %d errors (%dms)",
                report.success_count, report.total,
                report.skipped, report.errors, report.duration_ms,
            )

        except Exception as e:
            logger.error("Sync cycle failed: %s", e, exc_info=True)

    def stop(self) -> None:
        """Signal the scheduler to stop after the current cycle."""
        self._running = False


def _parse_interval(cron_expr: str) -> int:
    """Parse a cron expression or simple interval into seconds.

    Supports:
    - "*/N * * * *" -> every N minutes
    - "N" -> every N seconds (simple format)
    - Standard 5-field cron (calculates approximate interval)
    """
    cron_expr = cron_expr.strip()

    # Simple integer = seconds
    if cron_expr.isdigit():
        return max(60, int(cron_expr))

    parts = cron_expr.split()
    if len(parts) != 5:
        logger.warning("Unparseable cron expression '%s', defaulting to 15 min", cron_expr)
        return 900

    minute_field = parts[0]

    # */N pattern
    if minute_field.startswith("*/"):
        try:
            n = int(minute_field[2:])
            return n * 60
        except ValueError:
            pass

    # Specific minute = hourly
    if minute_field.isdigit():
        return 3600

    logger.warning("Complex cron '%s', defaulting to 15 min", cron_expr)
    return 900
