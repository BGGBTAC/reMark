"""Indexer service — chunks notes and writes them to the VectorIndex."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from src.obsidian.vault import ObsidianVault
from src.search.backends import EmbeddingBackend
from src.search.chunker import Chunk, chunk_markdown
from src.search.index import VectorIndex

logger = logging.getLogger(__name__)


class Indexer:
    """Orchestrates chunking + embedding + index writes."""

    def __init__(
        self,
        backend: EmbeddingBackend,
        index: VectorIndex,
        vault: ObsidianVault,
        chunk_size: int = 512,
        chunk_overlap: int = 50,
        batch_size: int = 64,
    ):
        self._backend = backend
        self._index = index
        self._vault = vault
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._batch_size = batch_size

    async def index_note(self, doc_id: str, vault_path: Path, content: str) -> int:
        """Chunk and index a single note.

        Returns the number of chunks indexed.
        """
        chunks = chunk_markdown(
            content,
            chunk_size=self._chunk_size,
            chunk_overlap=self._chunk_overlap,
        )

        if not chunks:
            self._index.remove_document(doc_id)
            return 0

        texts = [c.content for c in chunks]
        vectors = await self._backend.embed(texts)

        self._index.upsert_document(
            doc_id=doc_id,
            vault_path=str(vault_path),
            chunks=chunks,
            embeddings=vectors,
        )
        return len(chunks)

    async def reindex_vault(
        self,
        on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Embed every chunk across the vault in backend-sized batches.

        Collects all chunks before calling the backend so one embed() call
        can cover many documents — amortising HTTP round-trips on batch-
        capable backends (Voyage, OpenAI, Ollama).

        on_progress(done, total) is called after each batch so callers can
        display a progress indicator without coupling the indexer to any
        particular UI.
        """
        self._index.clear()

        notes = self._vault.list_notes_by_source("remarkable")

        # Collect every (doc_id, vault_path, chunk) triple first so we can
        # form cross-document batches of exactly the right size.
        all_items: list[tuple[str, str, Chunk]] = []
        for note_path in notes:
            result = self._vault.read_note(note_path)
            if result is None:
                continue
            frontmatter, content = result
            doc_id = frontmatter.get("remarkable_id") or str(note_path)
            chunks = chunk_markdown(
                content,
                chunk_size=self._chunk_size,
                chunk_overlap=self._chunk_overlap,
            )
            for chunk in chunks:
                all_items.append((doc_id, str(note_path), chunk))

        total = len(all_items)
        if total == 0:
            logger.info("Reindexed vault: 0 notes, 0 chunks")
            return {
                "notes": 0,
                "chunks": 0,
                "backend": self._backend.name,
                "dimension": self._backend.dimension,
            }

        # Respect both the configured batch ceiling and the backend's own limit
        # (e.g. Ollama reports max_batch_size; Voyage/OpenAI don't declare one).
        effective_batch = min(
            self._batch_size,
            getattr(self._backend, "max_batch_size", self._batch_size),
        )

        # Accumulate per-document chunks so upsert_document gets them all at
        # once — the index clears and re-inserts per doc, so partial writes
        # would lose chunks if we flushed mid-document.
        pending: dict[str, tuple[str, list[Chunk], list[list[float]]]] = {}

        processed = 0
        for i in range(0, total, effective_batch):
            window = all_items[i : i + effective_batch]
            texts = [chunk.content for _, _, chunk in window]
            vectors = await self._backend.embed(texts)

            for (doc_id, vault_path, chunk), vec in zip(window, vectors, strict=True):
                if doc_id not in pending:
                    pending[doc_id] = (vault_path, [], [])
                pending[doc_id][1].append(chunk)
                pending[doc_id][2].append(vec)

            processed += len(window)
            if on_progress is not None:
                await on_progress(processed, total)

        for doc_id, (vault_path, chunks, embeddings) in pending.items():
            self._index.upsert_document(
                doc_id=doc_id,
                vault_path=vault_path,
                chunks=chunks,
                embeddings=embeddings,
            )

        indexed_docs = len(pending)
        logger.info(
            "Reindexed vault: %d notes, %d chunks",
            indexed_docs, total,
        )

        return {
            "notes": indexed_docs,
            "chunks": total,
            "backend": self._backend.name,
            "dimension": self._backend.dimension,
        }

    def remove_document(self, doc_id: str) -> int:
        """Remove a document from the index."""
        return self._index.remove_document(doc_id)
