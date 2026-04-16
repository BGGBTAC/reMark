"""Bridge API: POST /api/search."""
from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from src.config import AppConfig
from src.sync.state import SyncState
from src.web.app import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def bridge_token(tmp_path):
    state = SyncState(tmp_path / "s.db")
    token = state.issue_bridge_token("test-search-client")
    state.close()
    return token


class _FakeSearchQuery:
    """Returns deterministic hits without touching any embedding backend."""

    async def ask(self, query: str, mode: str, top_k: int, synthesize: bool = False):
        from dataclasses import dataclass

        @dataclass
        class _Hit:
            vault_path: str
            content: str
            distance: float

            @property
            def score(self) -> float:
                return max(0.0, 1.0 - self.distance / 2.0)

        # Return up to min(top_k, 3) canned hits so limit enforcement is testable.
        n = min(top_k, 3)
        return type("R", (), {
            "hits": [
                _Hit(
                    vault_path=f"{query}-{i}.md",
                    content="...matcha powder and oat milk...",
                    distance=i * 0.1,
                )
                for i in range(n)
            ],
        })()


@pytest.fixture
def bridge_client_with_search(tmp_path, bridge_token):
    vault = tmp_path / "vault"
    vault.mkdir()

    state_dir = tmp_path / "state"
    state_dir.mkdir()

    state = SyncState(state_dir / "state.db")
    # Re-issue with the exact token string — we need to register it in this DB.
    # (bridge_token fixture used a different tmp_path; build a fresh one here.)
    token = state.issue_bridge_token("test-search-client")
    state.close()

    cfg = {
        "sync": {"state_db": str(state_dir / "state.db")},
        "obsidian": {"vault_path": str(vault)},
    }
    app = create_app(AppConfig(**cfg))
    app.state.search_query = _FakeSearchQuery()

    return TestClient(app), token


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestApiSearch:
    def test_returns_hits(self, bridge_client_with_search):
        client, token = bridge_client_with_search
        resp = client.post(
            "/api/search",
            json={"query": "matcha", "mode": "bm25", "limit": 5},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "hits" in data
        assert len(data["hits"]) <= 5
        for hit in data["hits"]:
            assert "path" in hit
            assert "snippet" in hit
            assert "score" in hit

    def test_respects_limit(self, bridge_client_with_search):
        client, token = bridge_client_with_search
        resp = client.post(
            "/api/search",
            json={"query": "matcha", "mode": "hybrid", "limit": 2},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["hits"]) <= 2

    def test_rejects_unknown_mode(self, bridge_client_with_search):
        client, token = bridge_client_with_search
        resp = client.post(
            "/api/search",
            json={"query": "x", "mode": "nope", "limit": 5},
            headers={"Authorization": f"Bearer {token}"},
        )
        # Pydantic Literal validation → 422
        assert resp.status_code in (400, 422)

    def test_rejects_empty_query(self, bridge_client_with_search):
        client, token = bridge_client_with_search
        resp = client.post(
            "/api/search",
            json={"query": "", "mode": "bm25", "limit": 5},
            headers={"Authorization": f"Bearer {token}"},
        )
        # Pydantic min_length=1 → 422
        assert resp.status_code in (400, 422)

    def test_requires_bearer(self, bridge_client_with_search):
        client, _token = bridge_client_with_search
        resp = client.post(
            "/api/search",
            json={"query": "x", "mode": "semantic", "limit": 5},
        )
        assert resp.status_code in (401, 403)

    def test_invalid_token_rejected(self, bridge_client_with_search):
        client, _token = bridge_client_with_search
        resp = client.post(
            "/api/search",
            json={"query": "x", "mode": "bm25", "limit": 5},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_search_query_attribute_always_set_by_create_app(self, tmp_path):
        """create_app always sets app.state.search_query (None or a real instance)."""
        vault = tmp_path / "vault"
        vault.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        cfg = {
            "sync": {"state_db": str(state_dir / "state.db")},
            "obsidian": {"vault_path": str(vault)},
            # search.enabled defaults to False — search_query should be None
        }
        app = create_app(AppConfig(**cfg))
        # The attribute must exist regardless of whether search is configured.
        assert hasattr(app.state, "search_query")
        # No backend configured → None
        assert app.state.search_query is None

    def test_search_query_none_when_search_disabled(self, tmp_path):
        """search_query stays None when search.enabled is False."""
        vault = tmp_path / "vault"
        vault.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        cfg = {
            "sync": {"state_db": str(state_dir / "state.db")},
            "obsidian": {"vault_path": str(vault)},
            "search": {"enabled": False},
        }
        app = create_app(AppConfig(**cfg))
        assert app.state.search_query is None

    def test_503_when_search_not_configured(self, tmp_path):
        """Without a search_query on app.state, endpoint returns 503."""
        vault = tmp_path / "vault"
        vault.mkdir()
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        state = SyncState(state_dir / "state.db")
        token = state.issue_bridge_token("no-search-client")
        state.close()

        cfg = {
            "sync": {"state_db": str(state_dir / "state.db")},
            "obsidian": {"vault_path": str(vault)},
        }
        app = create_app(AppConfig(**cfg))
        # Explicitly do NOT set app.state.search_query

        client = TestClient(app)
        resp = client.post(
            "/api/search",
            json={"query": "matcha", "mode": "bm25", "limit": 5},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 503
