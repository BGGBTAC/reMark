"""Tests for sync state, engine, and scheduler."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.sync.scheduler import _parse_interval
from src.sync.state import SyncState

# =====================
# SyncState
# =====================

class TestSyncState:
    def test_init_creates_tables(self, tmp_path):
        db = tmp_path / "test.db"
        state = SyncState(db)

        # Should not raise — tables exist
        state.conn.execute("SELECT * FROM sync_state LIMIT 1")
        state.conn.execute("SELECT * FROM sync_log LIMIT 1")
        state.conn.execute("SELECT * FROM auth_cache LIMIT 1")
        state.close()

    def test_needs_sync_new_doc(self, tmp_path):
        state = SyncState(tmp_path / "test.db")
        assert state.needs_sync("doc-new", "hash-123") is True
        state.close()

    def test_needs_sync_unchanged(self, tmp_path):
        state = SyncState(tmp_path / "test.db")

        state.mark_synced(
            doc_id="doc-1", doc_name="Note",
            parent_folder="Work", cloud_hash="hash-abc",
            vault_path="/vault/note.md", ocr_engine="crdt",
            page_count=3, action_count=1,
        )

        assert state.needs_sync("doc-1", "hash-abc") is False
        state.close()

    def test_needs_sync_changed_hash(self, tmp_path):
        state = SyncState(tmp_path / "test.db")

        state.mark_synced(
            doc_id="doc-1", doc_name="Note",
            parent_folder="Work", cloud_hash="hash-old",
            vault_path="/vault/note.md", ocr_engine="crdt",
            page_count=3, action_count=1,
        )

        assert state.needs_sync("doc-1", "hash-new") is True
        state.close()

    def test_needs_sync_after_error(self, tmp_path):
        state = SyncState(tmp_path / "test.db")

        state.mark_synced(
            doc_id="doc-1", doc_name="Note",
            parent_folder="Work", cloud_hash="hash-abc",
            vault_path="/vault/note.md", ocr_engine="crdt",
            page_count=3, action_count=0,
        )
        state.mark_error("doc-1", "something broke")

        # Should retry after error even if hash matches
        assert state.needs_sync("doc-1", "hash-abc") is True
        state.close()

    def test_mark_synced_updates_version(self, tmp_path):
        state = SyncState(tmp_path / "test.db")

        for i in range(3):
            state.mark_synced(
                doc_id="doc-1", doc_name="Note",
                parent_folder="Work", cloud_hash=f"hash-{i}",
                vault_path="/vault/note.md", ocr_engine="crdt",
                page_count=1, action_count=0,
            )

        doc = state.get_doc_state("doc-1")
        assert doc is not None
        assert doc["version"] == 3
        assert doc["cloud_hash"] == "hash-2"
        state.close()

    def test_get_doc_state_missing(self, tmp_path):
        state = SyncState(tmp_path / "test.db")
        assert state.get_doc_state("nonexistent") is None
        state.close()

    def test_pending_responses(self, tmp_path):
        state = SyncState(tmp_path / "test.db")

        state.mark_synced(
            doc_id="doc-1", doc_name="Note 1",
            parent_folder="", cloud_hash="h1",
            vault_path="/v/n1.md", ocr_engine="crdt",
            page_count=1, action_count=0,
        )
        state.mark_synced(
            doc_id="doc-2", doc_name="Note 2",
            parent_folder="", cloud_hash="h2",
            vault_path="/v/n2.md", ocr_engine="crdt",
            page_count=1, action_count=0,
        )

        # Mark one as pending
        state.mark_response_pending("doc-1")
        pending = state.get_pending_responses()
        assert len(pending) == 1
        assert pending[0]["doc_id"] == "doc-1"

        # Mark as sent
        state.mark_response_sent("doc-1")
        pending = state.get_pending_responses()
        assert len(pending) == 0
        state.close()

    def test_sync_stats(self, tmp_path):
        state = SyncState(tmp_path / "test.db")

        state.mark_synced(
            doc_id="d1", doc_name="A", parent_folder="",
            cloud_hash="h1", vault_path="", ocr_engine="crdt",
            page_count=5, action_count=3,
        )
        state.mark_synced(
            doc_id="d2", doc_name="B", parent_folder="",
            cloud_hash="h2", vault_path="", ocr_engine="google_vision",
            page_count=2, action_count=1,
        )
        state.mark_error("d3", "failed")

        stats = state.get_sync_stats()
        assert stats.total_docs == 3
        assert stats.synced == 2
        assert stats.errors == 1
        assert stats.total_pages == 7
        assert stats.total_actions == 4
        assert stats.last_sync is not None
        state.close()

    def test_sync_log(self, tmp_path):
        state = SyncState(tmp_path / "test.db")

        state.mark_synced(
            doc_id="d1", doc_name="Note", parent_folder="",
            cloud_hash="h1", vault_path="", ocr_engine="crdt",
            page_count=1, action_count=0,
        )
        state.mark_error("d2", "broke")

        log = state.get_recent_log(10)
        assert len(log) >= 2
        actions = [entry["action"] for entry in log]
        assert "sync" in actions
        assert "error" in actions
        state.close()

    def test_empty_stats(self, tmp_path):
        state = SyncState(tmp_path / "test.db")
        stats = state.get_sync_stats()
        assert stats.total_docs == 0
        assert stats.last_sync is None
        state.close()


# =====================
# SyncEngine
# =====================

class TestSyncEngine:
    @pytest.mark.asyncio
    async def test_sync_once_skips_unchanged(self, tmp_path):
        from src.config import AppConfig
        from src.remarkable.cloud import DocumentMetadata
        from src.sync.engine import SyncEngine

        config = AppConfig()
        config.sync.state_db = str(tmp_path / "state.db")
        config.obsidian.vault_path = str(tmp_path / "vault")
        config.obsidian.git.enabled = False

        engine = SyncEngine(config)

        # Pre-mark a doc as synced
        engine.state.mark_synced(
            doc_id="doc-1", doc_name="Old Note",
            parent_folder="Work", cloud_hash="same-hash",
            vault_path="", ocr_engine="crdt",
            page_count=1, action_count=0,
        )

        # Mock cloud to return that same doc
        cloud = AsyncMock()
        doc_manager = AsyncMock()
        doc_manager.list_documents = AsyncMock(return_value=[
            DocumentMetadata(
                id="doc-1", name="Old Note", parent="f1",
                doc_type="DocumentType", version=1,
                hash="same-hash", modified="",
            ),
        ])

        ocr_pipeline = AsyncMock()

        report = await engine.sync_once(cloud, doc_manager, ocr_pipeline)

        assert report.skipped == 1
        assert report.total == 0
        assert report.errors == 0

    @pytest.mark.asyncio
    async def test_sync_once_processes_new_doc(self, tmp_path):
        from src.config import AppConfig
        from src.ocr.pipeline import PageText
        from src.remarkable.cloud import DocumentMetadata
        from src.remarkable.documents import ResolvedDocument
        from src.remarkable.formats import PageContent, TextBlock
        from src.sync.engine import SyncEngine

        config = AppConfig()
        config.sync.state_db = str(tmp_path / "state.db")
        config.obsidian.vault_path = str(tmp_path / "vault")
        config.obsidian.git.enabled = False
        config.processing.extract_actions = False
        config.processing.extract_tags = False
        config.processing.generate_summary = False

        engine = SyncEngine(config)

        new_doc = DocumentMetadata(
            id="doc-new", name="New Note", parent="",
            doc_type="DocumentType", version=1,
            hash="new-hash", modified="2026-04-13",
        )

        # Mock doc_manager
        doc_dir = tmp_path / "downloads" / "doc-new"
        doc_dir.mkdir(parents=True)

        resolved = ResolvedDocument(
            meta=new_doc,
            local_dir=doc_dir,
            folder_path="Work",
            page_ids=["p1"],
            page_count=1,
        )

        doc_manager = AsyncMock()
        doc_manager.list_documents = AsyncMock(return_value=[new_doc])
        doc_manager.download = AsyncMock(return_value=resolved)
        doc_manager.cleanup = MagicMock()

        # Mock OCR pipeline
        ocr_pipeline = AsyncMock()
        ocr_pipeline.recognize = AsyncMock(return_value=[
            PageText(page_id="p1", text="Test content", confidence=1.0, engine_used="crdt"),
        ])

        cloud = AsyncMock()

        # Mock anthropic calls
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="# New Note\n\nTest content")]

        with patch("src.sync.engine.parse_notebook", return_value=[
            PageContent(page_id="p1", text_blocks=[TextBlock(text="Test")])
        ]), patch("src.sync.engine.anthropic.AsyncAnthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_client.messages.create = AsyncMock(return_value=mock_response)
            mock_anthropic.return_value = mock_client

            report = await engine.sync_once(cloud, doc_manager, ocr_pipeline)

        assert report.success_count == 1
        assert report.skipped == 0

        # Doc should now be in state
        assert not engine.state.needs_sync("doc-new", "new-hash")


# =====================
# _parse_interval
# =====================

class TestParseInterval:
    def test_every_n_minutes(self):
        assert _parse_interval("*/15 * * * *") == 900
        assert _parse_interval("*/5 * * * *") == 300

    def test_specific_minute(self):
        assert _parse_interval("30 * * * *") == 3600

    def test_simple_seconds(self):
        assert _parse_interval("600") == 600

    def test_minimum_60(self):
        assert _parse_interval("10") == 60

    def test_garbage_fallback(self):
        assert _parse_interval("not a cron") == 900

    def test_specific_minute_hourly(self):
        # "0 */2 * * *" means "at minute 0" — minute field is "0", treated as hourly
        assert _parse_interval("0 */2 * * *") == 3600
