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
    status TEXT DEFAULT 'synced',
    device_id TEXT NOT NULL DEFAULT 'default'
);
-- NOTE: the idx_sync_state_device index is created in _apply_migrations
-- so pre-0.4.0 databases (which lack the device_id column) can have the
-- column added before we try to index it.

-- Registered reMarkable tablets. One row per physical device when the
-- operator wants to sync multiple tablets into the same vault. Legacy
-- single-device installs implicitly use a row with id='default'.
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    vault_subfolder TEXT NOT NULL DEFAULT '',
    device_token_path TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    last_sync_at TEXT,
    active INTEGER NOT NULL DEFAULT 1
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

CREATE TABLE IF NOT EXISTS external_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    external_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    UNIQUE(provider, external_id)
);
CREATE INDEX IF NOT EXISTS idx_external_links_doc
    ON external_links(doc_id, provider, kind);

CREATE TABLE IF NOT EXISTS api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    operation TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    doc_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_usage_time ON api_usage(timestamp);
CREATE INDEX IF NOT EXISTS idx_api_usage_provider ON api_usage(provider);

CREATE TABLE IF NOT EXISTS reverse_push_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vault_path TEXT NOT NULL UNIQUE,
    queued_at TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    remarkable_doc_id TEXT,
    pushed_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_reverse_push_status ON reverse_push_queue(status);

CREATE TABLE IF NOT EXISTS webpush_subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    user_agent TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS plugin_state (
    name TEXT PRIMARY KEY,
    enabled INTEGER DEFAULT 1,
    config TEXT,
    installed_at TEXT NOT NULL,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS template_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL,
    template_name TEXT NOT NULL,
    pushed_at TEXT NOT NULL,
    filled_at TEXT,
    vault_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_template_doc ON template_instances(doc_id);
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
        self._apply_migrations()
        self.conn.commit()

    def _apply_migrations(self) -> None:
        """Apply additive migrations for pre-existing databases.

        SQLite doesn't support ``IF NOT EXISTS`` on ADD COLUMN, so we
        introspect the schema and add columns that are missing. Keep each
        migration tiny and idempotent.
        """
        # v0.4.0 — sync_state.device_id
        cols = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(sync_state)").fetchall()
        }
        if "device_id" not in cols:
            self.conn.execute(
                "ALTER TABLE sync_state "
                "ADD COLUMN device_id TEXT NOT NULL DEFAULT 'default'"
            )
        # Index creation lives here so fresh installs and upgraded ones
        # both get it, and pre-0.4 DBs don't trip on a missing column.
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sync_state_device "
            "ON sync_state(device_id)"
        )

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
        device_id: str = "default",
    ) -> None:
        """Record a successful sync for a document."""
        now = datetime.now(UTC).isoformat()

        self.conn.execute(
            """INSERT INTO sync_state
               (doc_id, doc_name, parent_folder, cloud_hash, local_hash,
                version, last_synced_at, vault_path, ocr_engine,
                page_count, action_count, status, device_id)
               VALUES (?, ?, ?, ?, ?,
                       COALESCE((SELECT version FROM sync_state WHERE doc_id = ?), 0) + 1,
                       ?, ?, ?, ?, ?, 'synced', ?)
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
                 status = 'synced',
                 device_id = excluded.device_id
            """,
            (doc_id, doc_name, parent_folder, cloud_hash, cloud_hash,
             doc_id, now, vault_path, ocr_engine, page_count, action_count,
             device_id),
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

    def list_active_docs(self) -> list[dict]:
        """Return all synced document entries (not errored, not pending)."""
        rows = self.conn.execute(
            "SELECT * FROM sync_state WHERE status IN ('synced', 'pending_response')"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_archived(self, doc_id: str) -> None:
        """Mark a document as archived (deleted from reMarkable)."""
        self.conn.execute(
            "UPDATE sync_state SET status = 'archived' WHERE doc_id = ?",
            (doc_id,),
        )
        self.conn.commit()
        self._log("archive", doc_id, "archived after cloud deletion")

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

    def record_external_link(
        self,
        doc_id: str,
        provider: str,
        kind: str,
        external_id: str,
    ) -> None:
        """Record a mapping from a doc to an external system's item (task, event, etc.)."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT OR IGNORE INTO external_links
               (doc_id, provider, kind, external_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (doc_id, provider, kind, external_id, now),
        )
        self.conn.commit()

    def get_external_links(
        self,
        doc_id: str | None = None,
        provider: str | None = None,
        kind: str | None = None,
        status: str = "active",
    ) -> list[dict]:
        """Look up external links, optionally filtered."""
        where = ["status = ?"]
        params: list = [status]

        if doc_id:
            where.append("doc_id = ?")
            params.append(doc_id)
        if provider:
            where.append("provider = ?")
            params.append(provider)
        if kind:
            where.append("kind = ?")
            params.append(kind)

        clause = " AND ".join(where)
        rows = self.conn.execute(
            f"SELECT * FROM external_links WHERE {clause}",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def log_api_usage(
        self,
        provider: str,
        model: str,
        operation: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        doc_id: str | None = None,
    ) -> None:
        """Log an API call's token usage and cost."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO api_usage
               (timestamp, provider, model, operation,
                input_tokens, output_tokens, cost_usd, doc_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, provider, model, operation, input_tokens, output_tokens, cost_usd, doc_id),
        )
        self.conn.commit()

    def get_api_usage_summary(self, days: int | None = None) -> dict:
        """Aggregate API usage statistics.

        If days is set, only include usage within the last N days.
        """
        where = ""
        params: list = []
        if days is not None and days > 0:
            where = "WHERE timestamp >= datetime('now', ?)"
            params.append(f"-{days} days")

        rows = self.conn.execute(
            f"""SELECT provider,
                       COUNT(*) as calls,
                       SUM(input_tokens) as input_tokens,
                       SUM(output_tokens) as output_tokens,
                       SUM(cost_usd) as cost_usd
                FROM api_usage {where}
                GROUP BY provider""",
            params,
        ).fetchall()

        providers: dict[str, dict] = {}
        total_cost = 0.0
        total_calls = 0
        for row in rows:
            providers[row["provider"]] = {
                "calls": row["calls"] or 0,
                "input_tokens": row["input_tokens"] or 0,
                "output_tokens": row["output_tokens"] or 0,
                "cost_usd": round(row["cost_usd"] or 0.0, 4),
            }
            total_cost += row["cost_usd"] or 0.0
            total_calls += row["calls"] or 0

        return {
            "providers": providers,
            "total_cost_usd": round(total_cost, 4),
            "total_calls": total_calls,
            "days": days,
        }

    # -- Reverse push queue --

    def enqueue_reverse_push(self, vault_path: str) -> bool:
        """Queue a vault note to be pushed to reMarkable. Returns True if newly queued."""
        now = datetime.now(UTC).isoformat()
        cursor = self.conn.execute(
            """INSERT OR IGNORE INTO reverse_push_queue
               (vault_path, queued_at, status) VALUES (?, ?, 'pending')""",
            (vault_path, now),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_reverse_queue(self, status: str = "pending") -> list[dict]:
        """Get entries from the reverse-push queue."""
        rows = self.conn.execute(
            "SELECT * FROM reverse_push_queue WHERE status = ? ORDER BY queued_at",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_reverse_pushed(self, vault_path: str, remarkable_doc_id: str) -> None:
        """Mark a queued push as successful."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """UPDATE reverse_push_queue
               SET status = 'pushed', remarkable_doc_id = ?, pushed_at = ?
               WHERE vault_path = ?""",
            (remarkable_doc_id, now, vault_path),
        )
        self.conn.commit()

    def mark_reverse_failed(self, vault_path: str, error: str) -> None:
        """Mark a queued push as failed with error message."""
        self.conn.execute(
            "UPDATE reverse_push_queue SET status = 'error', error = ? WHERE vault_path = ?",
            (error, vault_path),
        )
        self.conn.commit()

    # -- Web push subscriptions --

    def add_webpush_subscription(
        self, endpoint: str, p256dh: str, auth: str, user_agent: str = "",
    ) -> int:
        """Register a Web Push subscription. Returns the row ID."""
        now = datetime.now(UTC).isoformat()
        cursor = self.conn.execute(
            """INSERT OR REPLACE INTO webpush_subscriptions
               (endpoint, p256dh, auth, user_agent, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (endpoint, p256dh, auth, user_agent, now),
        )
        self.conn.commit()
        return cursor.lastrowid or 0

    def list_webpush_subscriptions(self) -> list[dict]:
        """Return all active subscriptions."""
        rows = self.conn.execute("SELECT * FROM webpush_subscriptions").fetchall()
        return [dict(r) for r in rows]

    def remove_webpush_subscription(self, endpoint: str) -> None:
        """Remove a subscription (e.g. after 410 Gone response)."""
        self.conn.execute(
            "DELETE FROM webpush_subscriptions WHERE endpoint = ?", (endpoint,),
        )
        self.conn.commit()

    # -- Plugin state --

    def register_plugin(self, name: str, config: str = "") -> None:
        """Record that a plugin has been loaded."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT OR IGNORE INTO plugin_state (name, config, installed_at)
               VALUES (?, ?, ?)""",
            (name, config, now),
        )
        self.conn.commit()

    def set_plugin_enabled(self, name: str, enabled: bool) -> None:
        """Enable or disable a plugin."""
        self.conn.execute(
            "UPDATE plugin_state SET enabled = ? WHERE name = ?",
            (1 if enabled else 0, name),
        )
        self.conn.commit()

    def list_plugins(self) -> list[dict]:
        """List all registered plugins."""
        rows = self.conn.execute("SELECT * FROM plugin_state").fetchall()
        return [dict(r) for r in rows]

    def is_plugin_enabled(self, name: str) -> bool:
        """Check if a plugin is enabled. Defaults to True if not yet registered."""
        row = self.conn.execute(
            "SELECT enabled FROM plugin_state WHERE name = ?", (name,),
        ).fetchone()
        if row is None:
            return True
        return bool(row["enabled"])

    # -- Template instances --

    def record_template_push(
        self, doc_id: str, template_name: str,
    ) -> None:
        """Record that a template was pushed to the tablet."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO template_instances
               (doc_id, template_name, pushed_at) VALUES (?, ?, ?)""",
            (doc_id, template_name, now),
        )
        self.conn.commit()

    def mark_template_filled(self, doc_id: str, vault_path: str) -> None:
        """Record that a template was filled on the tablet and extracted to vault."""
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """UPDATE template_instances
               SET filled_at = ?, vault_path = ? WHERE doc_id = ?""",
            (now, vault_path, doc_id),
        )
        self.conn.commit()

    def get_template_for_doc(self, doc_id: str) -> dict | None:
        """Look up whether a document was a pushed template."""
        row = self.conn.execute(
            "SELECT * FROM template_instances WHERE doc_id = ?", (doc_id,),
        ).fetchone()
        return dict(row) if row else None

    def mark_external_link_completed(self, provider: str, external_id: str) -> None:
        """Mark an external link as completed (e.g. task was marked done)."""
        self.conn.execute(
            "UPDATE external_links SET status = 'completed' "
            "WHERE provider = ? AND external_id = ?",
            (provider, external_id),
        )
        self.conn.commit()

    # -- Devices registry --

    def register_device(
        self,
        device_id: str,
        label: str,
        device_token_path: str,
        vault_subfolder: str = "",
    ) -> None:
        """Insert or update a device entry.

        ``device_id`` is expected to be a stable, user-chosen slug such as
        ``rm2`` or ``pro`` — it ends up in ``sync_state.device_id`` so it
        should stay short and shell-safe.
        """
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO devices
                 (id, label, vault_subfolder, device_token_path,
                  registered_at, active)
               VALUES (?, ?, ?, ?, ?, 1)
               ON CONFLICT(id) DO UPDATE SET
                 label = excluded.label,
                 vault_subfolder = excluded.vault_subfolder,
                 device_token_path = excluded.device_token_path,
                 active = 1""",
            (device_id, label, vault_subfolder, device_token_path, now),
        )
        self.conn.commit()
        self._log("device", None, f"registered {device_id} ({label})")

    def list_devices(self, active_only: bool = True) -> list[dict]:
        """Return registered devices, most recently used first."""
        sql = "SELECT * FROM devices"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY COALESCE(last_sync_at, registered_at) DESC"
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def get_device(self, device_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM devices WHERE id = ?", (device_id,),
        ).fetchone()
        return dict(row) if row else None

    def deactivate_device(self, device_id: str) -> None:
        """Soft-delete: set active=0 so historic sync rows keep their FK."""
        self.conn.execute(
            "UPDATE devices SET active = 0 WHERE id = ?", (device_id,),
        )
        self.conn.commit()
        self._log("device", None, f"deactivated {device_id}")

    def touch_device(self, device_id: str) -> None:
        """Record the current time as ``last_sync_at`` for a device."""
        self.conn.execute(
            "UPDATE devices SET last_sync_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), device_id),
        )
        self.conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
