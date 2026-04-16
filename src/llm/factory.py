"""Build the right LLMClient for a Config.llm section.

Anthropic needs an API key at construction; Ollama needs the base URL
and timeout. Both present the same consumer-facing surface.
"""
from __future__ import annotations

from src.config import LLMConfig
from src.llm.anthropic import AnthropicClient
from src.llm.client import LLMClient
from src.llm.ollama import OllamaClient


def build_llm_client(cfg: LLMConfig, *, anthropic_api_key: str | None) -> LLMClient:
    if cfg.provider == "ollama":
        return OllamaClient(
            base_url=cfg.ollama.base_url,
            timeout_seconds=cfg.ollama.timeout_seconds,
        )
    return AnthropicClient(api_key=anthropic_api_key)
