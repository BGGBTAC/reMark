"""Indexer service — chunks notes and writes them to the VectorIndex."""

from __future__ import annotations

import logging
from pathlib import Path

from src.obsidian.vault import ObsidianVault
from src.search.backends import EmbeddingBackend
from src.search.chunker import chunk_markdown
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
    ):
        self._backend = backend
        self._index = index
        self._vault = vault
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap

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

    async def reindex_vault(self) -> dict:
        """Rebuild the index from all vault notes.

        Walks the vault for every note with source: remarkable in frontmatter,
        re-chunks and re-embeds. Clears the existing index first.
        """
        self._index.clear()

        notes = self._vault.list_notes_by_source("remarkable")
        total_chunks = 0
        indexed_docs = 0

        for note_path in notes:
            result = self._vault.read_note(note_path)
            if result is None:
                continue

            frontmatter, content = result
            doc_id = frontmatter.get("remarkable_id") or str(note_path)

            count = await self.index_note(doc_id, note_path, content)
            if count > 0:
                total_chunks += count
                indexed_docs += 1

        logger.info(
            "Reindexed vault: %d notes, %d chunks",
            indexed_docs, total_chunks,
        )

        return {
            "notes": indexed_docs,
            "chunks": total_chunks,
            "backend": self._backend.name,
            "dimension": self._backend.dimension,
        }

    def remove_document(self, doc_id: str) -> int:
        """Remove a document from the index."""
        return self._index.remove_document(doc_id)
