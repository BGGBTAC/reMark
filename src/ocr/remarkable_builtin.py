"""OCR engine using reMarkable's built-in MyScript text conversion.

This is the cheapest and often highest-quality option — zero API cost,
because the conversion happens on-device when the user taps "Convert to text".
The results sync to Cloud in {doc_id}.textconversion/{page_id}.json.

Limitation: only works if the user has manually triggered conversion on the tablet.
For fully automatic processing, configure a fallback engine.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from src.ocr.pipeline import BoundingBox, OCREngine, OCRResult

logger = logging.getLogger(__name__)


class RemarkableBuiltinOCR(OCREngine):
    """Extract text from reMarkable's on-device MyScript conversion results."""

    @property
    def name(self) -> str:
        return "remarkable_builtin"

    async def recognize_page(self, page_image: bytes) -> OCRResult:
        """Not used — this engine reads from files, not images.

        The pipeline calls get_builtin_text_conversion() directly.
        This method exists to satisfy the OCREngine interface.
        """
        return OCRResult(text="", confidence=0.0, engine=self.name)

    def recognize_from_file(self, conversion_path: Path) -> OCRResult | None:
        """Read MyScript conversion result from a .textconversion JSON file.

        Returns None if the file doesn't exist or contains no text.
        """
        if not conversion_path.exists():
            return None

        try:
            data = json.loads(conversion_path.read_text())
        except json.JSONDecodeError as e:
            logger.warning("Malformed conversion file %s: %s", conversion_path.name, e)
            return None

        text = data.get("text", "")

        # Alternative format: array of paragraphs
        if not text and "paragraphs" in data:
            paragraphs = data["paragraphs"]
            text = "\n".join(p.get("text", "") for p in paragraphs if isinstance(p, dict))

        if not text.strip():
            return None

        # Extract word-level bounding boxes if available
        boxes = _parse_word_boxes(data)

        return OCRResult(
            text=text.strip(),
            confidence=1.0,  # user-verified conversion
            engine=self.name,
            word_boxes=boxes,
        )


def _parse_word_boxes(data: dict) -> list[BoundingBox] | None:
    """Extract word-level bounding boxes from MyScript conversion data."""
    words = data.get("words", [])
    if not words:
        return None

    boxes = []
    for word in words:
        if not isinstance(word, dict):
            continue
        bbox = word.get("boundingBox", {})
        if bbox:
            boxes.append(
                BoundingBox(
                    x=bbox.get("x", 0),
                    y=bbox.get("y", 0),
                    width=bbox.get("width", 0),
                    height=bbox.get("height", 0),
                    text=word.get("label", ""),
                    confidence=word.get("confidence", 1.0),
                )
            )

    return boxes if boxes else None
