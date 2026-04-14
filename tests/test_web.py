"""Tests for the web dashboard + PWA."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.config import AppConfig
from src.web.app import create_app
from src.web.push import generate_vapid_keys


@pytest.fixture
def vault_path(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Inbox").mkdir()
    return vault


@pytest.fixture
def config(tmp_path, vault_path):
    cfg = AppConfig()
    cfg.obsidian.vault_path = str(vault_path)
    cfg.sync.state_db = str(tmp_path / "state.db")
    cfg.logging.file = str(tmp_path / "log.txt")
    cfg.web.app_name = "reMark Test"
    return cfg


@pytest.fixture
def client(config):
    app = create_app(config)
    return TestClient(app)


def _write_note(vault_path: Path, folder: str, name: str, title: str, body: str,
                **frontmatter):
    fm = {"title": title, "source": "remarkable", **frontmatter}
    (vault_path / folder).mkdir(exist_ok=True, parents=True)
    path = vault_path / folder / f"{name}.md"

    import yaml
    fm_str = yaml.dump(fm, default_flow_style=False, sort_keys=False)
    path.write_text(f"---\n{fm_str}---\n\n{body}\n", encoding="utf-8")
    return path


# =====================
# Routes
# =====================

class TestRoutes:
    def test_dashboard_renders(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Dashboard" in resp.text
        assert "reMark Test" in resp.text

    def test_notes_empty(self, client):
        resp = client.get("/notes")
        assert resp.status_code == 200
        assert "No notes match" in resp.text

    def test_notes_lists_synced(self, client, vault_path):
        _write_note(vault_path, "Inbox", "sample", "Sample Note",
                    "# Sample\n\nContent here")
        resp = client.get("/notes")
        assert resp.status_code == 200
        assert "Sample Note" in resp.text

    def test_notes_filter_query(self, client, vault_path):
        _write_note(vault_path, "Inbox", "alpha", "Alpha",
                    "Content about apples")
        _write_note(vault_path, "Inbox", "beta", "Beta",
                    "Content about bananas")

        resp = client.get("/notes?q=apples")
        assert resp.status_code == 200
        assert "Alpha" in resp.text
        assert "Beta" not in resp.text

    def test_view_note(self, client, vault_path):
        _write_note(vault_path, "Inbox", "detail", "Detail",
                    "Body content")
        resp = client.get("/notes/Inbox/detail.md")
        assert resp.status_code == 200
        assert "Detail" in resp.text
        assert "Body content" in resp.text

    def test_view_note_missing(self, client):
        resp = client.get("/notes/nope.md")
        assert resp.status_code == 404

    def test_actions_empty(self, client):
        resp = client.get("/actions")
        assert resp.status_code == 200
        assert "No open action items" in resp.text

    def test_actions_lists(self, client, vault_path):
        actions_dir = vault_path / "Actions"
        actions_dir.mkdir()
        (actions_dir / "meeting-actions.md").write_text(
            "# Actions\n\n- [ ] Do the thing\n- [?] Ask something\n",
            encoding="utf-8",
        )
        resp = client.get("/actions")
        assert resp.status_code == 200
        assert "Do the thing" in resp.text
        assert "Ask something" in resp.text

    def test_ask_get_shows_disabled(self, client):
        resp = client.get("/ask")
        assert resp.status_code == 200
        assert "disabled" in resp.text.lower() or "Ask your notes" in resp.text

    def test_ask_post_disabled(self, client):
        resp = client.post("/ask", data={"query": "anything"})
        assert resp.status_code == 200
        assert "disabled" in resp.text.lower()

    def test_quick_entry_get(self, client):
        resp = client.get("/quick-entry")
        assert resp.status_code == 200
        assert "Content" in resp.text

    def test_quick_entry_creates_note(self, client, vault_path):
        resp = client.post(
            "/quick-entry",
            data={"title": "From Web", "body": "Quick body"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/notes"

        inbox_files = list((vault_path / "Inbox").glob("*.md"))
        assert len(inbox_files) == 1
        content = inbox_files[0].read_text()
        assert "Quick body" in content
        assert "source: web" in content

    def test_quick_entry_default_title(self, client, vault_path):
        resp = client.post(
            "/quick-entry",
            data={"body": "no title"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_settings_renders(self, client):
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert "Settings" in resp.text

    def test_health(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# =====================
# PWA endpoints
# =====================

class TestPWA:
    def test_manifest(self, client):
        resp = client.get("/manifest.webmanifest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "reMark Test"
        assert data["display"] == "standalone"
        assert len(data["icons"]) == 2

    def test_service_worker(self, client):
        resp = client.get("/service-worker.js")
        assert resp.status_code == 200
        assert "service worker" in resp.text.lower() or "caches" in resp.text

    def test_vapid_public_key_empty(self, client):
        resp = client.get("/vapid-public-key")
        assert resp.status_code == 200
        assert resp.json() == {"key": ""}

    def test_webpush_subscribe_requires_fields(self, client):
        resp = client.post("/webpush/subscribe", json={})
        assert resp.status_code == 400

    def test_webpush_subscribe_success(self, client):
        resp = client.post("/webpush/subscribe", json={
            "endpoint": "https://push.example/abc",
            "keys": {"p256dh": "PUB", "auth": "AUTH"},
        })
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


# =====================
# Auth
# =====================

class TestAuth:
    def test_auth_enabled_rejects_without_creds(self, config):
        config.web.username = "admin"
        config.web.password = "secret"
        client = TestClient(create_app(config))
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 401

    def test_auth_enabled_accepts_valid(self, config):
        config.web.username = "admin"
        config.web.password = "secret"
        client = TestClient(create_app(config))
        resp = client.get("/", auth=("admin", "secret"))
        assert resp.status_code == 200

    def test_auth_enabled_rejects_wrong_pw(self, config):
        config.web.username = "admin"
        config.web.password = "secret"
        client = TestClient(create_app(config))
        resp = client.get("/", auth=("admin", "wrong"))
        assert resp.status_code == 401


# =====================
# Push helper
# =====================

class TestPushHelper:
    def test_generate_vapid_keys(self):
        pub, priv = generate_vapid_keys()
        assert len(pub) > 40
        assert len(priv) > 30
        assert pub != priv

    def test_send_push_no_keys(self, tmp_path):
        from src.config import WebConfig
        from src.sync.state import SyncState
        from src.web.push import send_push

        state = SyncState(tmp_path / "p.db")
        cfg = WebConfig(vapid_public_key="", vapid_private_key="")
        count = send_push(cfg, state, "title", "body")
        assert count == 0
        state.close()
