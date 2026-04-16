"""Tests for BM25 + RRF hybrid search."""

from __future__ import annotations

import pytest

from src.search.backends import EmbeddingBackend
from src.search.chunker import Chunk
from src.search.index import VectorIndex
from src.search.query import SearchQuery


class _StaticEmbedding(EmbeddingBackend):
    """Tiny deterministic backend — every text collapses to the same vector.

    That way semantic ranking is effectively a tie and BM25 drives the
    actual ordering in the hybrid test.
    """

    def __init__(self, dim: int = 4):
        self._dim = dim

    @property
    def name(self) -> str:
        return "static"

    @property
    def dimension(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * self._dim for _ in texts]


@pytest.fixture
def populated_index(tmp_path):
    index = VectorIndex(tmp_path / "idx.db", dimension=4)

    docs = [
        ("doc-coffee", "First we brew the espresso with a dedicated grinder."),
        ("doc-tea", "Matcha requires whisking the powder into hot water."),
        ("doc-remark", "The reMarkable tablet syncs via a long-lived device token."),
        ("doc-generic", "General notes about productivity and journaling."),
    ]
    for doc_id, text in docs:
        chunk = Chunk(index=0, content=text, heading_path=["Note"], start_offset=0)
        index.upsert_document(doc_id, f"/v/{doc_id}.md", [chunk], [[0.1, 0.1, 0.1, 0.1]])

    yield index
    index.close()


class TestBM25:
    def test_rare_keyword_ranks_first(self, populated_index):
        hits = populated_index.search_bm25("reMarkable tablet", top_k=3)
        assert hits, "BM25 should return something for a keyword present in the corpus"
        assert hits[0].doc_id == "doc-remark"

    def test_unknown_word_returns_nothing(self, populated_index):
        assert populated_index.search_bm25("xylophones", top_k=5) == []

    def test_empty_query_returns_nothing(self, populated_index):
        assert populated_index.search_bm25("", top_k=5) == []

    def test_quoted_special_chars_dont_crash(self, populated_index):
        # Parens are operator characters in FTS5 syntax; our quoting
        # must neutralize them instead of blowing up.
        populated_index.search_bm25("(note)", top_k=5)


class TestHybrid:
    @pytest.mark.asyncio
    async def test_hybrid_surfaces_exact_keyword_hit(self, populated_index):
        """With semantic scores tied, BM25 should drive the hybrid result."""
        searcher = SearchQuery(
            backend=_StaticEmbedding(dim=4),
            index=populated_index,
        )
        result = await searcher.ask(
            "reMarkable device token",
            top_k=2,
            min_score=0.0,
            synthesize=False,
            mode="hybrid",
        )
        assert result.has_results
        assert result.hits[0].doc_id == "doc-remark"

    @pytest.mark.asyncio
    async def test_bm25_mode_skips_embedding(self, populated_index):
        """bm25 mode should not consult the embedding backend at all."""

        class ExplodingBackend(EmbeddingBackend):
            @property
            def name(self) -> str:
                return "boom"

            @property
            def dimension(self) -> int:
                return 4

            async def embed(self, texts):
                raise AssertionError("embed() must not be called in bm25 mode")

        searcher = SearchQuery(backend=ExplodingBackend(), index=populated_index)
        result = await searcher.ask(
            "matcha powder",
            top_k=1,
            min_score=0.0,
            mode="bm25",
        )
        assert result.hits and result.hits[0].doc_id == "doc-tea"

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self, populated_index):
        searcher = SearchQuery(backend=_StaticEmbedding(), index=populated_index)
        result = await searcher.ask("", mode="hybrid")
        assert result.hits == []


class TestIndexMaintenance:
    def test_remove_also_clears_fts(self, populated_index):
        before = populated_index.search_bm25("espresso", top_k=5)
        assert before and before[0].doc_id == "doc-coffee"

        populated_index.remove_document("doc-coffee")

        after = populated_index.search_bm25("espresso", top_k=5)
        assert after == []

    def test_clear_empties_fts(self, populated_index):
        populated_index.clear()
        assert populated_index.search_bm25("tablet", top_k=5) == []
