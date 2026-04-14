"""Tests for the semantic search pipeline."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.obsidian.vault import ObsidianVault
from src.search.backends import (
    DEFAULT_MODELS,
    EmbeddingBackend,
    EmbeddingError,
    LocalBackend,
    OpenAIBackend,
    VoyageBackend,
    build_backend,
)
from src.search.chunker import Chunk, chunk_markdown
from src.search.index import VectorIndex
from src.search.indexer import Indexer
from src.search.query import SearchQuery

# =====================
# chunker
# =====================

class TestChunker:
    def test_basic_chunking(self):
        text = "# Heading\n\nSome paragraph text here.\n\nAnother paragraph."
        chunks = chunk_markdown(text, chunk_size=100)
        assert len(chunks) >= 1
        assert all(isinstance(c, Chunk) for c in chunks)

    def test_heading_path_tracking(self):
        text = (
            "# Top\n\nIntro\n\n"
            "## Sub A\n\nContent A\n\n"
            "## Sub B\n\nContent B"
        )
        chunks = chunk_markdown(text, chunk_size=50)

        paths = [c.heading_path for c in chunks]
        assert ["Top"] in paths or any("Top" in p for p in paths)
        assert any("Sub A" in p for p in paths)
        assert any("Sub B" in p for p in paths)

    def test_strips_frontmatter(self):
        text = "---\ntitle: Test\n---\n\n# Body\n\nActual content"
        chunks = chunk_markdown(text)
        combined = " ".join(c.content for c in chunks)
        assert "title: Test" not in combined
        assert "Actual content" in combined

    def test_empty_content(self):
        assert chunk_markdown("") == []
        assert chunk_markdown("---\ntitle: X\n---\n") == []

    def test_respects_chunk_size(self):
        # Long content should be split
        long_text = ("This is a long paragraph. " * 30)
        text = f"# Section\n\n{long_text}"
        chunks = chunk_markdown(text, chunk_size=200)
        assert len(chunks) > 1

    def test_preserves_code_fences(self):
        text = (
            "# Code\n\nHere is code:\n\n"
            "```python\ndef foo():\n    pass\n```\n\n"
            "More text."
        )
        chunks = chunk_markdown(text, chunk_size=1000)
        combined = " ".join(c.content for c in chunks)
        assert "def foo():" in combined
        assert "```python" in combined

    def test_heading_context_property(self):
        chunk = Chunk(
            index=0,
            content="x",
            heading_path=["Project", "Status"],
            start_offset=0,
        )
        assert chunk.heading_context == "Project › Status"

    def test_empty_heading_path(self):
        chunk = Chunk(index=0, content="x", heading_path=[], start_offset=0)
        assert chunk.heading_context == ""


# =====================
# backends
# =====================

class TestBackends:
    def test_default_models(self):
        assert DEFAULT_MODELS["voyage"] == "voyage-3.5"
        assert DEFAULT_MODELS["openai"] == "text-embedding-3-small"
        assert DEFAULT_MODELS["local"] == "all-MiniLM-L6-v2"

    def test_voyage_dimension(self):
        backend = VoyageBackend(api_key="fake-key")
        assert backend.dimension == 1024
        assert backend.name == "voyage"

    def test_openai_small_dimension(self):
        backend = OpenAIBackend(api_key="fake-key", model="text-embedding-3-small")
        assert backend.dimension == 1536
        assert backend.name == "openai"

    def test_openai_large_dimension(self):
        backend = OpenAIBackend(api_key="fake-key", model="text-embedding-3-large")
        assert backend.dimension == 3072

    def test_local_default_dimension(self):
        backend = LocalBackend()
        assert backend.dimension == 384
        assert backend.name == "local"

    def test_build_backend_missing_api_key(self, monkeypatch):
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        with pytest.raises(EmbeddingError, match="VOYAGE_API_KEY"):
            build_backend("voyage")

    def test_build_backend_unknown(self):
        with pytest.raises(EmbeddingError, match="Unknown"):
            build_backend("nonsense")

    def test_build_local(self):
        backend = build_backend("local")
        assert backend.name == "local"

    @pytest.mark.asyncio
    async def test_empty_input_returns_empty(self):
        backend = LocalBackend()
        vectors = await backend.embed([])
        assert vectors == []


# =====================
# VectorIndex
# =====================

class MockEmbedding(EmbeddingBackend):
    """Deterministic embedding backend for testing."""

    def __init__(self, dim: int = 8):
        self._dim = dim

    @property
    def name(self) -> str:
        return "mock"

    @property
    def dimension(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # Deterministic: hash each text into a vector
        vectors = []
        for text in texts:
            v = [float((hash(text + str(i)) % 1000) / 1000) for i in range(self._dim)]
            vectors.append(v)
        return vectors


class TestVectorIndex:
    def test_creates_tables(self, tmp_path):
        index = VectorIndex(tmp_path / "test.db", dimension=8)
        assert index.stats()["total_chunks"] == 0
        assert index.stats()["dimension"] == 8
        index.close()

    def test_upsert_and_retrieve(self, tmp_path):
        index = VectorIndex(tmp_path / "idx.db", dimension=4)
        chunks = [
            Chunk(index=0, content="first", heading_path=["H"], start_offset=0),
            Chunk(index=1, content="second", heading_path=["H"], start_offset=10),
        ]
        vectors = [
            [0.1, 0.2, 0.3, 0.4],
            [0.5, 0.6, 0.7, 0.8],
        ]

        index.upsert_document("doc-1", "/vault/note.md", chunks, vectors)

        stats = index.stats()
        assert stats["total_chunks"] == 2
        assert stats["total_docs"] == 1

    def test_replace_on_upsert(self, tmp_path):
        index = VectorIndex(tmp_path / "r.db", dimension=4)

        index.upsert_document(
            "doc-1", "/v/note.md",
            [Chunk(index=0, content="old", heading_path=[], start_offset=0)],
            [[0.1, 0.2, 0.3, 0.4]],
        )
        index.upsert_document(
            "doc-1", "/v/note.md",
            [Chunk(index=0, content="new v1", heading_path=[], start_offset=0),
             Chunk(index=1, content="new v2", heading_path=[], start_offset=5)],
            [[0.2, 0.3, 0.4, 0.5], [0.3, 0.4, 0.5, 0.6]],
        )

        assert index.stats()["total_chunks"] == 2

    def test_remove_document(self, tmp_path):
        index = VectorIndex(tmp_path / "rm.db", dimension=4)
        index.upsert_document(
            "doc-1", "/v/a.md",
            [Chunk(index=0, content="x", heading_path=[], start_offset=0)],
            [[0.1, 0.2, 0.3, 0.4]],
        )
        index.upsert_document(
            "doc-2", "/v/b.md",
            [Chunk(index=0, content="y", heading_path=[], start_offset=0)],
            [[0.5, 0.6, 0.7, 0.8]],
        )
        assert index.stats()["total_docs"] == 2

        removed = index.remove_document("doc-1")
        assert removed == 1
        assert index.stats()["total_docs"] == 1

    def test_search_returns_hits(self, tmp_path):
        index = VectorIndex(tmp_path / "s.db", dimension=4)

        chunks = [
            Chunk(index=0, content="apple fruit", heading_path=["F"], start_offset=0),
            Chunk(index=1, content="orange fruit", heading_path=["F"], start_offset=20),
        ]
        vectors = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]]
        index.upsert_document("doc-1", "/v/n.md", chunks, vectors)

        hits = index.search([1.0, 0.0, 0.0, 0.0], top_k=2)
        assert len(hits) == 2
        # First hit should be more similar to the query
        assert hits[0].content == "apple fruit"

    def test_search_respects_min_score(self, tmp_path):
        index = VectorIndex(tmp_path / "sc.db", dimension=4)
        index.upsert_document(
            "doc-1", "/v/n.md",
            [Chunk(index=0, content="x", heading_path=[], start_offset=0)],
            [[1.0, 0.0, 0.0, 0.0]],
        )

        # Query in opposite direction → low score
        hits = index.search([-1.0, 0.0, 0.0, 0.0], top_k=5, min_score=0.9)
        assert len(hits) == 0

    def test_dimension_mismatch_raises(self, tmp_path):
        index = VectorIndex(tmp_path / "d.db", dimension=4)
        with pytest.raises(ValueError, match="dimension"):
            index.upsert_document(
                "doc-1", "/v/n.md",
                [Chunk(index=0, content="x", heading_path=[], start_offset=0)],
                [[1.0, 2.0]],  # wrong dim
            )

    def test_clear(self, tmp_path):
        index = VectorIndex(tmp_path / "cl.db", dimension=4)
        index.upsert_document(
            "doc-1", "/v/n.md",
            [Chunk(index=0, content="x", heading_path=[], start_offset=0)],
            [[1.0, 0.0, 0.0, 0.0]],
        )
        assert index.stats()["total_chunks"] == 1

        index.clear()
        assert index.stats()["total_chunks"] == 0


# =====================
# Indexer
# =====================

class TestIndexer:
    @pytest.mark.asyncio
    async def test_index_note(self, tmp_path):
        vault = ObsidianVault(tmp_path, {"_default": "Inbox"})
        backend = MockEmbedding(dim=8)
        index = VectorIndex(tmp_path / "ix.db", dimension=8)

        note_path = tmp_path / "Inbox" / "test.md"
        vault.write_note(
            note_path,
            {"title": "Test", "source": "remarkable"},
            "# Test\n\nSome content here for testing.",
        )

        indexer = Indexer(backend, index, vault, chunk_size=200)
        count = await indexer.index_note("doc-1", note_path, note_path.read_text())

        assert count >= 1
        assert index.stats()["total_chunks"] >= 1

    @pytest.mark.asyncio
    async def test_reindex_vault(self, tmp_path):
        vault = ObsidianVault(tmp_path, {"_default": "Inbox"})
        backend = MockEmbedding(dim=8)
        index = VectorIndex(tmp_path / "rv.db", dimension=8)

        for i in range(3):
            note_path = tmp_path / "Inbox" / f"note{i}.md"
            vault.write_note(
                note_path,
                {"title": f"Note {i}", "source": "remarkable", "remarkable_id": f"doc-{i}"},
                f"# Note {i}\n\nContent of note number {i}.",
            )

        indexer = Indexer(backend, index, vault, chunk_size=200)
        report = await indexer.reindex_vault()

        assert report["notes"] == 3
        assert report["chunks"] >= 3
        assert report["backend"] == "mock"


# =====================
# SearchQuery
# =====================

class TestSearchQuery:
    @pytest.mark.asyncio
    async def test_ask_returns_hits(self, tmp_path):
        backend = MockEmbedding(dim=8)
        index = VectorIndex(tmp_path / "q.db", dimension=8)

        chunks = [
            Chunk(index=0, content="python programming", heading_path=[], start_offset=0),
        ]
        vectors = await backend.embed(["python programming"])
        index.upsert_document("doc-1", "/v/a.md", chunks, vectors)

        searcher = SearchQuery(backend=backend, index=index)
        result = await searcher.ask("python programming", top_k=3, synthesize=False)

        assert result.has_results
        assert len(result.hits) == 1

    @pytest.mark.asyncio
    async def test_ask_empty_query(self, tmp_path):
        backend = MockEmbedding(dim=8)
        index = VectorIndex(tmp_path / "eq.db", dimension=8)

        searcher = SearchQuery(backend=backend, index=index)
        result = await searcher.ask("")

        assert not result.has_results
        assert result.answer == ""

    @pytest.mark.asyncio
    async def test_ask_with_synthesis(self, tmp_path):
        backend = MockEmbedding(dim=8)
        index = VectorIndex(tmp_path / "syn.db", dimension=8)

        chunks = [
            Chunk(index=0, content="The project deadline is Friday",
                  heading_path=["Meeting"], start_offset=0),
        ]
        vectors = await backend.embed(["The project deadline is Friday"])
        index.upsert_document("doc-1", "/v/meeting.md", chunks, vectors)

        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="The deadline is Friday per [[meeting]].")]
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        searcher = SearchQuery(
            backend=backend,
            index=index,
            anthropic_client=mock_client,
        )
        result = await searcher.ask(
            "when is the deadline",
            top_k=1,
            min_score=0.0,
            synthesize=True,
        )

        assert result.has_results
        assert "Friday" in result.answer
