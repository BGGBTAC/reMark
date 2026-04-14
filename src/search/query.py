"""Query pipeline: embed a question, retrieve relevant chunks,
optionally synthesize an answer with Claude.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import anthropic

from src.search.backends import EmbeddingBackend
from src.search.index import SearchHit, VectorIndex

logger = logging.getLogger(__name__)


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
    ) -> QueryResult:
        """Run a semantic search and optionally synthesize an answer.

        Args:
            query: Natural language question.
            top_k: Max number of chunks to retrieve.
            min_score: Minimum similarity score (0..1).
            synthesize: If True, use Claude to synthesize an answer from hits.
        """
        if not query.strip():
            return QueryResult(query=query, hits=[])

        query_vectors = await self._backend.embed([query])
        if not query_vectors:
            return QueryResult(query=query, hits=[])

        hits = self._index.search(
            query_vector=query_vectors[0],
            top_k=top_k,
            min_score=min_score,
        )

        logger.info(
            "Query '%s...' returned %d hits (backend=%s)",
            query[:40], len(hits), self._backend.name,
        )

        answer = ""
        if synthesize and hits and self._client is not None:
            answer = await self._synthesize(query, hits)

        return QueryResult(query=query, hits=hits, answer=answer)

    async def _synthesize(self, query: str, hits: list[SearchHit]) -> str:
        """Use Claude to write a grounded answer from the retrieved chunks."""
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
