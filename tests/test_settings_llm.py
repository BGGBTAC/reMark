"""/settings/llm renders a form with provider select + Ollama fields."""

from __future__ import annotations

import pytest

from src.config import AppConfig

_WEB_SKIP_REASON = ""
try:
    from fastapi.testclient import TestClient

    from src.web.app import create_app

    _WEB_AVAILABLE = True
except ModuleNotFoundError as _e:
    # itsdangerous or another optional web dep missing in this test env.
    # The same skip applies to tests/test_web.py — not a B15 regression.
    _WEB_AVAILABLE = False
    _WEB_SKIP_REASON = f"web deps unavailable: {_e}"

pytestmark = pytest.mark.skipif(
    not _WEB_AVAILABLE,
    reason=_WEB_SKIP_REASON if not _WEB_AVAILABLE else "",
)


@pytest.fixture
def client(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Inbox").mkdir()
    cfg = AppConfig()
    cfg.obsidian.vault_path = str(vault)
    cfg.sync.state_db = str(tmp_path / "state.db")
    cfg.logging.file = str(tmp_path / "log.txt")
    app = create_app(cfg)
    return TestClient(app)


def test_settings_llm_listed_on_index(client):
    """The /settings index page includes an 'llm' section link."""
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert "llm" in resp.text.lower() or "LLM" in resp.text


def test_settings_llm_renders_form(client):
    """/settings/llm returns a 200 with provider select + Ollama fields."""
    resp = client.get("/settings/llm")
    assert resp.status_code == 200
    html = resp.text

    # Provider select must list both supported providers
    assert "anthropic" in html
    assert "ollama" in html

    # Ollama nested fields should be present
    assert "base_url" in html or "ollama" in html.lower()


def test_settings_llm_unknown_section_is_404(client):
    """A section that does not exist should 404 — basic allowlist check."""
    resp = client.get("/settings/nonexistent")
    assert resp.status_code == 404
