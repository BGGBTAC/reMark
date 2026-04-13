"""OCR engine using Vision Language Models (Claude, GPT-4o).

Sends rendered page images to a VLM with a structured prompt that
preserves document structure (headings, lists, tables, diagrams).

Cost varies by model — roughly $0.50-2.00 per page depending on provider.
"""

from __future__ import annotations

import base64
import logging

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
    """Vision Language Model OCR with structure preservation."""

    def __init__(
        self,
        provider: str = "anthropic",
        model: str = "claude-sonnet-4-20250514",
        api_key: str | None = None,
        prompt: str = DEFAULT_PROMPT,
    ):
        self._provider = provider
        self._model = model
        self._api_key = api_key
        self._prompt = prompt

    @property
    def name(self) -> str:
        return f"vlm_{self._provider}"

    async def recognize_page(self, page_image: bytes) -> OCRResult:
        """Send page image to VLM for transcription."""
        if self._provider == "anthropic":
            return await self._recognize_anthropic(page_image)
        elif self._provider == "openai":
            return await self._recognize_openai(page_image)
        else:
            raise ValueError(f"Unknown VLM provider: {self._provider}")

    async def _recognize_anthropic(self, page_image: bytes) -> OCRResult:
        """Use Anthropic's Claude API for vision-based OCR."""
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=self._api_key)
        image_b64 = base64.b64encode(page_image).decode("ascii")

        response = await client.messages.create(
            model=self._model,
            max_tokens=4096,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": image_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": self._prompt,
                    },
                ],
            }],
        )

        text = response.content[0].text.strip()
        confidence = _estimate_confidence(text)

        logger.debug(
            "Anthropic VLM: %d chars, estimated confidence %.2f",
            len(text), confidence,
        )

        return OCRResult(
            text=text,
            confidence=confidence,
            engine=self.name,
        )

    async def _recognize_openai(self, page_image: bytes) -> OCRResult:
        """Use OpenAI's GPT-4o API for vision-based OCR."""
        import httpx

        image_b64 = base64.b64encode(page_image).decode("ascii")

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "max_tokens": 4096,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{image_b64}",
                                },
                            },
                            {
                                "type": "text",
                                "text": self._prompt,
                            },
                        ],
                    }],
                },
            )

        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        confidence = _estimate_confidence(text)

        logger.debug(
            "OpenAI VLM: %d chars, estimated confidence %.2f",
            len(text), confidence,
        )

        return OCRResult(
            text=text,
            confidence=confidence,
            engine=self.name,
        )


def _estimate_confidence(text: str) -> float:
    """Heuristic confidence estimate based on response quality.

    VLMs don't return per-word confidence, so we estimate based on
    output characteristics.
    """
    if not text.strip():
        return 0.0

    # Very short responses suggest low-quality recognition
    if len(text) < 10:
        return 0.3

    # Check for common "I can't read this" patterns
    low_confidence_markers = [
        "unreadable", "illegible", "cannot make out",
        "unable to transcribe", "blank page", "empty page",
    ]
    text_lower = text.lower()
    if any(marker in text_lower for marker in low_confidence_markers):
        return 0.2

    # Reasonable length with actual content
    if len(text) > 50:
        return 0.85

    return 0.7
