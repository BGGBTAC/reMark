"""OCR pipeline picks the Ollama vision model when llm.provider=ollama."""

from __future__ import annotations

from src.config import AppConfig
from src.llm.client import LLMClient, LLMResponse
from src.ocr.pipeline import build_pipeline


class _StubLLM(LLMClient):
    provider = "stub"

    async def complete(self, system, messages, model, max_tokens=4096):
        return LLMResponse(
            text="x",
            input_tokens=1,
            output_tokens=1,
            provider=self.provider,
            model=model,
        )

    async def complete_vision(self, system, image, prompt, model, max_tokens=2048):
        return LLMResponse(
            text="y",
            input_tokens=1,
            output_tokens=1,
            provider=self.provider,
            model=model,
        )


class _OllamaStub(_StubLLM):
    provider = "ollama"


class _AnthropicStub(_StubLLM):
    provider = "anthropic"


def test_pipeline_uses_ollama_vision_model_when_provider_is_ollama():
    cfg = AppConfig.model_validate(
        {
            "llm": {
                "provider": "ollama",
                "ollama": {"vision_model": "bakllava"},
            },
            "ocr": {
                "primary": "vlm",
                "fallback": "none",
                "vlm": {"model": "claude-old"},
            },
        }
    )
    pipeline = build_pipeline(cfg, llm_client=_OllamaStub())

    # The VLM engine is the primary — it's stored as pipeline._primary
    vlm = pipeline._primary
    assert vlm is not None, "Expected a VLM engine as primary"
    assert vlm._model == "bakllava", f"Expected Ollama vision_model 'bakllava', got '{vlm._model}'"


def test_pipeline_uses_anthropic_vlm_model_by_default():
    cfg = AppConfig.model_validate(
        {
            "llm": {"provider": "anthropic"},
            "ocr": {
                "primary": "vlm",
                "fallback": "none",
                "vlm": {"model": "claude-sonnet-4"},
            },
        }
    )
    pipeline = build_pipeline(cfg, llm_client=_AnthropicStub())

    vlm = pipeline._primary
    assert vlm is not None, "Expected a VLM engine as primary"
    assert vlm._model == "claude-sonnet-4", (
        f"Expected Anthropic model 'claude-sonnet-4', got '{vlm._model}'"
    )


def test_pipeline_ollama_fallback_also_uses_vision_model():
    """If VLM is the fallback engine, it also picks the Ollama vision model."""
    cfg = AppConfig.model_validate(
        {
            "llm": {
                "provider": "ollama",
                "ollama": {"vision_model": "llava-next"},
            },
            "ocr": {
                "primary": "remarkable_builtin",
                "fallback": "vlm",
                "vlm": {"model": "claude-old"},
            },
        }
    )
    pipeline = build_pipeline(cfg, llm_client=_OllamaStub())

    vlm = pipeline._fallback
    assert vlm is not None, "Expected a VLM engine as fallback"
    assert vlm._model == "llava-next", (
        f"Expected Ollama vision_model 'llava-next', got '{vlm._model}'"
    )
