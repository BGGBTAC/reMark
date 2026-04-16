"""Reindex batches chunks across multiple notes into one embed() call."""

from __future__ import annotations

import pytest

from src.obsidian.vault import ObsidianVault
from src.search.backends import EmbeddingBackend
from src.search.index import VectorIndex
from src.search.indexer import Indexer


class _CountingBackend(EmbeddingBackend):
    """Records every embed() call so tests can assert on batching behaviour."""

    name = "counting"
    dimension = 4

    def __init__(self):
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class _TinyBatchBackend(_CountingBackend):
    """Declares a tiny max_batch_size to force many small embed() calls."""

    max_batch_size = 2


def _make_vault(tmp_path, n_notes: int, sections: int = 2) -> ObsidianVault:
    """Create a vault with n_notes notes, each containing `sections` heading sections."""
    vault = ObsidianVault(tmp_path, {"_default": "Inbox"})
    inbox = tmp_path / "Inbox"
    inbox.mkdir(exist_ok=True)
    for i in range(n_notes):
        body = f"# Note {i}\n\nFirst chunk for note {i}.\n\n"
        for s in range(1, sections):
            body += f"## Section {s}\n\nChunk {s} content for note {i}.\n\n"
        vault.write_note(
            inbox / f"note{i}.md",
            {"title": f"Note {i}", "source": "remarkable", "remarkable_id": f"doc-{i}"},
            body,
        )
    return vault


@pytest.mark.asyncio
async def test_reindex_batches_across_notes(tmp_path):
    """Cross-document batching: many small notes → fewer embed() calls than chunks."""
    vault = _make_vault(tmp_path, n_notes=5, sections=2)
    backend = _CountingBackend()
    index = VectorIndex(tmp_path / "idx.db", dimension=4)
    indexer = Indexer(backend=backend, index=index, vault=vault, chunk_size=512, batch_size=64)

    report = await indexer.reindex_vault()

    total_embedded = sum(len(c) for c in backend.calls)
    assert total_embedded >= 5, "at least one chunk per note"
    # With batch_size=64 all chunks fit in one or two calls
    assert len(backend.calls) <= 2
    assert report["chunks"] >= 5
    assert report["notes"] == 5


@pytest.mark.asyncio
async def test_reindex_respects_backend_max_batch_size(tmp_path):
    """If backend.max_batch_size < configured batch_size, the backend limit wins."""
    vault = _make_vault(tmp_path, n_notes=6, sections=3)
    backend = _TinyBatchBackend()
    index = VectorIndex(tmp_path / "idx.db", dimension=4)
    # batch_size=64 but backend.max_batch_size=2 — effective limit must be 2
    indexer = Indexer(backend=backend, index=index, vault=vault, chunk_size=512, batch_size=64)

    await indexer.reindex_vault()

    for call in backend.calls:
        assert len(call) <= 2, f"call had {len(call)} texts, expected ≤ 2"


@pytest.mark.asyncio
async def test_reindex_calls_progress_callback(tmp_path):
    """on_progress is called with cumulative (done, total) and finishes at done==total."""
    vault = _make_vault(tmp_path, n_notes=3, sections=2)
    backend = _CountingBackend()
    index = VectorIndex(tmp_path / "idx.db", dimension=4)
    indexer = Indexer(backend=backend, index=index, vault=vault, chunk_size=512, batch_size=64)

    progress: list[tuple[int, int]] = []

    async def on_progress(done: int, total: int) -> None:
        progress.append((done, total))

    await indexer.reindex_vault(on_progress=on_progress)

    assert progress, "on_progress was never called"
    last_done, last_total = progress[-1]
    assert last_done == last_total, "final progress update should have done == total"
    # done should never exceed total
    for done, total in progress:
        assert done <= total


@pytest.mark.asyncio
async def test_reindex_empty_vault_returns_zero(tmp_path):
    """Empty vault: zero notes, zero chunks, no embed() calls."""
    vault = ObsidianVault(tmp_path, {"_default": "Inbox"})
    (tmp_path / "Inbox").mkdir(exist_ok=True)
    backend = _CountingBackend()
    index = VectorIndex(tmp_path / "idx.db", dimension=4)
    indexer = Indexer(backend=backend, index=index, vault=vault, chunk_size=512, batch_size=64)

    report = await indexer.reindex_vault()

    assert report["notes"] == 0
    assert report["chunks"] == 0
    assert backend.calls == []


@pytest.mark.asyncio
async def test_reindex_progress_not_called_on_empty(tmp_path):
    """on_progress should not fire when there are no chunks to embed."""
    vault = ObsidianVault(tmp_path, {"_default": "Inbox"})
    (tmp_path / "Inbox").mkdir(exist_ok=True)
    backend = _CountingBackend()
    index = VectorIndex(tmp_path / "idx.db", dimension=4)
    indexer = Indexer(backend=backend, index=index, vault=vault, chunk_size=512, batch_size=64)

    calls: list[tuple[int, int]] = []

    async def on_progress(done: int, total: int) -> None:
        calls.append((done, total))

    await indexer.reindex_vault(on_progress=on_progress)
    assert calls == []
