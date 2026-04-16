"""LLMConfig defaults + overrides."""

from __future__ import annotations

from src.config import AppConfig, LLMConfig, OllamaConfig


def test_llm_default_is_anthropic():
    cfg = LLMConfig()
    assert cfg.provider == "anthropic"


def test_ollama_defaults_are_localhost():
    cfg = OllamaConfig()
    assert cfg.base_url == "http://localhost:11434"
    assert cfg.text_model == "llama3.1"
    assert cfg.vision_model == "llava"
    assert cfg.embedding_model == "nomic-embed-text"
    assert cfg.timeout_seconds == 120.0


def test_config_accepts_llm_block():
    raw = {
        "llm": {
            "provider": "ollama",
            "ollama": {"base_url": "http://gpu-host:11434", "text_model": "mistral"},
        },
    }
    cfg = AppConfig.model_validate(raw)
    assert cfg.llm.provider == "ollama"
    assert cfg.llm.ollama.base_url == "http://gpu-host:11434"
    assert cfg.llm.ollama.text_model == "mistral"


def test_config_default_has_anthropic_llm():
    cfg = AppConfig.model_validate({})
    assert cfg.llm.provider == "anthropic"
    assert cfg.llm.ollama.base_url == "http://localhost:11434"
