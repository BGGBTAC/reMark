"""VLMOcr — delegates vision to any LLMClient (Anthropic or Ollama)."""
from __future__ import annotations

from src.llm.client import LLMClient, LLMMessage, LLMResponse
from src.ocr.vlm import VLMOcr


class _VisionStub(LLMClient):
    provider = "stub"

    def __init__(self, text: str = "heading\n\nbody"):
        self._text = text
        self.calls: list = []

    async def complete(self, system, messages, model, max_tokens=4096):
        raise NotImplementedError

    async def complete_vision(self, system, image, prompt, model, max_tokens=2048):
        self.calls.append((system, image, prompt, model))
        return LLMResponse(
            text=self._text,
            input_tokens=10, output_tokens=5,
            provider=self.provider, model=model,
        )


async def test_vlm_ocr_delegates_to_llm_client():
    llm = _VisionStub()
    ocr = VLMOcr(llm=llm, model="llava")
    result = await ocr.recognize_page(b"\x89PNG\r\n")
    assert "heading" in result.text
    assert result.engine == "vlm_stub"
    assert result.confidence == 1.0
    assert llm.calls and llm.calls[0][3] == "llava"


async def test_vlm_ocr_reports_zero_confidence_on_empty_text():
    llm = _VisionStub(text="")
    ocr = VLMOcr(llm=llm, model="llava")
    result = await ocr.recognize_page(b"\x89PNG\r\n")
    assert result.text == ""
    assert result.confidence == 0.0


async def test_vlm_ocr_uses_configured_prompt():
    llm = _VisionStub()
    ocr = VLMOcr(llm=llm, model="llava", prompt="my custom prompt")
    await ocr.recognize_page(b"\x00")
    _, _, used_prompt, _ = llm.calls[0]
    assert used_prompt == "my custom prompt"
