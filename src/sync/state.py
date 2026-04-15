"""SQLite sync state database for idempotent processing.

Tracks which documents have been synced, their hashes, and processing
status so we never process the same version twice.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

-- Local user accounts for the web UI + per-user vault isolation.
-- Default deployment (pre-0.7) had a single implicit "admin" user;
-- the migration in _apply_migrations creates that row so existing
-- installs stay functional on first boot after upgrade.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',      -- 'admin' | 'user'
    vault_path TEXT,                         -- NULL = fall back to config.obsidian.vault_path
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(active);

-- Structured audit log — distinct from sync_log which tracks
-- technical sync events. Captures user-initiated actions (login,
-- settings change, API call) with request metadata.
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id INTEGER,
    username TEXT,
    action TEXT NOT NULL,
    resource TEXT,
    method TEXT,
    status INTEGER,
    ip TEXT,
    user_agent TEXT,
    details TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);

-- Scheduled reports: periodic Claude-powered summaries pushed to
-- configured output channels (Teams / Notion / Vault).
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    schedule TEXT NOT NULL,                 -- cron string
    prompt TEXT NOT NULL,                   -- what to ask Claude
    channels TEXT NOT NULL,                 -- JSON array: ["teams","notion","vault"]
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at TEXT,
    next_run_at TEXT,
    last_status TEXT,
    last_error TEXT,
    created_by INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_enabled ON reports(enabled, next_run_at);

-- Bearer tokens issued to external clients (the Obsidian companion
-- plugin, CLI scripts, etc.). Only the sha256 hash is stored so a
-- leaked state DB can't replay tokens. Plain-text is returned once on
-- issue; callers are responsible for copying it.
CREATE TABLE IF NOT EXISTS bridge_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked INTEGER NOT NULL DEFAULT 0
);

-- Offline / retry queue. Captures operations that failed transiently
-- (network, rate limit, token refresh) so a later sync cycle can pick
-- them up without losing state. Kept separate from reverse_push_queue
-- because the retry semantics differ: reverse_push is user-initiated,
-- sync_queue is system-owned graceful-degradation.
CREATE TABLE IF NOT EXISTS sync_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    op_type TEXT NOT NULL,
    doc_id TEXT,
    payload TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    created_at TEXT NOT NULL,
    next_attempt_at TEXT,
    last_error TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_queue_status
    ON sync_queue(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_sync_queue_doc ON sync_queue(doc_id);

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
        self._ensure_indexes()
        self.conn.commit()

    def _ensure_indexes(self) -> None:
        """Best-effort indexes for v0.6.5 hot paths.

        Added after-the-fact via ``IF NOT EXISTS`` so upgrades don't
        need a dedicated migration step. Dashboard widgets and the
        offline queue both do ``WHERE status = ?`` scans — covered now.
        """
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sync_state_status "
            "ON sync_state(status)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sync_state_synced_at "
            "ON sync_state(last_synced_at)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_external_links_provider_status "
            "ON external_links(provider, status)"
        )

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

        # v0.7.0 — per-user isolation. sync_state and devices grow a
        # user_id column that maps back to the new users table.
        # Pre-0.7 rows default to 1 (the implicit "admin" user seeded
        # below), preserving existing data during upgrade.
        if "user_id" not in cols:
            self.conn.execute(
                "ALTER TABLE sync_state ADD COLUMN user_id INTEGER DEFAULT 1"
            )
        device_cols = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(devices)").fetchall()
        }
        if device_cols and "user_id" not in device_cols:
            self.conn.execute(
                "ALTER TABLE devices ADD COLUMN user_id INTEGER DEFAULT 1"
            )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sync_state_user ON sync_state(user_id)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id)"
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
        user_id: int = 1,
    ) -> None:
        """Record a successful sync for a document."""
        now = datetime.now(UTC).isoformat()

        self.conn.execute(
            """INSERT INTO sync_state
               (doc_id, doc_name, parent_folder, cloud_hash, local_hash,
                version, last_synced_at, vault_path, ocr_engine,
                page_count, action_count, status, device_id, user_id)
               VALUES (?, ?, ?, ?, ?,
                       COALESCE((SELECT version FROM sync_state WHERE doc_id = ?), 0) + 1,
                       ?, ?, ?, ?, ?, 'synced', ?, ?)
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
                 device_id = excluded.device_id,
                 user_id = excluded.user_id
            """,
            (doc_id, doc_name, parent_folder, cloud_hash, cloud_hash,
             doc_id, now, vault_path, ocr_engine, page_count, action_count,
             device_id, user_id),
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

    def list_synced(
        self,
        folder: str | None = None,
        query: str | None = None,
        limit: int = 200,
        user_id: int | None = None,
    ) -> list[dict]:
        """Server-side filter over sync_state for the /notes list view.

        Avoids walking the whole vault and parsing every frontmatter
        block on each filter keystroke. ``folder`` filters by the
        leading path component stored in ``parent_folder``; ``query``
        does a case-insensitive LIKE against doc_name; ``user_id``
        scopes the rows when multi-user is in play.
        """
        where = ["status IN ('synced', 'pending_response')"]
        params: list = []
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        if folder:
            where.append("(parent_folder = ? OR parent_folder LIKE ?)")
            params.extend([folder, folder + "/%"])
        if query:
            where.append("LOWER(doc_name) LIKE ?")
            params.append("%" + query.lower() + "%")
        clause = " AND ".join(where)
        rows = self.conn.execute(
            f"""SELECT doc_id, doc_name, parent_folder, vault_path,
                       page_count, action_count, last_synced_at
                FROM sync_state
                WHERE {clause}
                ORDER BY doc_name ASC
                LIMIT ?""",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_synced(
        self, limit: int = 10, user_id: int | None = None,
    ) -> list[dict]:
        """Return the N most-recently-synced documents.

        Backs the dashboard "Recent notes" widget. Sourcing from the
        state DB avoids a full-vault ``rglob`` + frontmatter read on
        every request — cheap constant-time query instead. Pass
        ``user_id`` to scope multi-user dashboards.
        """
        where = "status IN ('synced', 'pending_response')"
        params: list = []
        if user_id is not None:
            where += " AND user_id = ?"
            params.append(user_id)
        params.append(limit)
        rows = self.conn.execute(
            f"""SELECT doc_id, doc_name, parent_folder, vault_path,
                      last_synced_at, page_count, action_count
               FROM sync_state
               WHERE {where}
               ORDER BY last_synced_at DESC
               LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]

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

    # -- Users (v0.7+ multi-user web auth) --

    def create_user(
        self,
        username: str,
        password_hash: str,
        role: str = "user",
        vault_path: str | None = None,
    ) -> int:
        """Insert a user row. Caller hashes the password with passlib
        before calling — we never see the plaintext.
        """
        now = datetime.now(UTC).isoformat()
        cur = self.conn.execute(
            """INSERT INTO users
                 (username, password_hash, role, vault_path, active, created_at)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (username, password_hash, role, vault_path, now),
        )
        self.conn.commit()
        self._log("user", None, f"created user '{username}' ({role})")
        return cur.lastrowid or 0

    def get_user(self, username: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,),
        ).fetchone()
        return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,),
        ).fetchone()
        return dict(row) if row else None

    def list_users(self, active_only: bool = False) -> list[dict]:
        sql = "SELECT id, username, role, vault_path, active, created_at, last_login_at FROM users"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY id ASC"
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def touch_user_login(self, user_id: int) -> None:
        self.conn.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), user_id),
        )
        self.conn.commit()

    def set_user_active(self, user_id: int, active: bool) -> None:
        self.conn.execute(
            "UPDATE users SET active = ? WHERE id = ?",
            (1 if active else 0, user_id),
        )
        self.conn.commit()

    def set_user_password(self, user_id: int, password_hash: str) -> None:
        self.conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )
        self.conn.commit()
        self._log("user", None, f"password changed for user_id={user_id}")

    def ensure_default_admin(self, password_hash: str) -> int | None:
        """Seed a fallback admin on first boot if the users table is
        empty. Returns the new user_id, or None if the table already
        has rows.
        """
        row = self.conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        if row and row["n"] > 0:
            return None
        return self.create_user("admin", password_hash, role="admin")

    # -- Audit log --

    def audit(
        self,
        action: str,
        user_id: int | None = None,
        username: str | None = None,
        resource: str | None = None,
        method: str | None = None,
        status: int | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        details: str | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO audit_log
                 (ts, user_id, username, action, resource, method,
                  status, ip, user_agent, details)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                datetime.now(UTC).isoformat(), user_id, username, action,
                resource, method, status, ip, (user_agent or "")[:255], details,
            ),
        )
        self.conn.commit()

    def list_audit(
        self,
        limit: int = 100,
        offset: int = 0,
        user_id: int | None = None,
        action: str | None = None,
        since: str | None = None,
    ) -> list[dict]:
        where = ["1=1"]
        params: list = []
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        if action:
            where.append("action = ?")
            params.append(action)
        if since:
            where.append("ts >= ?")
            params.append(since)
        clause = " AND ".join(where)
        rows = self.conn.execute(
            f"""SELECT * FROM audit_log
                WHERE {clause}
                ORDER BY id DESC
                LIMIT ? OFFSET ?""",
            (*params, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]

    def audit_prune(self, retention_days: int) -> int:
        cutoff = (
            datetime.now(UTC) - timedelta(days=retention_days)
        ).isoformat()
        cur = self.conn.execute(
            "DELETE FROM audit_log WHERE ts < ?", (cutoff,),
        )
        self.conn.commit()
        return cur.rowcount or 0

    # -- Bridge tokens (bearer auth for external clients) --

    def issue_bridge_token(self, label: str) -> str:
        """Mint a new bearer token and return the plain-text value.

        Only the sha256 hash lands in the DB. The plain value is shown
        once so the caller can copy it into the Obsidian plugin or a
        CI secret store.
        """
        import hashlib
        import secrets

        now = datetime.now(UTC).isoformat()
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

        self.conn.execute(
            """INSERT INTO bridge_tokens
                 (label, token_hash, created_at, revoked)
               VALUES (?, ?, ?, 0)""",
            (label, token_hash, now),
        )
        self.conn.commit()
        self._log("token", None, f"issued bridge token '{label}'")
        return token

    def verify_bridge_token(self, token: str) -> str | None:
        """Return the matching label if ``token`` is active, else ``None``.

        The comparison deliberately runs in Python with
        ``secrets.compare_digest`` against every non-revoked token hash,
        rather than letting SQLite short-circuit a ``WHERE token_hash = ?``
        byte-by-byte. That closes the timing side channel an attacker
        could otherwise use to enumerate valid prefixes.
        """
        import hashlib
        import secrets

        if not token:
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

        rows = self.conn.execute(
            "SELECT id, label, token_hash FROM bridge_tokens "
            "WHERE revoked = 0"
        ).fetchall()

        match_id: int | None = None
        match_label: str | None = None
        for row in rows:
            if secrets.compare_digest(token_hash, row["token_hash"]):
                match_id = row["id"]
                match_label = row["label"]
                # Don't break — always compare against every row so the
                # response time is a function of the total number of
                # tokens, not of which one matched.

        if match_id is None:
            return None

        self.conn.execute(
            "UPDATE bridge_tokens SET last_used_at = ? WHERE id = ?",
            (datetime.now(UTC).isoformat(), match_id),
        )
        self.conn.commit()
        return match_label

    def list_bridge_tokens(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, label, created_at, last_used_at, revoked "
            "FROM bridge_tokens ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def revoke_bridge_token(self, token_id: int) -> None:
        self.conn.execute(
            "UPDATE bridge_tokens SET revoked = 1 WHERE id = ?",
            (token_id,),
        )
        self.conn.commit()
        self._log("token", None, f"revoked bridge token id={token_id}")

    # -- Sync queue (offline / retry) --

    def enqueue(
        self,
        op_type: str,
        doc_id: str | None = None,
        payload: str = "",
        priority: int = 0,
        max_attempts: int = 5,
    ) -> int:
        """Queue an operation for later retry.

        ``op_type`` is a free-form short string (``"process_document"``,
        ``"push_response"``, ``"index"``). Returns the queue row id so
        callers can correlate logs.
        """
        now = datetime.now(UTC).isoformat()
        cur = self.conn.execute(
            """INSERT INTO sync_queue
                 (op_type, doc_id, payload, priority, status,
                  attempts, max_attempts, created_at, next_attempt_at)
               VALUES (?, ?, ?, ?, 'pending', 0, ?, ?, ?)""",
            (op_type, doc_id, payload, priority, max_attempts, now, now),
        )
        self.conn.commit()
        self._log("queue", doc_id, f"enqueued {op_type} (id={cur.lastrowid})")
        return cur.lastrowid or 0

    def dequeue_ready(self, limit: int = 20) -> list[dict]:
        """Return pending items whose ``next_attempt_at`` is due.

        Rows stay in ``pending`` status — callers mark them ``done`` or
        ``failed`` explicitly via :meth:`mark_queue_done` and
        :meth:`mark_queue_failed` once the retry is actually attempted.
        """
        now = datetime.now(UTC).isoformat()
        rows = self.conn.execute(
            """SELECT * FROM sync_queue
               WHERE status = 'pending'
                 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
               ORDER BY priority DESC, id ASC
               LIMIT ?""",
            (now, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_queue(self, status: str | None = None) -> list[dict]:
        """Return queue entries — for the CLI and web widgets."""
        if status:
            rows = self.conn.execute(
                "SELECT * FROM sync_queue WHERE status = ? "
                "ORDER BY id DESC LIMIT 200",
                (status,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM sync_queue ORDER BY id DESC LIMIT 200"
            ).fetchall()
        return [dict(r) for r in rows]

    def queue_summary(self) -> dict[str, int]:
        """Counts by status for dashboard widgets."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM sync_queue GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def mark_queue_done(self, queue_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            "UPDATE sync_queue SET status='done', completed_at=? WHERE id=?",
            (now, queue_id),
        )
        self.conn.commit()

    def mark_queue_failed(self, queue_id: int, error: str) -> None:
        """Bump attempts, back off, mark ``failed`` when the cap is hit.

        Back-off is ``5 ** attempts`` minutes, so the first retry waits
        5 min, the second 25 min, the third ~2 h, capped at 6 h. Short
        enough to recover from a Cloud-side blip, long enough not to
        thrash the state DB.
        """
        row = self.conn.execute(
            "SELECT attempts, max_attempts FROM sync_queue WHERE id=?",
            (queue_id,),
        ).fetchone()
        if row is None:
            return

        attempts = int(row["attempts"]) + 1
        max_attempts = int(row["max_attempts"])
        status = "failed" if attempts >= max_attempts else "pending"

        backoff_sec = min(5 ** attempts * 60, 6 * 3600)
        next_at = datetime.now(UTC).timestamp() + backoff_sec
        next_iso = datetime.fromtimestamp(next_at, tz=UTC).isoformat()

        self.conn.execute(
            """UPDATE sync_queue
               SET attempts = ?,
                   status = ?,
                   next_attempt_at = ?,
                   last_error = ?
               WHERE id = ?""",
            (attempts, status, next_iso, error[:500], queue_id),
        )
        self.conn.commit()

    def retry_queue_entry(self, queue_id: int) -> None:
        """Reset a failed entry to ``pending`` so the next cycle picks it up."""
        self.conn.execute(
            """UPDATE sync_queue
               SET status = 'pending', attempts = 0, next_attempt_at = NULL,
                   last_error = NULL
               WHERE id = ?""",
            (queue_id,),
        )
        self.conn.commit()

    def clear_queue(self, status: str | None = None) -> int:
        """Delete queue rows. Returns number removed."""
        if status:
            cur = self.conn.execute(
                "DELETE FROM sync_queue WHERE status = ?", (status,),
            )
        else:
            cur = self.conn.execute("DELETE FROM sync_queue")
        self.conn.commit()
        return cur.rowcount or 0

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

    def list_devices(
        self, active_only: bool = True, user_id: int | None = None,
    ) -> list[dict]:
        """Return registered devices, most recently used first.

        ``user_id`` scopes the result to devices owned by a specific
        user; leave ``None`` for CLI / admin contexts that should see
        every registered tablet.
        """
        where: list[str] = []
        params: list = []
        if active_only:
            where.append("active = 1")
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        sql = "SELECT * FROM devices"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(last_sync_at, registered_at) DESC"
        return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

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
        # The web layer keeps a process-wide SyncState on app.state and
        # sets ``_shared=True`` on it so route handlers that `finally:
        # state.close()` don't tear down the shared connection. CLI and
        # tests leave ``_shared`` unset and behave as before.
        if getattr(self, "_shared", False):
            return
        if self._conn:
            self._conn.close()
            self._conn = None
