"""OllamaEmbeddingBackend — /api/embeddings against a local Ollama server."""
from __future__ import annotations


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _FakeHTTP:
    def __init__(self, payloads):
        # list so we can return different vectors per call
        self._payloads = list(payloads)
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url, json):
        self.calls.append((url, json))
        return _Resp(self._payloads.pop(0))


async def test_ollama_backend_embeds_each_text():
    from src.search.backends import OllamaEmbeddingBackend

    http = _FakeHTTP([
        {"embedding": [0.1, 0.2, 0.3]},
        {"embedding": [0.4, 0.5, 0.6]},
    ])
    backend = OllamaEmbeddingBackend(
        base_url="http://host:11434",
        model="nomic-embed-text",
        http=http,
    )
    vectors = await backend.embed(["foo", "bar"])
    assert len(vectors) == 2
    assert vectors[0] == [0.1, 0.2, 0.3]
    assert vectors[1] == [0.4, 0.5, 0.6]
    # Two separate POSTs to the same endpoint
    assert len(http.calls) == 2
    assert http.calls[0][0] == "http://host:11434/api/embeddings"
    assert http.calls[0][1] == {"model": "nomic-embed-text", "prompt": "foo"}
    assert http.calls[1][1] == {"model": "nomic-embed-text", "prompt": "bar"}


async def test_ollama_backend_empty_list_is_noop():
    from src.search.backends import OllamaEmbeddingBackend

    http = _FakeHTTP([])
    backend = OllamaEmbeddingBackend(model="nomic-embed-text", http=http)
    assert await backend.embed([]) == []
    assert http.calls == []


def test_ollama_backend_name_is_ollama():
    from src.search.backends import OllamaEmbeddingBackend
    backend = OllamaEmbeddingBackend(model="nomic-embed-text")
    assert backend.name == "ollama"


def test_ollama_backend_known_dimensions():
    from src.search.backends import OllamaEmbeddingBackend

    assert OllamaEmbeddingBackend(model="nomic-embed-text").dimension == 768
    assert OllamaEmbeddingBackend(model="mxbai-embed-large").dimension == 1024
    assert OllamaEmbeddingBackend(model="all-minilm").dimension == 384


def test_ollama_backend_unknown_model_falls_back_to_768():
    from src.search.backends import OllamaEmbeddingBackend
    # Unknown models: assume 768 (most common for Ollama embedding models today)
    assert OllamaEmbeddingBackend(model="custom-model-xyz").dimension == 768


def test_ollama_backend_strips_trailing_slash():
    from src.search.backends import OllamaEmbeddingBackend
    b = OllamaEmbeddingBackend(base_url="http://host:11434/", model="nomic-embed-text")
    assert b._base_url == "http://host:11434"
