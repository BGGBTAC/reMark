"""Bridge API: per-note sync status and preview."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.config import AppConfig
from src.sync.state import SyncState
from src.web.app import create_app


def _make_env(tmp_path, token_label="test-client"):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Inbox").mkdir()
    (vault / "Inbox" / "Note.md").write_text("# hello", encoding="utf-8")

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cfg = {
        "sync": {"state_db": str(state_dir / "state.db")},
        "obsidian": {"vault_path": str(vault)},
    }

    app = create_app(AppConfig(**cfg))
    state = SyncState(state_dir / "state.db")
    token = state.issue_bridge_token(token_label)
    state.close()

    # Return the app's own shared state so tests can seed rows directly.
    return TestClient(app), app.state.sync_state, token


def _seed_sync_state(state: SyncState, vault_path: str, device_id: str = "default") -> None:
    """Insert a synced row into sync_state for the test vault path."""
    state.conn.execute(
        "INSERT OR REPLACE INTO sync_state "
        "(doc_id, vault_path, device_id, last_synced_at, status) "
        "VALUES (?, ?, ?, ?, ?)",
        ("doc-abc", vault_path, device_id, str(int(time.time())), "synced"),
    )
    state.conn.commit()


# ---------------------------------------------------------------------------
# D1: GET /api/notes/{vault_path}/status
# ---------------------------------------------------------------------------

class TestNoteStatus:
    def test_returns_metadata(self, tmp_path):
        client, state, token = _make_env(tmp_path)
        _seed_sync_state(state, "Inbox/Note.md")

        resp = client.get(
            "/api/notes/Inbox/Note.md/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["vault_path"] == "Inbox/Note.md"
        assert data["synced_at"] is not None
        assert data["device_id"] == "default"
        assert "pending_push" in data
        assert "last_error" in data

    def test_404_for_unknown_note(self, tmp_path):
        client, _state, token = _make_env(tmp_path)
        resp = client.get(
            "/api/notes/Nonexistent.md/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404

    def test_rejects_missing_bearer(self, tmp_path):
        client, _, _token = _make_env(tmp_path)
        resp = client.get("/api/notes/any.md/status")
        assert resp.status_code in (401, 403)

    def test_rejects_invalid_bearer(self, tmp_path):
        client, _, _token = _make_env(tmp_path)
        resp = client.get(
            "/api/notes/Inbox/Note.md/status",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert resp.status_code == 401

    def test_rejects_path_traversal(self, tmp_path):
        client, _state, token = _make_env(tmp_path)
        resp = client.get(
            "/api/notes/..%2F..%2Fetc%2Fpasswd/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        # Either 400 (path traversal blocked) or 404 (no row matched)
        assert resp.status_code in (400, 404)
