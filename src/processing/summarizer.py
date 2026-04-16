"""Note summarization.

Generates concise summaries of notes for quick reference,
Obsidian frontmatter, and response PDFs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.llm.client import LLMClient, LLMMessage

logger = logging.getLogger(__name__)

SUMMARY_PROMPT = """\
Summarize this note concisely. Return a JSON object:
{
  "one_line": "Single sentence summary (max 120 chars)",
  "key_points": ["3-5 bullet points capturing the essentials"],
  "topics": ["2-4 main topics discussed"]
}

Rules:
- Be specific, not generic
- Preserve names, dates, numbers
- Keep the author's terminology
- Return ONLY valid JSON, no explanation"""


@dataclass
class NoteSummary:
    """Summary of a note at different levels of detail."""

    one_line: str
    key_points: list[str]
    topics: list[str]


class NoteSummarizer:
    """Generate note summaries via the Anthropic API."""

    def __init__(self, llm: LLMClient, model: str):
        self._llm = llm
        self._model = model

    async def summarize(self, text: str, notebook_name: str = "") -> NoteSummary:
        """Generate a multi-level summary of a note."""
        if not text.strip():
            return NoteSummary(
                one_line=f"Empty note: {notebook_name}" if notebook_name else "Empty note",
                key_points=[],
                topics=[],
            )

        try:
            context = f"Notebook: {notebook_name}\n\n" if notebook_name else ""

            response = await self._llm.complete(
                system=SUMMARY_PROMPT,
                messages=[LLMMessage(role="user", content=f"{context}{text[:4000]}")],
                model=self._model,
                max_tokens=512,
            )

            return _parse_summary_response(response.text.strip(), notebook_name)

        except Exception as e:
            logger.warning("Summarization failed: %s", e)
            return _fallback_summary(text, notebook_name)

    async def summarize_batch(self, notes: list[tuple[str, str]]) -> list[NoteSummary]:
        """Summarize multiple notes. Each entry is (text, notebook_name)."""
        import asyncio

        tasks = [self.summarize(text, name) for text, name in notes]
        return await asyncio.gather(*tasks)


def _parse_summary_response(raw: str, notebook_name: str) -> NoteSummary:
    """Parse the JSON response into a NoteSummary."""
    import json

    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    try:
        data = json.loads(raw)
        return NoteSummary(
            one_line=data.get("one_line", "")[:120],
            key_points=data.get("key_points", []),
            topics=data.get("topics", []),
        )
    except (json.JSONDecodeError, KeyError):
        logger.warning("Failed to parse summary response, using fallback")
        return NoteSummary(
            one_line=raw[:120] if raw else f"Summary of {notebook_name}",
            key_points=[],
            topics=[],
        )


def _fallback_summary(text: str, notebook_name: str) -> NoteSummary:
    """Generate a basic summary without the API."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    # Use first non-heading line as one-liner
    one_line = ""
    key_points = []
    for line in lines[:10]:
        clean = line.lstrip("#").strip()
        if not one_line and len(clean) > 5:
            one_line = clean[:120]
        elif clean.startswith("- ") or clean.startswith("* "):
            key_points.append(clean[2:])

    if not one_line:
        one_line = f"Notes from {notebook_name}" if notebook_name else "Handwritten notes"

    return NoteSummary(
        one_line=one_line,
        key_points=key_points[:5],
        topics=[],
    )
