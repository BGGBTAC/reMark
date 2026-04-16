"""Factory selects the right LLMClient per config."""

from __future__ import annotations

from unittest.mock import patch

from src.config import LLMConfig, OllamaConfig
from src.llm.anthropic import AnthropicClient
from src.llm.factory import build_llm_client
from src.llm.ollama import OllamaClient


def test_factory_returns_anthropic_by_default():
    cfg = LLMConfig()
    with patch("src.llm.anthropic.AnthropicClient.__init__", return_value=None) as init:
        client = build_llm_client(cfg, anthropic_api_key="sk-xxx")
    assert isinstance(client, AnthropicClient)
    init.assert_called_once()
    assert init.call_args.kwargs == {"api_key": "sk-xxx"}


def test_factory_returns_ollama_when_configured():
    cfg = LLMConfig(
        provider="ollama",
        ollama=OllamaConfig(base_url="http://host:11434", timeout_seconds=60.0),
    )
    client = build_llm_client(cfg, anthropic_api_key=None)
    assert isinstance(client, OllamaClient)
    assert client.provider == "ollama"


def test_factory_passes_ollama_base_url_and_timeout():
    cfg = LLMConfig(
        provider="ollama",
        ollama=OllamaConfig(base_url="http://gpu:11434", timeout_seconds=45.0),
    )
    client = build_llm_client(cfg, anthropic_api_key=None)
    assert client._base_url == "http://gpu:11434"
    assert client._timeout == 45.0
