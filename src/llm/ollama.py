"""OllamaClient — talks to a local Ollama server's REST API.

/api/chat drives text completions; /api/generate drives vision (where
image input is passed via the `images: [base64]` field — vision support
in /api/chat is inconsistent across Ollama versions).
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from src.llm.client import LLMClient, LLMMessage, LLMResponse


class OllamaClient(LLMClient):
    provider = "ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        timeout_seconds: float = 120.0,
        http: Any | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._http = http

    def _client(self) -> Any:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self._http

    async def complete(
        self,
        system: str,
        messages: list[LLMMessage],
        model: str,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        payload_messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        payload_messages.extend({"role": m.role, "content": m.content} for m in messages)
        body = {
            "model": model,
            "messages": payload_messages,
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        resp = await self._client().post(f"{self._base_url}/api/chat", json=body)
        resp.raise_for_status()
        data = resp.json()
        text = data.get("message", {}).get("content", "")
        return LLMResponse(
            text=text,
            input_tokens=int(data.get("prompt_eval_count", 0)),
            output_tokens=int(data.get("eval_count", 0)),
            provider=self.provider,
            model=model,
        )

    async def complete_vision(
        self,
        system: str,
        image: bytes,
        prompt: str,
        model: str,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        body = {
            "model": model,
            "prompt": prompt,
            "system": system,
            "images": [base64.standard_b64encode(image).decode("ascii")],
            "stream": False,
            "options": {"num_predict": max_tokens},
        }
        resp = await self._client().post(f"{self._base_url}/api/generate", json=body)
        resp.raise_for_status()
        data = resp.json()
        text = data.get("response", "")
        return LLMResponse(
            text=text,
            input_tokens=int(data.get("prompt_eval_count", 0)),
            output_tokens=int(data.get("eval_count", 0)),
            provider=self.provider,
            model=model,
        )
