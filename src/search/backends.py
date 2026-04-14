"""Embedding backend strategies for semantic search.

Mirrors the pattern used by the OCR pipeline (strategy + fallback).
All backends implement the EmbeddingBackend protocol, returning
normalized float vectors.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

# Default models per backend
DEFAULT_MODELS = {
    "voyage": "voyage-3.5",
    "openai": "text-embedding-3-small",
    "local": "all-MiniLM-L6-v2",
}


class EmbeddingError(Exception):
    """Raised when an embedding backend fails."""


class EmbeddingBackend(ABC):
    """Protocol for embedding backends."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Vector dimension produced by this backend."""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns one vector per input."""


class VoyageBackend(EmbeddingBackend):
    """Voyage AI embeddings (Anthropic's recommended partner)."""

    def __init__(self, api_key: str, model: str = ""):
        self._api_key = api_key
        self._model = model or DEFAULT_MODELS["voyage"]
        self._client = None
        # voyage-3.5 has 1024 dimensions
        self._dimension = 1024

    @property
    def name(self) -> str:
        return "voyage"

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        if self._client is None:
            try:
                import voyageai
                self._client = voyageai.AsyncClient(api_key=self._api_key)
            except ImportError as e:
                raise EmbeddingError(
                    "voyageai package not installed. "
                    "Install with: pip install 'remark-bridge[voyage]'"
                ) from e

        try:
            result = await self._client.embed(texts, model=self._model)
            return result.embeddings
        except Exception as e:
            raise EmbeddingError(f"Voyage embedding failed: {e}") from e


class OpenAIBackend(EmbeddingBackend):
    """OpenAI embeddings (text-embedding-3-*)."""

    def __init__(self, api_key: str, model: str = ""):
        self._api_key = api_key
        self._model = model or DEFAULT_MODELS["openai"]
        self._client = None
        # text-embedding-3-small = 1536 dimensions
        self._dimension = 1536 if "small" in self._model else 3072

    @property
    def name(self) -> str:
        return "openai"

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        if self._client is None:
            try:
                from openai import AsyncOpenAI
                self._client = AsyncOpenAI(api_key=self._api_key)
            except ImportError as e:
                raise EmbeddingError(
                    "openai package not installed. "
                    "Install with: pip install 'remark-bridge[openai]'"
                ) from e

        try:
            response = await self._client.embeddings.create(
                model=self._model,
                input=texts,
            )
            return [d.embedding for d in response.data]
        except Exception as e:
            raise EmbeddingError(f"OpenAI embedding failed: {e}") from e


class LocalBackend(EmbeddingBackend):
    """Local embeddings via sentence-transformers."""

    def __init__(self, model: str = ""):
        self._model_name = model or DEFAULT_MODELS["local"]
        self._model = None
        # all-MiniLM-L6-v2 = 384 dimensions
        self._dimension = 384

    @property
    def name(self) -> str:
        return "local"

    @property
    def dimension(self) -> int:
        return self._dimension

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self._model_name)
                self._dimension = self._model.get_sentence_embedding_dimension()
            except ImportError as e:
                raise EmbeddingError(
                    "sentence-transformers not installed. "
                    "Install with: pip install 'remark-bridge[local-embeddings]'"
                ) from e

        try:
            import asyncio

            # sentence-transformers is sync, run in executor
            loop = asyncio.get_event_loop()
            vectors = await loop.run_in_executor(
                None, lambda: self._model.encode(texts, convert_to_numpy=False),
            )
            # Convert torch tensors / numpy arrays to lists of floats
            return [list(map(float, v)) for v in vectors]
        except Exception as e:
            raise EmbeddingError(f"Local embedding failed: {e}") from e


def build_backend(
    backend_name: str,
    model: str = "",
    api_key_env: str = "",
) -> EmbeddingBackend:
    """Build an embedding backend from configuration."""
    if backend_name == "voyage":
        env_var = api_key_env or "VOYAGE_API_KEY"
        api_key = os.environ.get(env_var, "")
        if not api_key:
            raise EmbeddingError(
                f"Voyage backend requires {env_var} environment variable"
            )
        return VoyageBackend(api_key=api_key, model=model)

    if backend_name == "openai":
        env_var = api_key_env or "OPENAI_API_KEY"
        api_key = os.environ.get(env_var, "")
        if not api_key:
            raise EmbeddingError(
                f"OpenAI backend requires {env_var} environment variable"
            )
        return OpenAIBackend(api_key=api_key, model=model)

    if backend_name == "local":
        return LocalBackend(model=model)

    raise EmbeddingError(f"Unknown embedding backend: {backend_name}")
