"""Tests for Obsidian → reMarkable reverse sync."""

from unittest.mock import AsyncMock

import pytest

from src.config import ReverseSyncConfig
from src.obsidian.vault import ObsidianVault
from src.sync.reverse_sync import ReverseSyncer
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
# collect_candidates
# =====================

class TestCollectCandidates:
    def test_disabled_returns_nothing(self, vault, state):
        config = ReverseSyncConfig(enabled=False)
        syncer = ReverseSyncer(config, vault, state)
        assert syncer.collect_candidates() == []

    def test_frontmatter_trigger(self, vault, state):
        note_path = vault.path / "Inbox" / "push-me.md"
        vault.write_note(
            note_path,
            {"title": "Push Me", "push_to_tablet": True},
            "Content to push",
        )

        config = ReverseSyncConfig(
            enabled=True,
            trigger_on_frontmatter=True,
            trigger_on_folder=False,
            trigger_on_demand=False,
        )
        syncer = ReverseSyncer(config, vault, state)

        candidates = syncer.collect_candidates()
        assert note_path in candidates

    def test_frontmatter_skips_already_pushed(self, vault, state):
        note_path = vault.path / "Inbox" / "done.md"
        vault.write_note(
            note_path,
            {"title": "Done", "push_to_tablet": True,
             "pushed_to_tablet_at": "2026-01-01T00:00:00Z"},
            "Already pushed",
        )

        config = ReverseSyncConfig(enabled=True, trigger_on_frontmatter=True)
        syncer = ReverseSyncer(config, vault, state)
        assert syncer.collect_candidates() == []

    def test_folder_trigger(self, vault, state):
        (vault.path / "To-Tablet").mkdir(parents=True)
        note_path = vault.path / "To-Tablet" / "folder-note.md"
        vault.write_note(note_path, {"title": "FN"}, "Folder content")

        config = ReverseSyncConfig(
            enabled=True,
            trigger_on_frontmatter=False,
            trigger_on_folder=True,
            folder="To-Tablet",
            trigger_on_demand=False,
        )
        syncer = ReverseSyncer(config, vault, state)
        assert note_path in syncer.collect_candidates()

    def test_folder_nonexistent(self, vault, state):
        config = ReverseSyncConfig(
            enabled=True, trigger_on_frontmatter=False,
            trigger_on_folder=True, folder="Missing",
            trigger_on_demand=False,
        )
        syncer = ReverseSyncer(config, vault, state)
        assert syncer.collect_candidates() == []

    def test_queue_trigger(self, vault, state):
        note_path = vault.path / "Inbox" / "queued.md"
        vault.write_note(note_path, {"title": "Q"}, "Queued content")
        state.enqueue_reverse_push(str(note_path))

        config = ReverseSyncConfig(
            enabled=True,
            trigger_on_frontmatter=False,
            trigger_on_folder=False,
            trigger_on_demand=True,
        )
        syncer = ReverseSyncer(config, vault, state)
        assert note_path in syncer.collect_candidates()

    def test_queue_skips_missing_files(self, vault, state):
        state.enqueue_reverse_push("/vault/nonexistent.md")

        config = ReverseSyncConfig(
            enabled=True,
            trigger_on_frontmatter=False,
            trigger_on_folder=False,
            trigger_on_demand=True,
        )
        syncer = ReverseSyncer(config, vault, state)
        assert syncer.collect_candidates() == []

    def test_dedupes_across_triggers(self, vault, state):
        # Note qualifies for both folder AND frontmatter
        (vault.path / "To-Tablet").mkdir(parents=True)
        note_path = vault.path / "To-Tablet" / "dup.md"
        vault.write_note(
            note_path,
            {"title": "D", "push_to_tablet": True},
            "Content",
        )

        config = ReverseSyncConfig(
            enabled=True,
            trigger_on_frontmatter=True,
            trigger_on_folder=True,
            folder="To-Tablet",
        )
        syncer = ReverseSyncer(config, vault, state)
        candidates = syncer.collect_candidates()
        assert len(candidates) == 1


# =====================
# push flow
# =====================

class TestPushFlow:
    @pytest.mark.asyncio
    async def test_run_pushes_pdf(self, vault, state, tmp_path):
        note_path = vault.path / "Inbox" / "p.md"
        vault.write_note(
            note_path,
            {"title": "P", "push_to_tablet": True},
            "# P\n\nBody\n\n- item 1\n- item 2",
        )

        config = ReverseSyncConfig(enabled=True, format="pdf")
        syncer = ReverseSyncer(config, vault, state)

        cloud = AsyncMock()
        from src.remarkable.cloud import DocumentMetadata
        cloud.list_items = AsyncMock(return_value=[
            DocumentMetadata(
                id="folder-1", name="From-Vault", parent="",
                doc_type="CollectionType", version=1, hash="", modified="",
            ),
        ])
        cloud.upload_document = AsyncMock(return_value="new-rm-doc")

        result = await syncer.run(cloud)

        assert len(result.pushed) == 1
        assert result.failed == []

        # Verify state was updated
        pushed = state.get_reverse_queue(status="pushed")
        assert len(pushed) == 1

    @pytest.mark.asyncio
    async def test_run_stamps_frontmatter(self, vault, state):
        note_path = vault.path / "Inbox" / "stamp.md"
        vault.write_note(
            note_path,
            {"title": "S", "push_to_tablet": True},
            "Content",
        )

        config = ReverseSyncConfig(enabled=True, stamp_frontmatter=True)
        syncer = ReverseSyncer(config, vault, state)

        cloud = AsyncMock()
        cloud.list_items = AsyncMock(return_value=[])
        cloud.create_folder = AsyncMock(return_value="folder-id")
        cloud.upload_document = AsyncMock(return_value="rm-id")

        await syncer.run(cloud)

        fm, _ = vault.read_note(note_path)
        assert "pushed_to_tablet_at" in fm

    @pytest.mark.asyncio
    async def test_run_handles_upload_failure(self, vault, state):
        note_path = vault.path / "Inbox" / "fail.md"
        vault.write_note(
            note_path,
            {"title": "F", "push_to_tablet": True},
            "Content",
        )

        config = ReverseSyncConfig(enabled=True)
        syncer = ReverseSyncer(config, vault, state)

        cloud = AsyncMock()
        cloud.list_items = AsyncMock(return_value=[])
        cloud.create_folder = AsyncMock(return_value="fid")
        cloud.upload_document = AsyncMock(side_effect=RuntimeError("network down"))

        result = await syncer.run(cloud)

        assert result.pushed == []
        assert len(result.failed) == 1
        errored = state.get_reverse_queue(status="error")
        assert len(errored) == 1
        assert "network down" in errored[0]["error"]

    @pytest.mark.asyncio
    async def test_push_single(self, vault, state):
        note_path = vault.path / "Inbox" / "single.md"
        vault.write_note(note_path, {"title": "S"}, "Content")

        config = ReverseSyncConfig(enabled=True, format="pdf")
        syncer = ReverseSyncer(config, vault, state)

        cloud = AsyncMock()
        cloud.list_items = AsyncMock(return_value=[])
        cloud.create_folder = AsyncMock(return_value="f")
        cloud.upload_document = AsyncMock(return_value="rm-x")

        result = await syncer.push_single(note_path, cloud)
        assert result == "rm-x"

    @pytest.mark.asyncio
    async def test_run_notebook_format(self, vault, state):
        note_path = vault.path / "Inbox" / "nb.md"
        vault.write_note(
            note_path,
            {"title": "NB", "push_to_tablet": True},
            "# NB\n\nContent",
        )

        config = ReverseSyncConfig(enabled=True, format="notebook")
        syncer = ReverseSyncer(config, vault, state)

        cloud = AsyncMock()
        cloud.list_items = AsyncMock(return_value=[])
        cloud.create_folder = AsyncMock(return_value="f")
        cloud.upload_document = AsyncMock(return_value="nb-id")

        result = await syncer.run(cloud)
        assert len(result.pushed) == 1

    @pytest.mark.asyncio
    async def test_empty_run(self, vault, state):
        config = ReverseSyncConfig(enabled=True)
        syncer = ReverseSyncer(config, vault, state)

        cloud = AsyncMock()
        result = await syncer.run(cloud)

        assert result.total == 0
        cloud.upload_document.assert_not_called()
