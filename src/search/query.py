"""Query pipeline: embed a question, retrieve relevant chunks,
optionally synthesize an answer via the configured LLM.

Supports three retrieval modes:

* ``semantic`` — cosine similarity over embeddings only
* ``bm25`` — keyword scoring via SQLite FTS5
* ``hybrid`` (default) — both lists fused with Reciprocal Rank Fusion

RRF keeps the implementation framework-agnostic and robust to score-scale
differences between the two retrievers — we only look at rank position.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import anthropic

from src.search.backends import EmbeddingBackend
from src.search.index import SearchHit, VectorIndex

logger = logging.getLogger(__name__)

# Reciprocal Rank Fusion constant. 60 is the canonical value from the
# original Cormack et al. paper; it smooths over small ranking noise
# without letting the tail dominate.
RRF_K = 60

SearchMode = Literal["semantic", "bm25", "hybrid"]


SYNTHESIS_PROMPT = """\
You are answering a question using the user's own handwritten notes as context.
The context is retrieved from their Obsidian knowledge base.

Rules:
- Answer based ONLY on the provided context
- If the context doesn't contain the answer, say so explicitly
- Cite the source note for each claim (e.g., "In [[Note Name]], ...")
- Keep the answer concise (under 300 words)
- Preserve the author's terminology
- If multiple notes are relevant, synthesize across them

Return plain prose with wiki-link citations, no headings."""


@dataclass
class QueryResult:
    """A semantic query result, with optional synthesized answer."""

    query: str
    hits: list[SearchHit]
    answer: str = ""

    @property
    def has_results(self) -> bool:
        return len(self.hits) > 0


class SearchQuery:
    """Executes semantic queries against the vector index."""

    def __init__(
        self,
        backend: EmbeddingBackend,
        index: VectorIndex,
        anthropic_client: anthropic.AsyncAnthropic | None = None,
        synthesis_model: str = "claude-sonnet-4-20250514",
    ):
        self._backend = backend
        self._index = index
        self._client = anthropic_client
        self._synthesis_model = synthesis_model

    async def ask(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.3,
        synthesize: bool = False,
        mode: SearchMode = "hybrid",
    ) -> QueryResult:
        """Retrieve relevant chunks and optionally synthesize an answer.

        Args:
            query: Natural language question.
            top_k: Max number of chunks to return after fusion.
            min_score: Minimum similarity score (0..1). Applied to the
                semantic leg only — BM25 scores follow a different scale
                so we never prune them before fusion.
            synthesize: If True, use the configured LLM to write a grounded answer.
            mode: ``"semantic"``, ``"bm25"``, or ``"hybrid"`` (default).
        """
        if not query.strip():
            return QueryResult(query=query, hits=[])

        if mode == "bm25":
            hits = self._index.search_bm25(query, top_k=top_k)
        elif mode == "semantic":
            hits = await self._semantic(query, top_k, min_score)
        else:
            hits = await self._hybrid(query, top_k, min_score)

        logger.info(
            "Query '%s...' returned %d hits (mode=%s, backend=%s)",
            query[:40],
            len(hits),
            mode,
            self._backend.name,
        )

        answer = ""
        if synthesize and hits and self._client is not None:
            answer = await self._synthesize(query, hits)

        return QueryResult(query=query, hits=hits, answer=answer)

    async def _semantic(
        self,
        query: str,
        top_k: int,
        min_score: float,
    ) -> list[SearchHit]:
        query_vectors = await self._backend.embed([query])
        if not query_vectors:
            return []
        return self._index.search(
            query_vector=query_vectors[0],
            top_k=top_k,
            min_score=min_score,
        )

    async def _hybrid(
        self,
        query: str,
        top_k: int,
        min_score: float,
    ) -> list[SearchHit]:
        """Reciprocal Rank Fusion of semantic + BM25.

        We retrieve ``2*top_k`` from each leg so the fusion has enough
        headroom to reward chunks that appear in both lists. The final
        list is re-sorted by fused score and truncated to ``top_k``.
        """
        pool_size = max(top_k * 2, 10)

        semantic_hits = await self._semantic(query, pool_size, min_score=0.0)
        bm25_hits = self._index.search_bm25(query, top_k=pool_size)

        fused: dict[int, tuple[SearchHit, float]] = {}

        for rank, hit in enumerate(semantic_hits, start=1):
            fused[hit.chunk_id] = (hit, 1.0 / (RRF_K + rank))
        for rank, hit in enumerate(bm25_hits, start=1):
            if hit.chunk_id in fused:
                prev_hit, prev_score = fused[hit.chunk_id]
                # Keep the semantic SearchHit (carries the cosine
                # distance that the UI already displays); just bump the
                # fused score.
                fused[hit.chunk_id] = (prev_hit, prev_score + 1.0 / (RRF_K + rank))
            else:
                fused[hit.chunk_id] = (hit, 1.0 / (RRF_K + rank))

        ordered = sorted(fused.values(), key=lambda item: item[1], reverse=True)
        # Respect min_score on the semantic-leg only: if a chunk came
        # purely from BM25, we keep it; if it came from both, it
        # already passed the semantic gate.
        semantic_passed = {h.chunk_id for h in semantic_hits if h.score >= min_score}
        bm25_ids = {h.chunk_id for h in bm25_hits}
        filtered = [
            hit
            for (hit, _score) in ordered
            if hit.chunk_id in semantic_passed or hit.chunk_id in bm25_ids
        ]
        return filtered[:top_k]

    async def _synthesize(self, query: str, hits: list[SearchHit]) -> str:
        """Use the configured LLM to write a grounded answer from the retrieved chunks."""
        context_parts = []
        for i, hit in enumerate(hits, 1):
            note_name = hit.vault_path.rsplit("/", 1)[-1].removesuffix(".md")
            heading = hit.heading_context
            header = f"[{i}] From [[{note_name}]]"
            if heading:
                header += f" › {heading}"
            context_parts.append(f"{header}\n{hit.content}")

        context = "\n\n---\n\n".join(context_parts)
        user_message = f"Question: {query}\n\nContext from notes:\n\n{context}"

        try:
            response = await self._client.messages.create(
                model=self._synthesis_model,
                max_tokens=1024,
                system=SYNTHESIS_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            return response.content[0].text.strip()
        except Exception as e:
            logger.warning("Synthesis failed: %s", e)
            return ""
