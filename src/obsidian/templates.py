"""Note templates for the Obsidian vault.

Provides formatting functions that turn processed data into
the final Markdown output written to the vault.
"""

from __future__ import annotations

from src.ocr.pipeline import PageText
from src.processing.actions import ActionItem
from src.processing.summarizer import NoteSummary


def format_note_content(
    structured_md: str,
    summary: NoteSummary | None = None,
    actions: list[ActionItem] | None = None,
    page_texts: list[PageText] | None = None,
) -> str:
    """Build the full Markdown content for a vault note.

    Combines the structured content with optional summary and action sections.
    """
    parts: list[str] = []

    # Summary block at the top
    if summary and summary.key_points:
        parts.append("> **Summary:** " + summary.one_line)
        parts.append(">")
        for point in summary.key_points:
            parts.append(f"> - {point}")
        parts.append("")

    # Main structured content
    if structured_md.strip():
        parts.append(structured_md.strip())
        parts.append("")

    # Inline action items section
    if actions:
        parts.append("---")
        parts.append("")
        parts.append("## Action Items")
        parts.append("")
        for action in actions:
            _type = action.type
            prefix = "- [ ]"
            if _type == "question":
                prefix = "- [?]"
            elif _type == "decision":
                prefix = "- [!]"

            line = f"{prefix} {action.task}"
            if action.assignee:
                line += f" @{action.assignee}"
            if action.deadline:
                line += f" (due: {action.deadline})"
            parts.append(line)
        parts.append("")

    # OCR metadata footer (collapsed)
    if page_texts:
        engines = list({pt.engine_used for pt in page_texts if pt.engine_used != "none"})
        avg_conf = sum(pt.confidence for pt in page_texts) / len(page_texts) if page_texts else 0
        if engines:
            parts.append("---")
            parts.append(f"*OCR: {', '.join(engines)} (confidence: {avg_conf:.0%})*")
            parts.append("")

    return "\n".join(parts)


def format_action_index(
    actions_by_note: dict[str, list[ActionItem]],
) -> str:
    """Generate a master action index across all notes.

    Written to Actions/index.md for a global view of all tasks.
    """
    parts = ["# Action Items\n"]

    open_count = 0
    for note_name, actions in sorted(actions_by_note.items()):
        if not actions:
            continue

        parts.append(f"## [[{note_name}]]")
        parts.append("")

        for action in actions:
            prefix = "- [ ]"
            if action.type == "question":
                prefix = "- [?]"

            line = f"{prefix} {action.task}"
            if action.priority == "high":
                line += " #priority-high"
            parts.append(line)
            open_count += 1

        parts.append("")

    parts.insert(1, f"*{open_count} open items across {len(actions_by_note)} notes*\n")

    return "\n".join(parts)


def format_daily_digest(
    date: str,
    notes_processed: list[tuple[str, NoteSummary]],
    total_actions: int,
) -> str:
    """Generate a daily digest note summarizing sync activity.

    Written to Inbox/digest-{date}.md.
    """
    parts = [
        f"# Sync Digest — {date}\n",
        f"Processed **{len(notes_processed)}** notes, found **{total_actions}** action items.\n",
    ]

    for note_name, summary in notes_processed:
        parts.append(f"### [[{note_name}]]")
        parts.append(summary.one_line)
        if summary.key_points:
            for point in summary.key_points:
                parts.append(f"- {point}")
        parts.append("")

    return "\n".join(parts)
