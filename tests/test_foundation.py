"""Tests for Phase D foundation fixes: deletion, cost tracking, doctor."""

from unittest.mock import MagicMock

import pytest

from src.obsidian.vault import ObsidianVault
from src.processing.usage import (
    MODEL_PRICING,
    estimate_cost,
    log_anthropic_response,
    log_embedding_usage,
)
from src.sync.state import SyncState

# =====================
# Cost estimation
# =====================


class TestEstimateCost:
    def test_known_model(self):
        cost = estimate_cost("claude-sonnet-4-20250514", 1_000_000, 0)
        assert cost == 3.0

    def test_output_tokens_more_expensive(self):
        cost = estimate_cost("claude-sonnet-4-20250514", 0, 1_000_000)
        assert cost == 15.0

    def test_combined(self):
        cost = estimate_cost("claude-sonnet-4-20250514", 100_000, 50_000)
        # 0.1M * 3 + 0.05M * 15 = 0.3 + 0.75 = 1.05
        assert cost == pytest.approx(1.05, rel=1e-3)

    def test_unknown_model(self):
        assert estimate_cost("fake-model", 1_000_000, 1_000_000) == 0.0

    def test_zero_tokens(self):
        assert estimate_cost("claude-sonnet-4-20250514", 0, 0) == 0.0

    def test_embedding_model_no_output_price(self):
        cost = estimate_cost("text-embedding-3-small", 1_000_000, 0)
        assert cost == 0.02

    def test_all_models_have_pricing_tuples(self):
        for _model, pricing in MODEL_PRICING.items():
            assert len(pricing) == 2
            assert all(isinstance(p, (int, float)) for p in pricing)


# =====================
# log_anthropic_response
# =====================


class TestLogAnthropicResponse:
    def test_logs_with_state(self, tmp_path):
        state = SyncState(tmp_path / "usage.db")

        # Build a mock response shaped like Anthropic's
        response = MagicMock()
        response.usage.input_tokens = 1000
        response.usage.output_tokens = 500

        log_anthropic_response(
            state,
            response,
            model="claude-sonnet-4-20250514",
            operation="structure",
            doc_id="doc-123",
        )

        summary = state.get_api_usage_summary()
        assert summary["total_calls"] == 1
        assert summary["providers"]["anthropic"]["input_tokens"] == 1000
        assert summary["providers"]["anthropic"]["output_tokens"] == 500
        assert summary["total_cost_usd"] > 0
        state.close()

    def test_silent_when_state_none(self):
        response = MagicMock()
        response.usage.input_tokens = 100
        # Should not raise
        log_anthropic_response(None, response, model="x", operation="y")

    def test_silent_when_no_usage(self, tmp_path):
        state = SyncState(tmp_path / "nu.db")
        response = MagicMock()
        response.usage = None

        log_anthropic_response(state, response, model="x", operation="y")
        assert state.get_api_usage_summary()["total_calls"] == 0
        state.close()


class TestLogEmbeddingUsage:
    def test_logs_embedding(self, tmp_path):
        state = SyncState(tmp_path / "e.db")
        log_embedding_usage(
            state,
            provider="openai",
            model="text-embedding-3-small",
            input_tokens=500_000,
        )

        summary = state.get_api_usage_summary()
        assert summary["providers"]["openai"]["calls"] == 1
        assert summary["providers"]["openai"]["input_tokens"] == 500_000
        state.close()


# =====================
# api_usage aggregation
# =====================


class TestApiUsageSummary:
    def test_empty_db(self, tmp_path):
        state = SyncState(tmp_path / "empty.db")
        summary = state.get_api_usage_summary()
        assert summary["total_calls"] == 0
        assert summary["total_cost_usd"] == 0.0
        state.close()

    def test_multiple_providers(self, tmp_path):
        state = SyncState(tmp_path / "multi.db")
        state.log_api_usage("anthropic", "m1", "op", 100, 50, 0.05)
        state.log_api_usage("anthropic", "m1", "op", 200, 100, 0.10)
        state.log_api_usage("openai", "m2", "op", 500, 0, 0.01)

        summary = state.get_api_usage_summary()
        assert summary["total_calls"] == 3
        assert summary["providers"]["anthropic"]["calls"] == 2
        assert summary["providers"]["openai"]["calls"] == 1
        assert summary["total_cost_usd"] == pytest.approx(0.16, abs=1e-4)
        state.close()


# =====================
# Deletion handling — state
# =====================


class TestDeletionState:
    def test_list_active_docs(self, tmp_path):
        state = SyncState(tmp_path / "d.db")

        state.mark_synced(
            doc_id="d1",
            doc_name="Active",
            parent_folder="",
            cloud_hash="h1",
            vault_path="/v/a.md",
            ocr_engine="crdt",
            page_count=1,
            action_count=0,
        )
        state.mark_error("d2", "broke")

        active = state.list_active_docs()
        assert len(active) == 1
        assert active[0]["doc_id"] == "d1"
        state.close()

    def test_mark_archived(self, tmp_path):
        state = SyncState(tmp_path / "ar.db")
        state.mark_synced(
            doc_id="d1",
            doc_name="X",
            parent_folder="",
            cloud_hash="h",
            vault_path="/v/x.md",
            ocr_engine="crdt",
            page_count=1,
            action_count=0,
        )
        state.mark_archived("d1")

        entry = state.get_doc_state("d1")
        assert entry["status"] == "archived"
        assert state.list_active_docs() == []
        state.close()


# =====================
# Vault archiving
# =====================


class TestVaultArchive:
    def test_archive_note_moves_file(self, tmp_path):
        vault = ObsidianVault(tmp_path, {"_default": "Inbox"})
        note_path = tmp_path / "Inbox" / "old.md"
        vault.write_note(
            note_path,
            {"title": "Old Note", "source": "remarkable"},
            "Content here",
        )

        archived = vault.archive_note(note_path)

        assert archived is not None
        assert archived.exists()
        assert not note_path.exists()

        # Check status was updated
        result = vault.read_note(archived)
        assert result is not None
        fm, _ = result
        assert fm["status"] == "archived"
        assert "archived_at" in fm

    def test_archive_missing_returns_none(self, tmp_path):
        vault = ObsidianVault(tmp_path, {})
        result = vault.archive_note(tmp_path / "does-not-exist.md")
        assert result is None

    def test_archive_preserves_folder_structure(self, tmp_path):
        vault = ObsidianVault(tmp_path, {"_default": "Notes"})
        nested = tmp_path / "Notes" / "Work" / "note.md"
        vault.write_note(nested, {"title": "N"}, "Content")

        archived = vault.archive_note(nested)
        assert archived is not None
        # Should be under Archive/Notes/Work/
        assert "Archive" in str(archived)
        assert "Work" in str(archived)
