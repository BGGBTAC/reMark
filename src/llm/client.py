"""LLMClient interface — shared by Anthropic, Ollama, future providers.

Consumers (processing, reports, OCR VLM) depend on this ABC rather than on
a vendor SDK, so switching providers is a config flip.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class LLMMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    provider: str
    model: str


class LLMClient(ABC):
    """Minimal surface covering text + vision completions."""

    provider: str

    @abstractmethod
    async def complete(
        self,
        system: str,
        messages: list[LLMMessage],
        model: str,
        max_tokens: int = 4096,
    ) -> LLMResponse: ...

    @abstractmethod
    async def complete_vision(
        self,
        system: str,
        image: bytes,
        prompt: str,
        model: str,
        max_tokens: int = 2048,
    ) -> LLMResponse: ...
