"""Action item extraction from handwritten notes.

Detects tasks, questions, follow-ups, and decisions using both
text pattern matching and color-coded annotations from the tablet.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import anthropic

from src.remarkable.formats import StrokeGroup

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """\
Extract action items from these notes. For each item, return a JSON array where each element has:
{
  "task": "description of the task",
  "assignee": "person name or null",
  "deadline": "date string or null",
  "priority": "high" | "medium" | "low",
  "source_context": "surrounding text for reference",
  "type": "task" | "question" | "followup" | "decision"
}

Detection rules:
- Commitment verbs: "will", "need to", "should", "must", "going to"
- Deadline mentions: dates, "by Friday", "next week", "ASAP", "EOD"
- Questions: question marks, "Q:", "how", "why", "what if"
- Patterns: "TODO", "ACTION", "FOLLOW UP", "DECISION", checkbox patterns
- Red/blue annotations (if mentioned) indicate action items / questions

Return ONLY the JSON array. If no action items found, return [].
Do not wrap in markdown code blocks."""


@dataclass
class ActionItem:
    """A single extracted action item."""

    task: str
    type: str = "task"  # task | question | followup | decision
    assignee: str | None = None
    deadline: str | None = None
    priority: str = "medium"  # high | medium | low
    source_context: str = ""
    page_id: str = ""
    color: str | None = None  # pen color that triggered detection


class ActionExtractor:
    """Extract action items from notes using text patterns and the Anthropic API."""

    def __init__(self, client: anthropic.AsyncAnthropic, model: str):
        self._client = client
        self._model = model

    async def extract(
        self,
        text: str,
        color_annotations: dict[str, list[StrokeGroup]] | None = None,
    ) -> list[ActionItem]:
        """Extract action items from structured note text.

        Combines API-based extraction with regex pattern matching
        and color-coded annotations.

        Args:
            text: Structured note text (Markdown).
            color_annotations: {page_id: [StrokeGroup]} for colored strokes.
        """
        if not text.strip():
            return []

        # Run pattern-based extraction and API extraction
        pattern_actions = _extract_by_pattern(text)
        color_actions = _extract_by_color(color_annotations) if color_annotations else []

        # API-based extraction for richer understanding
        api_actions = await self._extract_via_api(text, color_annotations)

        # Merge and deduplicate
        all_actions = _merge_actions(api_actions, pattern_actions, color_actions)

        logger.info(
            "Extracted %d actions (API: %d, pattern: %d, color: %d)",
            len(all_actions), len(api_actions), len(pattern_actions), len(color_actions),
        )

        return all_actions

    async def _extract_via_api(
        self,
        text: str,
        color_annotations: dict[str, list[StrokeGroup]] | None,
    ) -> list[ActionItem]:
        """Use Claude to extract action items."""
        user_content = text

        if color_annotations:
            annotation_summary = _summarize_annotations(color_annotations)
            user_content += f"\n\n--- Color Annotations ---\n{annotation_summary}"

        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=EXTRACTION_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )

            raw = response.content[0].text.strip()
            return _parse_action_response(raw)

        except Exception as e:
            logger.warning("API action extraction failed: %s", e)
            return []


def _extract_by_pattern(text: str) -> list[ActionItem]:
    """Extract action items using regex patterns."""
    actions: list[ActionItem] = []

    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue

        # TODO: / ACTION: / FOLLOW UP:
        if match := re.match(r"(?:TODO|ACTION|FOLLOW\s*UP)\s*:\s*(.+)", stripped, re.IGNORECASE):
            actions.append(ActionItem(
                task=match.group(1).strip(),
                type="task",
                priority="high",
                source_context=stripped,
            ))
            continue

        # Q: questions
        if match := re.match(r"Q\s*:\s*(.+)", stripped):
            actions.append(ActionItem(
                task=match.group(1).strip(),
                type="question",
                priority="medium",
                source_context=stripped,
            ))
            continue

        # Unchecked checkboxes: - [ ] task
        if match := re.match(r"-\s*\[\s*\]\s*(.+)", stripped):
            actions.append(ActionItem(
                task=match.group(1).strip(),
                type="task",
                priority="medium",
                source_context=stripped,
            ))
            continue

        # ! priority markers
        if match := re.match(r"!\s*(.+)", stripped):
            actions.append(ActionItem(
                task=match.group(1).strip(),
                type="task",
                priority="high",
                source_context=stripped,
            ))
            continue

        # Lines ending with ? are potential questions
        if stripped.endswith("?") and len(stripped) > 10:
            actions.append(ActionItem(
                task=stripped,
                type="question",
                priority="low",
                source_context=stripped,
            ))

    return actions


def _extract_by_color(
    annotations: dict[str, list[StrokeGroup]],
) -> list[ActionItem]:
    """Create action items from color-coded strokes."""
    actions: list[ActionItem] = []

    for page_id, groups in annotations.items():
        for group in groups:
            action_type = "task"
            if group.color_name == "blue":
                action_type = "question"
            elif group.color_name == "yellow":
                action_type = "followup"
            elif group.color_name == "green":
                continue  # green = done/approved, skip

            actions.append(ActionItem(
                task=f"[{group.color_name} annotation on page {page_id[:8]}]",
                type=action_type,
                priority="medium",
                page_id=page_id,
                color=group.color_name,
            ))

    return actions


def _summarize_annotations(annotations: dict[str, list[StrokeGroup]]) -> str:
    """Summarize color annotations for the API prompt."""
    parts = []
    for page_id, groups in annotations.items():
        for group in groups:
            bbox = group.bbox
            parts.append(
                f"- Page {page_id[:8]}: {group.color_name} strokes "
                f"at region ({bbox[0]:.0f},{bbox[1]:.0f})-({bbox[2]:.0f},{bbox[3]:.0f})"
            )
    return "\n".join(parts) if parts else "None"


def _parse_action_response(raw: str) -> list[ActionItem]:
    """Parse the JSON response from the API into ActionItem objects."""
    # Strip markdown code blocks if present
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse action response as JSON: %s...", raw[:100])
        return []

    if not isinstance(items, list):
        return []

    actions = []
    for item in items:
        if not isinstance(item, dict) or "task" not in item:
            continue
        actions.append(ActionItem(
            task=item["task"],
            type=item.get("type", "task"),
            assignee=item.get("assignee"),
            deadline=item.get("deadline"),
            priority=item.get("priority", "medium"),
            source_context=item.get("source_context", ""),
        ))

    return actions


def _merge_actions(
    api: list[ActionItem],
    pattern: list[ActionItem],
    color: list[ActionItem],
) -> list[ActionItem]:
    """Merge actions from different sources, deduplicating by task similarity."""
    # API results are usually the most complete — start with those
    merged = list(api)
    seen_tasks = {a.task.lower().strip() for a in merged}

    # Add pattern-matched items that weren't caught by the API
    for action in pattern:
        task_lower = action.task.lower().strip()
        if task_lower not in seen_tasks:
            merged.append(action)
            seen_tasks.add(task_lower)

    # Color annotations are always unique (spatial, not textual)
    merged.extend(color)

    return merged
