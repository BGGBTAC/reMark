"""Tests for Microsoft Teams integration."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import TeamsConfig
from src.integrations.microsoft.teams import (
    DigestData,
    build_digest,
    correlate_meetings,
    post_digest,
    render_adaptive_card,
)
from src.obsidian.vault import ObsidianVault
from src.sync.state import SyncState


@pytest.fixture
def vault(tmp_path):
    return ObsidianVault(tmp_path / "vault", {"_default": "Inbox"})


@pytest.fixture
def state(tmp_path):
    s = SyncState(tmp_path / "state.db")
    yield s
    s.close()


# =====================
# build_digest
# =====================

class TestBuildDigest:
    def test_empty_vault(self, state, vault):
        digest = build_digest(state, vault, period="weekly")
        assert digest.notes_count == 0
        assert digest.action_items == []
        assert digest.cost_usd == 0.0

    def test_counts_synced_notes(self, state, vault):
        state.mark_synced(
            doc_id="d1", doc_name="Note 1", parent_folder="",
            cloud_hash="h", vault_path="/v/n.md", ocr_engine="crdt",
            page_count=3, action_count=2,
        )
        digest = build_digest(state, vault, period="weekly")
        assert digest.notes_count == 1

    def test_collects_open_actions(self, state, vault):
        actions_dir = vault.path / "Actions"
        actions_dir.mkdir(parents=True)
        (actions_dir / "src-actions.md").write_text(
            "- [ ] Do thing\n- [ ] Another thing\n- [x] Done",
            encoding="utf-8",
        )

        digest = build_digest(state, vault, period="weekly")
        assert len(digest.action_items) == 2
        assert all("Do" in a["text"] or "Another" in a["text"] for a in digest.action_items)

    def test_collects_top_tags(self, state, vault):
        # Note tagged recently
        now = datetime.now(UTC).isoformat()
        note = vault.path / "Inbox" / "tagged.md"
        vault.write_note(
            note,
            {
                "title": "T",
                "source": "remarkable",
                "last_synced": now,
                "tags": ["meeting", "backend"],
            },
            "Content",
        )

        digest = build_digest(state, vault, period="weekly")
        assert "meeting" in digest.top_tags or "backend" in digest.top_tags

    def test_cost_in_digest(self, state, vault):
        state.log_api_usage(
            provider="anthropic", model="claude-sonnet-4-20250514",
            operation="structure", input_tokens=500, output_tokens=100, cost_usd=0.025,
        )
        digest = build_digest(state, vault, period="weekly")
        assert digest.cost_usd == pytest.approx(0.025, rel=1e-3)


# =====================
# render_adaptive_card
# =====================

class TestAdaptiveCard:
    def test_basic_structure(self):
        d = DigestData(
            period="weekly",
            notes_count=5,
            action_items=[],
            top_tags=[],
            cost_usd=0.12,
            date_range="2026-04-07 → 2026-04-14",
        )
        card = render_adaptive_card(d)
        assert card["type"] == "message"
        assert len(card["attachments"]) == 1
        att = card["attachments"][0]
        assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
        body = att["content"]["body"]
        assert any("Weekly digest" in b.get("text", "") for b in body)

    def test_facts_present(self):
        d = DigestData(
            period="daily",
            notes_count=2,
            action_items=[],
            top_tags=["meeting"],
            cost_usd=0.5,
            date_range="2026-04-13 → 2026-04-14",
        )
        card = render_adaptive_card(d)
        body = card["attachments"][0]["content"]["body"]
        facts_block = next(b for b in body if b.get("type") == "FactSet")
        titles = {f["title"] for f in facts_block["facts"]}
        assert {"Period", "Synced notes", "Open actions", "API cost", "Top tags"}.issubset(titles)

    def test_action_items_rendered(self):
        d = DigestData(
            period="weekly", notes_count=1,
            action_items=[{"text": "Ship feature", "source": "Sprint"}],
            top_tags=[], cost_usd=0.0,
            date_range="...",
        )
        card = render_adaptive_card(d)
        body = card["attachments"][0]["content"]["body"]
        assert any("Ship feature" in b.get("text", "") for b in body)


# =====================
# post_digest
# =====================

class TestPostDigest:
    @pytest.mark.asyncio
    async def test_disabled_skips(self):
        cfg = TeamsConfig(enabled=False, webhook_url="")
        d = DigestData(
            period="daily", notes_count=0, action_items=[],
            top_tags=[], cost_usd=0.0, date_range="",
        )
        result = await post_digest(cfg, d)
        assert result is False

    @pytest.mark.asyncio
    async def test_no_webhook_skips(self):
        cfg = TeamsConfig(enabled=True, webhook_url="")
        d = DigestData("daily", 0, [], [], 0.0, "")
        result = await post_digest(cfg, d)
        assert result is False

    @pytest.mark.asyncio
    async def test_success(self):
        cfg = TeamsConfig(enabled=True, webhook_url="https://teams.test/webhook")
        d = DigestData("weekly", 5, [], [], 0.1, "x")

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("src.integrations.microsoft.teams.httpx.AsyncClient", return_value=mock_client):
            result = await post_digest(cfg, d)

        assert result is True
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_error_status(self):
        cfg = TeamsConfig(enabled=True, webhook_url="https://teams.test/webhook")
        d = DigestData("weekly", 0, [], [], 0.0, "")

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "server down"

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("src.integrations.microsoft.teams.httpx.AsyncClient", return_value=mock_client):
            result = await post_digest(cfg, d)

        assert result is False


# =====================
# correlate_meetings
# =====================

class TestCorrelateMeetings:
    @pytest.mark.asyncio
    async def test_correlates_by_title(self, vault):
        note_path = vault.path / "Inbox" / "Weekly Standup.md"
        vault.write_note(
            note_path,
            {"title": "Weekly Standup", "source": "remarkable"},
            "Notes",
        )

        graph = MagicMock()
        graph.get = AsyncMock(return_value={
            "value": [
                {"subject": "Weekly Standup - April",
                 "start": {"dateTime": "2026-04-15T09:00:00"}},
                {"subject": "Unrelated meeting",
                 "start": {"dateTime": "2026-04-15T11:00:00"}},
            ],
        })

        matches = await correlate_meetings(graph, vault)
        assert len(matches) == 1
        assert matches[0].note_title == "weekly standup"

    @pytest.mark.asyncio
    async def test_no_events(self, vault):
        graph = MagicMock()
        graph.get = AsyncMock(return_value={"value": []})
        matches = await correlate_meetings(graph, vault)
        assert matches == []

    @pytest.mark.asyncio
    async def test_graph_failure_returns_empty(self, vault):
        graph = MagicMock()
        graph.get = AsyncMock(side_effect=RuntimeError("network"))
        matches = await correlate_meetings(graph, vault)
        assert matches == []
