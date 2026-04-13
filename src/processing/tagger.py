"""Auto-tagging for notes based on content analysis.

Combines keyword-based tagging with API-powered categorization
for richer, context-aware tags.
"""

from __future__ import annotations

import logging
import re

import anthropic

logger = logging.getLogger(__name__)

TAGGING_PROMPT = """\
Analyze this note and return relevant tags as a JSON array of strings.

Rules:
- Return 3-8 tags
- Tags should be lowercase, hyphenated (e.g. "project-planning")
- Include: topic tags, format tags (meeting, brainstorm, journal), domain tags
- DO NOT include generic tags like "notes" or "text"
- Return ONLY the JSON array, no explanation

Example: ["weekly-standup", "backend", "performance", "q2-goals"]"""

# Keyword patterns for common note types
KEYWORD_TAGS: dict[str, list[str]] = {
    "meeting": [
        r"\bmeeting\b", r"\battendees?\b", r"\bagenda\b",
        r"\bminutes\b", r"\bstandup\b", r"\bsync\b",
    ],
    "brainstorm": [
        r"\bbrainstorm\b", r"\bideas?\b", r"\bwhat if\b",
        r"\bconcepts?\b",
    ],
    "planning": [
        r"\btimeline\b", r"\bdeadline\b", r"\bmilestone\b",
        r"\broadmap\b", r"\bsprint\b", r"\bplan\b",
    ],
    "review": [
        r"\breview\b", r"\bfeedback\b", r"\bretro\b",
        r"\bretrospective\b", r"\blessons learned\b",
    ],
    "journal": [
        r"\btoday\b.*\bI\b", r"\bfeeling\b", r"\breflect\b",
        r"\bjournal\b", r"\bdiary\b",
    ],
    "research": [
        r"\bsource\b", r"\breference\b", r"\bstudy\b",
        r"\bpaper\b", r"\bfindings?\b",
    ],
    "technical": [
        r"\bapi\b", r"\bdatabase\b", r"\bserver\b",
        r"\bdeployment\b", r"\bbug\b", r"\bconfig\b",
    ],
    "reading-notes": [
        r"\bchapter\b", r"\bauthor\b", r"\bbook\b",
        r"\bquote\b", r"\bpage\s*\d+",
    ],
}


class NoteTagger:
    """Auto-tag notes based on content."""

    def __init__(self, client: anthropic.AsyncAnthropic, model: str):
        self._client = client
        self._model = model

    async def tag(self, text: str, notebook_name: str = "") -> list[str]:
        """Generate tags for a note.

        Combines keyword matching with API-based categorization.
        """
        if not text.strip():
            return []

        keyword_tags = _extract_keyword_tags(text)
        api_tags = await self._tag_via_api(text, notebook_name)

        # Merge, keyword tags first (more reliable), then API tags
        seen = set()
        merged = []
        for tag in keyword_tags + api_tags:
            tag = tag.lower().strip()
            if tag and tag not in seen:
                merged.append(tag)
                seen.add(tag)

        return merged[:10]  # cap at 10 tags

    async def _tag_via_api(self, text: str, notebook_name: str) -> list[str]:
        """Use Claude to generate contextual tags."""
        try:
            context = f"Notebook: {notebook_name}\n\n" if notebook_name else ""

            response = await self._client.messages.create(
                model=self._model,
                max_tokens=256,
                system=TAGGING_PROMPT,
                messages=[{
                    "role": "user",
                    "content": f"{context}{text[:2000]}",  # cap input
                }],
            )

            raw = response.content[0].text.strip()
            return _parse_tag_response(raw)

        except Exception as e:
            logger.warning("API tagging failed: %s", e)
            return []


def _extract_keyword_tags(text: str) -> list[str]:
    """Extract tags based on keyword pattern matching."""
    text_lower = text.lower()
    tags = []

    for tag, patterns in KEYWORD_TAGS.items():
        for pattern in patterns:
            if re.search(pattern, text_lower):
                tags.append(tag)
                break

    return tags


def _parse_tag_response(raw: str) -> list[str]:
    """Parse the JSON array response from the API."""
    import json

    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    try:
        tags = json.loads(raw)
        if isinstance(tags, list):
            return [str(t) for t in tags if isinstance(t, str)]
    except json.JSONDecodeError:
        logger.warning("Failed to parse tag response: %s...", raw[:80])

    return []
