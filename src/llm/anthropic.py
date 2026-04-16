"""AnthropicClient — wraps anthropic.AsyncAnthropic behind LLMClient."""
from __future__ import annotations

import base64
from typing import Any

from src.llm.client import LLMClient, LLMMessage, LLMResponse


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, sdk: Any | None = None, api_key: str | None = None):
        if sdk is not None:
            self._sdk = sdk
        else:
            import anthropic
            self._sdk = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        system: str,
        messages: list[LLMMessage],
        model: str,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        payload = [{"role": m.role, "content": m.content} for m in messages]
        result = await self._sdk.messages.create(
            model=model,
            system=system,
            messages=payload,
            max_tokens=max_tokens,
        )
        return self._to_response(result, model)

    async def complete_vision(
        self,
        system: str,
        image: bytes,
        prompt: str,
        model: str,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        img_b64 = base64.standard_b64encode(image).decode("ascii")
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": img_b64,
                },
            },
            {"type": "text", "text": prompt},
        ]
        result = await self._sdk.messages.create(
            model=model,
            system=system,
            messages=[{"role": "user", "content": content}],
            max_tokens=max_tokens,
        )
        return self._to_response(result, model)

    def _to_response(self, result: Any, model: str) -> LLMResponse:
        text = "".join(getattr(part, "text", "") for part in result.content)
        usage = getattr(result, "usage", None)
        return LLMResponse(
            text=text,
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            provider=self.provider,
            model=model,
        )
