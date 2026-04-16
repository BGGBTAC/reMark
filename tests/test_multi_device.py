"""Tests for the multi-device registry and state migration."""

from __future__ import annotations

import sqlite3

import pytest

from src.remarkable.auth import device_token_path_for
from src.sync.state import SyncState


@pytest.fixture
def state(tmp_path):
    db = tmp_path / "state.db"
    s = SyncState(db)
    yield s
    s.close()


class TestDeviceTokenPath:
    def test_default_device_keeps_legacy_path(self, tmp_path):
        path = device_token_path_for("default", tmp_path)
        assert path == tmp_path / "device_token"

    def test_named_device_isolated_dir(self, tmp_path):
        path = device_token_path_for("pro", tmp_path)
        assert path == tmp_path / "devices" / "pro" / "device_token"

    def test_two_devices_dont_collide(self, tmp_path):
        a = device_token_path_for("rm2", tmp_path)
        b = device_token_path_for("pro", tmp_path)
        assert a != b
        assert a.parent != b.parent


class TestDevicesTable:
    def test_register_device_inserts_row(self, state):
        state.register_device("pro", "Paper Pro", "/tmp/tok", "rm-pro")
        rows = state.list_devices()
        assert len(rows) == 1
        assert rows[0]["id"] == "pro"
        assert rows[0]["label"] == "Paper Pro"
        assert rows[0]["vault_subfolder"] == "rm-pro"
        assert rows[0]["active"] == 1

    def test_register_device_is_idempotent(self, state):
        state.register_device("pro", "Paper Pro", "/tmp/tok", "rm-pro")
        state.register_device("pro", "Paper Pro Renamed", "/tmp/tok2", "rm-pro-v2")
        rows = state.list_devices()
        assert len(rows) == 1
        assert rows[0]["label"] == "Paper Pro Renamed"
        assert rows[0]["vault_subfolder"] == "rm-pro-v2"

    def test_deactivate_hides_from_default_list(self, state):
        state.register_device("rm2", "reMarkable 2", "/tmp/rm2", "")
        state.deactivate_device("rm2")
        assert state.list_devices(active_only=True) == []
        assert len(state.list_devices(active_only=False)) == 1

    def test_touch_updates_last_sync(self, state):
        state.register_device("pro", "Pro", "/tmp/p", "")
        assert state.get_device("pro")["last_sync_at"] is None
        state.touch_device("pro")
        assert state.get_device("pro")["last_sync_at"] is not None


class TestSyncStateDeviceColumn:
    def test_mark_synced_stores_device_id(self, state):
        state.mark_synced(
            doc_id="doc-1",
            doc_name="Meeting",
            parent_folder="Meetings",
            cloud_hash="h1",
            vault_path="/v/meeting.md",
            ocr_engine="builtin",
            page_count=2,
            action_count=1,
            device_id="pro",
        )
        row = state.conn.execute(
            "SELECT device_id FROM sync_state WHERE doc_id = ?",
            ("doc-1",),
        ).fetchone()
        assert row["device_id"] == "pro"

    def test_mark_synced_default_device_when_omitted(self, state):
        state.mark_synced(
            doc_id="doc-2",
            doc_name="Journal",
            parent_folder="Daily",
            cloud_hash="h2",
            vault_path="/v/journal.md",
            ocr_engine="builtin",
            page_count=1,
            action_count=0,
        )
        row = state.conn.execute(
            "SELECT device_id FROM sync_state WHERE doc_id = ?",
            ("doc-2",),
        ).fetchone()
        assert row["device_id"] == "default"


class TestLegacyMigration:
    def test_pre_device_id_column_is_added(self, tmp_path):
        """An old 0.3.x database without device_id must gain the column."""
        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            """CREATE TABLE sync_state (
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
            )"""
        )
        conn.execute(
            "INSERT INTO sync_state (doc_id, doc_name, cloud_hash) VALUES (?, ?, ?)",
            ("legacy-doc", "Old note", "h-old"),
        )
        conn.commit()
        conn.close()

        # Open through SyncState — migration should add device_id
        state = SyncState(db)
        try:
            cols = {
                row["name"]
                for row in state.conn.execute("PRAGMA table_info(sync_state)").fetchall()
            }
            assert "device_id" in cols
            row = state.conn.execute(
                "SELECT device_id FROM sync_state WHERE doc_id = ?",
                ("legacy-doc",),
            ).fetchone()
            assert row["device_id"] == "default"
        finally:
            state.close()
