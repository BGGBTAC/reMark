"""Tests for the bridge bearer-token auth + /api/* endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from src.config import AppConfig
from src.sync.state import SyncState
from src.web.app import create_app


@pytest.fixture
def env(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# hello", encoding="utf-8")

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cfg = {
        "sync": {"state_db": str(state_dir / "state.db")},
        "obsidian": {"vault_path": str(vault)},
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))

    app = create_app(AppConfig(**cfg))

    state = SyncState(state_dir / "state.db")
    token = state.issue_bridge_token("test-client")
    state.close()

    return TestClient(app), token, vault


class TestBridgeAuth:
    def test_missing_header_returns_401(self, env):
        client, _, _ = env
        resp = client.get("/api/status")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate") == "Bearer"

    def test_wrong_scheme_returns_401(self, env):
        client, token, _ = env
        resp = client.get(
            "/api/status",
            headers={"Authorization": f"Basic {token}"},
        )
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self, env):
        client, _, _ = env
        resp = client.get(
            "/api/status",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert resp.status_code == 401

    def test_valid_token_returns_200(self, env):
        client, token, _ = env
        resp = client.get(
            "/api/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["client"] == "test-client"
        assert "sync" in body
        assert "queue" in body

    def test_revoked_token_stops_working(self, env, tmp_path):
        client, token, _ = env

        # Revoke via the state DB directly to keep the test hermetic.
        state = SyncState((tmp_path / "state" / "state.db"))
        try:
            row_id = state.list_bridge_tokens()[0]["id"]
            state.revoke_bridge_token(row_id)
        finally:
            state.close()

        resp = client.get(
            "/api/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401


class TestApiPush:
    def test_rejects_missing_path(self, env):
        client, token, _ = env
        resp = client.post(
            "/api/push",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    def test_rejects_path_traversal(self, env):
        client, token, _ = env
        resp = client.post(
            "/api/push",
            json={"vault_path": "../../etc/passwd"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    def test_queues_valid_path(self, env, tmp_path):
        client, token, _ = env
        resp = client.post(
            "/api/push",
            json={"vault_path": "note.md"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["queued"] is True
        assert body["vault_path"] == "note.md"

    def test_missing_file_returns_404(self, env):
        client, token, _ = env
        resp = client.post(
            "/api/push",
            json={"vault_path": "does-not-exist.md"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 404


class TestTokenLifecycle:
    def test_issue_returns_plaintext_once(self, tmp_path):
        state = SyncState(tmp_path / "s.db")
        try:
            t1 = state.issue_bridge_token("a")
            t2 = state.issue_bridge_token("b")
            assert t1 != t2
            assert len(t1) >= 32
            # DB only carries hashes, never plain values
            rows = state.conn.execute(
                "SELECT token_hash FROM bridge_tokens"
            ).fetchall()
            hashes = [r["token_hash"] for r in rows]
            assert t1 not in hashes
            assert t2 not in hashes
        finally:
            state.close()

    def test_verify_bumps_last_used(self, tmp_path):
        state = SyncState(tmp_path / "s.db")
        try:
            token = state.issue_bridge_token("x")
            assert state.verify_bridge_token(token) == "x"
            row = state.list_bridge_tokens()[0]
            assert row["last_used_at"] is not None
        finally:
            state.close()

    def test_empty_token_is_rejected(self, tmp_path):
        state = SyncState(tmp_path / "s.db")
        try:
            assert state.verify_bridge_token("") is None
        finally:
            state.close()
