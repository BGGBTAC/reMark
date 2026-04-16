"""Bridge API — vault search for the Obsidian companion plugin.

Exposes semantic, BM25, and hybrid search through a simple POST endpoint
secured with the same Bearer token as /api/push. When the search index is
not configured the endpoint returns 503 rather than crashing, so clients
can degrade gracefully.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.web.api_notes import _bridge_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["bridge"])


class SearchBody(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    mode: Literal["semantic", "bm25", "hybrid"] = "hybrid"
    limit: int = Field(10, ge=1, le=50)


@router.post("/search")
async def search(
    request: Request,
    body: SearchBody,
):
    """Search the indexed vault.

    Accepts ``semantic``, ``bm25``, or ``hybrid`` (default) mode.
    Returns up to ``limit`` hits, each with a vault path, content snippet,
    and relevance score in [0, 1].

    Returns 503 when the search index hasn't been configured for this
    install (no embedding backend set up). Auth: Bearer token.
    """
    _bridge_auth(request)

    search_query = getattr(request.app.state, "search_query", None)
    if search_query is None:
        raise HTTPException(
            status_code=503,
            detail="search not configured — set up an embedding backend first",
        )

    result = await search_query.ask(
        query=body.query,
        mode=body.mode,
        top_k=body.limit,
        synthesize=False,
    )

    return {
        "hits": [
            {
                "path": h.vault_path,
                "snippet": h.content,
                "score": round(h.score, 4),
            }
            for h in result.hits
        ],
    }
