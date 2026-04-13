"""SQLite sync state database for idempotent processing.

Tracks which documents have been synced, their hashes, and processing
status so we never process the same version twice.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_state (
    doc_id TEXT PRIMARY KEY,
    doc_name TEXT,
    parent_folder TEXT,
    cloud_hash TEXT,
    local_hash TEXT,
    version INTEGER DEFAULT 0,
    last_synced_at TEXT,
    vault_path TEXT,
    ocr_engine TEXT,
    page_count INTEGER DEFAULT 0,
    action_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'synced'
);

CREATE TABLE IF NOT EXISTS sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    doc_id TEXT,
    action TEXT NOT NULL,
    details TEXT,
    duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS auth_cache (
    key TEXT PRIMARY KEY,
    value TEXT,
    expires_at TEXT
);
"""


@dataclass
class SyncStats:
    """Summary statistics from the sync state database."""

    total_docs: int
    synced: int
    errors: int
    pending: int
    total_pages: int
    total_actions: int
    last_sync: str | None


class SyncState:
    """SQLite-backed sync state tracking."""

    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path).expanduser().resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._ensure_schema()

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _ensure_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def needs_sync(self, doc_id: str, cloud_hash: str) -> bool:
        """Check if a document needs processing.

        Returns True if the document is new or its cloud hash has changed.
        """
        row = self.conn.execute(
            "SELECT cloud_hash, status FROM sync_state WHERE doc_id = ?",
            (doc_id,),
        ).fetchone()

        if row is None:
            return True

        # Re-process if hash changed or previous attempt errored
        return row["cloud_hash"] != cloud_hash or row["status"] == "error"

    def mark_synced(
        self,
        doc_id: str,
        doc_name: str,
        parent_folder: str,
        cloud_hash: str,
        vault_path: str,
        ocr_engine: str,
        page_count: int,
        action_count: int,
    ) -> None:
        """Record a successful sync for a document."""
        now = datetime.now(UTC).isoformat()

        self.conn.execute(
            """INSERT INTO sync_state
               (doc_id, doc_name, parent_folder, cloud_hash, local_hash,
                version, last_synced_at, vault_path, ocr_engine,
                page_count, action_count, status)
               VALUES (?, ?, ?, ?, ?,
                       COALESCE((SELECT version FROM sync_state WHERE doc_id = ?), 0) + 1,
                       ?, ?, ?, ?, ?, 'synced')
               ON CONFLICT(doc_id) DO UPDATE SET
                 doc_name = excluded.doc_name,
                 parent_folder = excluded.parent_folder,
                 cloud_hash = excluded.cloud_hash,
                 local_hash = excluded.local_hash,
                 version = sync_state.version + 1,
                 last_synced_at = excluded.last_synced_at,
                 vault_path = excluded.vault_path,
                 ocr_engine = excluded.ocr_engine,
                 page_count = excluded.page_count,
                 action_count = excluded.action_count,
                 status = 'synced'
            """,
            (doc_id, doc_name, parent_folder, cloud_hash, cloud_hash,
             doc_id, now, vault_path, ocr_engine, page_count, action_count),
        )
        self.conn.commit()
        self._log("sync", doc_id, f"synced {doc_name}")

    def mark_error(self, doc_id: str, error_msg: str) -> None:
        """Record a sync failure. The document will be retried next cycle."""
        now = datetime.now(UTC).isoformat()

        self.conn.execute(
            """INSERT INTO sync_state (doc_id, status, last_synced_at)
               VALUES (?, 'error', ?)
               ON CONFLICT(doc_id) DO UPDATE SET
                 status = 'error',
                 last_synced_at = ?
            """,
            (doc_id, now, now),
        )
        self.conn.commit()
        self._log("error", doc_id, error_msg)

    def mark_response_pending(self, doc_id: str) -> None:
        """Mark a document as having a pending response to push."""
        self.conn.execute(
            "UPDATE sync_state SET status = 'pending_response' WHERE doc_id = ?",
            (doc_id,),
        )
        self.conn.commit()

    def mark_response_sent(self, doc_id: str) -> None:
        """Mark a response as successfully pushed."""
        self.conn.execute(
            "UPDATE sync_state SET status = 'synced' WHERE doc_id = ?",
            (doc_id,),
        )
        self.conn.commit()
        self._log("push", doc_id, "response sent")

    def get_pending_responses(self) -> list[dict]:
        """Get documents with pending responses to push."""
        rows = self.conn.execute(
            "SELECT * FROM sync_state WHERE status = 'pending_response'"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_doc_state(self, doc_id: str) -> dict | None:
        """Get the sync state for a specific document."""
        row = self.conn.execute(
            "SELECT * FROM sync_state WHERE doc_id = ?", (doc_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_sync_stats(self) -> SyncStats:
        """Get summary statistics."""
        row = self.conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN status = 'synced' THEN 1 ELSE 0 END) as synced,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as errors,
                SUM(CASE WHEN status = 'pending_response' THEN 1 ELSE 0 END) as pending,
                SUM(page_count) as total_pages,
                SUM(action_count) as total_actions,
                MAX(last_synced_at) as last_sync
            FROM sync_state
        """).fetchone()

        return SyncStats(
            total_docs=row["total"] or 0,
            synced=row["synced"] or 0,
            errors=row["errors"] or 0,
            pending=row["pending"] or 0,
            total_pages=row["total_pages"] or 0,
            total_actions=row["total_actions"] or 0,
            last_sync=row["last_sync"],
        )

    def get_recent_log(self, limit: int = 50) -> list[dict]:
        """Get recent sync log entries."""
        rows = self.conn.execute(
            "SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    def _log(self, action: str, doc_id: str | None, details: str, duration_ms: int = 0) -> None:
        """Write to the sync log."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "INSERT INTO sync_log (timestamp, doc_id, action, details, duration_ms) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, doc_id, action, details, duration_ms),
        )
        self.conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
