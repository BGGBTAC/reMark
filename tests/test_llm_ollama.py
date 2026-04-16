"""OllamaClient — talks to /api/chat and /api/generate."""

from __future__ import annotations

import base64

from src.llm.client import LLMMessage
from src.llm.ollama import OllamaClient


class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _FakeHTTP:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url, json):
        self.calls.append((url, json))
        return _Resp(self.payload)


async def test_ollama_complete_text():
    payload = {
        "message": {"content": "hello from llama"},
        "prompt_eval_count": 8,
        "eval_count": 5,
    }
    http = _FakeHTTP(payload)
    client = OllamaClient(base_url="http://host:11434", http=http)
    resp = await client.complete(
        system="sys",
        messages=[LLMMessage(role="user", content="hi")],
        model="llama3.1",
    )
    assert resp.text == "hello from llama"
    assert resp.input_tokens == 8
    assert resp.output_tokens == 5
    assert resp.provider == "ollama"
    url, body = http.calls[0]
    assert url == "http://host:11434/api/chat"
    assert body["model"] == "llama3.1"
    assert body["stream"] is False
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1] == {"role": "user", "content": "hi"}


async def test_ollama_vision_encodes_image_base64():
    payload = {"response": "a cat", "prompt_eval_count": 3, "eval_count": 2}
    http = _FakeHTTP(payload)
    client = OllamaClient(base_url="http://host:11434", http=http)
    image_bytes = b"\x89PNG\r\n\x1a\n"
    resp = await client.complete_vision(
        system="sys",
        image=image_bytes,
        prompt="describe",
        model="llava",
    )
    assert resp.text == "a cat"
    url, body = http.calls[0]
    assert url == "http://host:11434/api/generate"
    assert body["model"] == "llava"
    assert body["system"] == "sys"
    assert body["prompt"] == "describe"
    assert body["images"] == [base64.standard_b64encode(image_bytes).decode("ascii")]
    assert body["stream"] is False


async def test_ollama_strips_trailing_slash_from_base_url():
    http = _FakeHTTP({"message": {"content": "ok"}, "prompt_eval_count": 0, "eval_count": 0})
    client = OllamaClient(base_url="http://host:11434/", http=http)
    await client.complete(system="s", messages=[LLMMessage(role="user", content="x")], model="m")
    url, _ = http.calls[0]
    assert url == "http://host:11434/api/chat"
