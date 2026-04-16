"""OCR engine using a Vision Language Model via LLMClient.

Delegates vision inference to whatever LLMClient is configured
(Anthropic, OpenAI, Ollama's llava, etc.). The OCR pipeline wires
this up from config.llm.provider.
"""

from __future__ import annotations

import logging

from src.llm.client import LLMClient
from src.ocr.pipeline import OCREngine, OCRResult

logger = logging.getLogger(__name__)

DEFAULT_PROMPT = """Transcribe this handwritten page to Markdown. Preserve:
- Headings and hierarchy
- Bullet points and numbered lists
- Underlined or emphasized text
- Diagrams: describe in [Diagram: description] blocks
- Tables: use Markdown table syntax
- Crossed-out text: wrap in ~~strikethrough~~
- Mathematical notation: use LaTeX syntax in $..$ blocks

Return ONLY the transcribed content, no commentary or explanation.
If the page is blank or unreadable, return an empty string."""


class VLMOcr(OCREngine):
    """Vision-LLM OCR with structure preservation."""

    def __init__(
        self,
        llm: LLMClient,
        model: str,
        prompt: str = DEFAULT_PROMPT,
    ):
        self._llm = llm
        self._model = model
        self._prompt = prompt

    @property
    def name(self) -> str:
        return f"vlm_{self._llm.provider}"

    async def recognize_page(self, page_image: bytes) -> OCRResult:
        response = await self._llm.complete_vision(
            system="",
            image=page_image,
            prompt=self._prompt,
            model=self._model,
        )
        # Generative VLMs don't expose token-level confidence; treat non-empty
        # output as confident and an empty response as a dropout.
        confidence = 1.0 if response.text.strip() else 0.0
        return OCRResult(
            text=response.text,
            confidence=confidence,
            engine=self.name,
        )
