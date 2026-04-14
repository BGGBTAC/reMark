"""Response generation orchestrator.

Reads a synced Obsidian note, optionally runs Claude for Q&A and analysis,
and produces either a PDF or a native reMarkable notebook ready for upload.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import anthropic

from src.config import ResponseConfig
from src.obsidian.vault import ObsidianVault
from src.response.notebook_writer import NotebookWriter
from src.response.pdf_generator import ResponseContent, ResponsePDFGenerator

logger = logging.getLogger(__name__)


QA_SYSTEM_PROMPT = """\
You are reading someone's handwritten notes. The author has marked
questions in their notes (prefixed with "Q:" or written in blue ink).
Answer each question concisely and directly based on your general
knowledge. If a question requires information beyond what you can
reliably answer, say so.

Return a JSON array:
[
  {"question": "original question text", "answer": "your concise answer"}
]

Return ONLY the JSON array, no commentary."""


ANALYSIS_SYSTEM_PROMPT = """\
Read these handwritten notes and provide a brief analysis.
Focus on:
- Key insights or themes you notice
- Potential gaps in reasoning or open questions
- Suggestions for follow-up or deeper exploration

Keep it under 300 words. Be specific, not generic. Use the author's
own terminology. Return plain prose, no headings."""


@dataclass
class GeneratedResponse:
    """A generated response ready for upload."""

    title: str
    format: str  # "pdf" | "notebook"
    pdf_bytes: bytes | None = None
    notebook_files: dict[str, bytes] | None = None
    question_count: int = 0
    action_count: int = 0


class ResponseGenerator:
    """Build response documents from synced Obsidian notes."""

    def __init__(
        self,
        vault: ObsidianVault,
        config: ResponseConfig,
        anthropic_client: anthropic.AsyncAnthropic | None = None,
        model: str = "claude-sonnet-4-20250514",
    ):
        self._vault = vault
        self._config = config
        self._client = anthropic_client
        self._model = model
        self._pdf_gen = ResponsePDFGenerator()
        self._notebook_writer = NotebookWriter()

    async def generate_from_note(self, note_path: Path) -> GeneratedResponse | None:
        """Read a vault note and build a response document.

        Returns None if the note doesn't exist or has no content worth responding to.
        """
        result = self._vault.read_note(note_path)
        if result is None:
            logger.warning("Note not found: %s", note_path)
            return None

        frontmatter, content = result
        title = frontmatter.get("title", note_path.stem)

        # Extract question-answer pairs if enabled
        qa_pairs = []
        if self._config.trigger_on_questions and self._client:
            qa_pairs = await self._answer_questions(content)

        # Build analysis if enabled
        analysis = ""
        if self._config.include_analysis and self._client:
            analysis = await self._build_analysis(content)

        # Pull action items from the existing structured content
        action_items = _extract_action_items(content)

        # Build summary from frontmatter
        summary = frontmatter.get("summary", "")
        key_points = []
        for line in content.split("\n"):
            stripped = line.strip()
            if stripped.startswith("> - "):
                key_points.append(stripped[4:])

        # Related notes from backlinks / wiki-links in the note
        related = []
        if self._config.include_related_notes:
            related = _extract_wiki_links(content)

        # Include Q&A in analysis section if any
        if qa_pairs:
            qa_text = "\n\n".join(
                f"Q: {qa['question']}\n→ {qa['answer']}" for qa in qa_pairs
            )
            analysis = (qa_text + "\n\n" + analysis).strip() if analysis else qa_text

        response_content = ResponseContent(
            note_title=title,
            summary=summary,
            key_points=key_points,
            action_items=action_items,
            analysis=analysis,
            related_notes=related,
            metadata={"source_path": str(note_path)},
        )

        response_title = f"Response — {title}"

        if self._config.format == "notebook":
            md_body = _format_as_markdown(response_content)
            files = self._notebook_writer.generate(response_title, md_body)
            return GeneratedResponse(
                title=response_title,
                format="notebook",
                notebook_files=files,
                question_count=len(qa_pairs),
                action_count=len(action_items),
            )

        # default: pdf
        pdf_bytes = self._pdf_gen.generate(response_content)
        return GeneratedResponse(
            title=response_title,
            format="pdf",
            pdf_bytes=pdf_bytes,
            question_count=len(qa_pairs),
            action_count=len(action_items),
        )

    async def _answer_questions(self, content: str) -> list[dict]:
        """Extract and answer questions from the note content."""
        questions = _extract_questions(content)
        if not questions:
            return []

        try:
            import json
            prompt = "Questions from the notes:\n\n" + "\n".join(
                f"- {q}" for q in questions
            )
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=2048,
                system=QA_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                lines = raw.split("\n")
                raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])
            answers = json.loads(raw)
            if isinstance(answers, list):
                return [qa for qa in answers if isinstance(qa, dict) and "question" in qa]
        except Exception as e:
            logger.warning("Q&A generation failed: %s", e)

        return []

    async def _build_analysis(self, content: str) -> str:
        """Generate a brief analysis of the note."""
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                system=ANALYSIS_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content[:4000]}],
            )
            return response.content[0].text.strip()
        except Exception as e:
            logger.warning("Analysis generation failed: %s", e)
            return ""


# -- Helpers --

_QUESTION_PATTERNS = [
    re.compile(r"^\s*Q\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Question\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE),
]


def _extract_questions(content: str) -> list[str]:
    """Pull question lines out of note content."""
    questions: list[str] = []
    seen: set[str] = set()

    for pattern in _QUESTION_PATTERNS:
        for match in pattern.finditer(content):
            q = match.group(1).strip()
            if q and q.lower() not in seen:
                questions.append(q)
                seen.add(q.lower())

    # Also catch stand-alone lines ending in "?"
    for line in content.split("\n"):
        stripped = line.strip().lstrip("- *#>").strip()

        # Skip lines already matched by Q:/Question: patterns
        if re.match(r"^(Q|Question)\s*:", stripped, re.IGNORECASE):
            continue

        if (
            stripped.endswith("?")
            and len(stripped) > 10
            and stripped.lower() not in seen
        ):
            questions.append(stripped)
            seen.add(stripped.lower())

    return questions


def _extract_action_items(content: str) -> list[dict]:
    """Pull action items from Markdown checkbox syntax."""
    actions = []
    for line in content.split("\n"):
        stripped = line.strip()
        m = re.match(r"-\s*\[\s*\]\s*(.+)", stripped)
        if m:
            task_line = m.group(1)
            priority = "medium"
            if "#priority-high" in task_line:
                priority = "high"
                task_line = task_line.replace("#priority-high", "").strip()
            elif "#priority-low" in task_line:
                priority = "low"
                task_line = task_line.replace("#priority-low", "").strip()

            assignee = None
            assignee_match = re.search(r"@(\w+)", task_line)
            if assignee_match:
                assignee = assignee_match.group(1)
                task_line = task_line.replace(f"@{assignee}", "").strip()

            deadline = None
            deadline_match = re.search(r"\(due:\s*([^)]+)\)|📅\s*([^\s]+)", task_line)
            if deadline_match:
                deadline = (deadline_match.group(1) or deadline_match.group(2)).strip()

            actions.append({
                "task": task_line,
                "priority": priority,
                "assignee": assignee,
                "deadline": deadline,
                "type": "task",
            })

        elif re.match(r"-\s*\[\?\]\s*(.+)", stripped):
            q = re.match(r"-\s*\[\?\]\s*(.+)", stripped).group(1)
            actions.append({
                "task": q,
                "priority": "medium",
                "type": "question",
            })

    return actions


def _extract_wiki_links(content: str) -> list[str]:
    """Extract [[wiki-link]] references from content."""
    links = re.findall(r"\[\[([^\]]+?)\]\]", content)
    seen: set[str] = set()
    ordered = []
    for link in links:
        key = link.strip().lower()
        if key and key not in seen:
            ordered.append(link.strip())
            seen.add(key)
    return ordered


def _format_as_markdown(content: ResponseContent) -> str:
    """Serialize ResponseContent to a Markdown body for notebook output."""
    parts = [f"# {content.note_title}", ""]

    if content.summary:
        parts.extend(["## Summary", content.summary, ""])

    if content.key_points:
        parts.append("## Key Points")
        parts.extend(f"- {p}" for p in content.key_points)
        parts.append("")

    if content.action_items:
        parts.append("## Action Items")
        for item in content.action_items:
            marker = "[ ]" if item.get("type", "task") == "task" else "[?]"
            line = f"- {marker} {item['task']}"
            if item.get("assignee"):
                line += f" @{item['assignee']}"
            if item.get("deadline"):
                line += f" (due: {item['deadline']})"
            parts.append(line)
        parts.append("")

    if content.analysis:
        parts.extend(["## Analysis", content.analysis, ""])

    if content.related_notes:
        parts.append("## Related Notes")
        parts.extend(f"- {n}" for n in content.related_notes)
        parts.append("")

    return "\n".join(parts)


def should_auto_trigger(
    config: ResponseConfig,
    content: str,
    has_color_questions: bool = False,
    action_count: int = 0,
) -> bool:
    """Decide whether a sync should trigger a response automatically."""
    if not config.auto_trigger:
        return False

    if config.trigger_on_questions and (has_color_questions or _extract_questions(content)):
        return True

    return config.trigger_on_actions and action_count > 0
