"""YAML frontmatter generation for Obsidian notes.

All vault notes must include frontmatter — this module ensures
consistent metadata across all synced notes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.ocr.pipeline import PageText
from src.processing.actions import ActionItem
from src.remarkable.formats import Notebook


def generate_frontmatter(
    notebook: Notebook,
    ocr_results: list[PageText],
    actions: list[ActionItem],
    tags: list[str],
    summary_one_line: str = "",
) -> dict:
    """Generate frontmatter dict for a synced note.

    This is the canonical format for all reMarkable-sourced notes.
    """
    now = datetime.now(UTC).isoformat()

    # Determine which OCR engines were used
    engines = list({r.engine_used for r in ocr_results if r.engine_used != "none"})

    # Average OCR confidence
    confidences = [r.confidence for r in ocr_results if r.confidence > 0]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    # Detect device type from notebook metadata (heuristic)
    device = _detect_device(notebook)

    fm: dict = {
        "title": notebook.name,
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "last_synced": now,
        "source": "remarkable",
        "device": device,
        "remarkable_id": notebook.id,
        "remarkable_folder": notebook.folder,
        "pages": notebook.page_count,
        "ocr_engine": engines[0] if len(engines) == 1 else engines,
        "ocr_confidence": round(avg_confidence, 2),
        "action_items": len(actions),
        "tags": tags,
        "status": "transcribed",
    }

    if summary_one_line:
        fm["summary"] = summary_one_line

    return fm


def update_frontmatter(existing: dict, updates: dict) -> dict:
    """Merge updates into existing frontmatter, preserving manual additions.

    Fields from updates overwrite existing values. Fields only in existing
    are preserved (they may have been added manually by the user).
    """
    merged = dict(existing)
    merged.update(updates)
    merged["last_synced"] = datetime.now(UTC).isoformat()
    return merged


def _detect_device(notebook: Notebook) -> str:
    """Heuristic device detection based on notebook properties.

    Paper Pro supports color and has higher resolution.
    This is a best-effort guess — the Cloud API doesn't expose device info directly.
    """
    # If any page has color strokes, it's likely a Paper Pro
    for page in notebook.pages:
        for line in page.lines:
            if hasattr(line, "color"):
                from rmscene.scene_items import PenColor

                if line.color not in (PenColor.BLACK, PenColor.GRAY, PenColor.WHITE):
                    return "paper-pro"

    return "remarkable"
