"""Transform raw OCR text into structured Markdown.

Uses the Anthropic API to clean up, structure, and format
handwritten notes into well-organized Markdown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import anthropic

logger = logging.getLogger(__name__)

STRUCTURE_PROMPT = """\
Structure these handwritten notes into clean Markdown.

Rules:
- Infer headings from context and visual emphasis
- Convert abbreviated words to full words where obvious
- Fix OCR errors where meaning is clear
- Preserve the author's intent and phrasing
- Add paragraph breaks at logical points
- Format lists, tables, and code blocks appropriately
- Keep [Diagram: ...] blocks as-is
- Keep ~~strikethrough~~ as-is
- DO NOT add content that isn't in the original
- DO NOT add commentary or explanations
- Return ONLY the structured Markdown"""

INCREMENTAL_PROMPT = """\
The following is an existing structured note, followed by new content
from additional pages. Merge the new content into the existing structure.

Rules:
- Maintain consistent heading hierarchy
- Append new content in logical sections
- Don't duplicate content that already exists
- If new content relates to existing sections, integrate it
- Keep the overall document coherent
- Return the complete merged document"""


@dataclass
class StructuredNote:
    """Result of note structuring."""

    title: str
    content_md: str
    detected_language: str


class NoteStructurer:
    """Transforms raw OCR text into structured Markdown via the Anthropic API."""

    def __init__(self, client: anthropic.AsyncAnthropic, model: str):
        self._client = client
        self._model = model

    async def structure(
        self,
        raw_text: str,
        notebook_name: str,
        page_numbers: list[int] | None = None,
    ) -> StructuredNote:
        """Structure raw OCR text into clean Markdown.

        Args:
            raw_text: Raw text from OCR pipeline.
            notebook_name: Name of the source notebook (used for title inference).
            page_numbers: Which pages this text came from.
        """
        if not raw_text.strip():
            return StructuredNote(
                title=notebook_name,
                content_md="",
                detected_language="unknown",
            )

        context = f"Notebook: {notebook_name}"
        if page_numbers:
            context += f"\nPages: {', '.join(str(p) for p in page_numbers)}"

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=STRUCTURE_PROMPT,
            messages=[{
                "role": "user",
                "content": f"{context}\n\n---\n\n{raw_text}",
            }],
        )

        content_md = response.content[0].text.strip()

        # Infer title from first heading or notebook name
        title = _extract_title(content_md) or notebook_name

        # Detect language from response
        language = await self._detect_language(content_md)

        return StructuredNote(
            title=title,
            content_md=content_md,
            detected_language=language,
        )

    async def structure_incremental(self, existing: str, new_pages: str) -> str:
        """Merge new page content into an existing structured note.

        Used when pages are added to an existing notebook.
        """
        if not new_pages.strip():
            return existing

        if not existing.strip():
            result = await self.structure(new_pages, "Untitled")
            return result.content_md

        response = await self._client.messages.create(
            model=self._model,
            max_tokens=8192,
            system=INCREMENTAL_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    f"## Existing Note\n\n{existing}\n\n"
                    f"## New Content\n\n{new_pages}"
                ),
            }],
        )

        return response.content[0].text.strip()

    async def _detect_language(self, text: str) -> str:
        """Quick language detection from text content."""
        # Simple heuristic — could use a library but this covers 90% of cases
        sample = text[:500].lower()

        german_markers = ["der", "die", "das", "und", "ist", "nicht", "ein", "für", "mit", "auf"]
        french_markers = ["les", "des", "une", "est", "pas", "pour", "dans", "que", "avec"]
        spanish_markers = ["los", "las", "una", "por", "para", "con", "que", "del"]

        words = set(sample.split())

        scores = {
            "de": sum(1 for m in german_markers if m in words),
            "fr": sum(1 for m in french_markers if m in words),
            "es": sum(1 for m in spanish_markers if m in words),
            "en": 0,  # default
        }

        best = max(scores, key=scores.get)
        if scores[best] >= 3:
            return best

        return "en"


def _extract_title(markdown: str) -> str | None:
    """Extract title from the first Markdown heading."""
    for line in markdown.split("\n"):
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return None
