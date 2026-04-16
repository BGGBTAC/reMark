"""Generate response PDFs to push back to reMarkable.

Creates styled, e-ink-optimized PDFs with note summaries,
action items, and analysis results using ReportLab.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO

from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
)

logger = logging.getLogger(__name__)

# e-ink optimized: high contrast, generous spacing, large fonts
EINK_STYLES = {
    "title": ParagraphStyle(
        "EinkTitle",
        fontSize=22,
        leading=28,
        alignment=TA_CENTER,
        spaceAfter=12 * mm,
        fontName="Helvetica-Bold",
    ),
    "subtitle": ParagraphStyle(
        "EinkSubtitle",
        fontSize=12,
        leading=16,
        alignment=TA_CENTER,
        spaceAfter=8 * mm,
        textColor="#555555",
    ),
    "heading": ParagraphStyle(
        "EinkHeading",
        fontSize=16,
        leading=22,
        spaceBefore=8 * mm,
        spaceAfter=4 * mm,
        fontName="Helvetica-Bold",
    ),
    "body": ParagraphStyle(
        "EinkBody",
        fontSize=11,
        leading=16,
        spaceAfter=3 * mm,
    ),
    "bullet": ParagraphStyle(
        "EinkBullet",
        fontSize=11,
        leading=16,
        leftIndent=8 * mm,
        spaceAfter=2 * mm,
    ),
    "checkbox": ParagraphStyle(
        "EinkCheckbox",
        fontSize=11,
        leading=16,
        leftIndent=8 * mm,
        spaceAfter=2 * mm,
        fontName="Courier",
    ),
    "context": ParagraphStyle(
        "EinkContext",
        fontSize=9,
        leading=13,
        leftIndent=12 * mm,
        textColor="#666666",
        spaceAfter=3 * mm,
    ),
    "footer": ParagraphStyle(
        "EinkFooter",
        fontSize=8,
        leading=11,
        alignment=TA_CENTER,
        textColor="#999999",
    ),
}


@dataclass
class ResponseContent:
    """Data for generating a response PDF."""

    note_title: str
    summary: str = ""
    key_points: list[str] = field(default_factory=list)
    action_items: list[dict] = field(default_factory=list)
    analysis: str = ""
    related_notes: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class ResponsePDFGenerator:
    """Generate styled PDFs optimized for reMarkable e-ink display."""

    def generate(self, content: ResponseContent) -> bytes:
        """Generate a response PDF.

        Returns PDF bytes ready for upload to reMarkable.
        """
        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=2 * cm,
            rightMargin=2 * cm,
            topMargin=2.5 * cm,
            bottomMargin=2 * cm,
        )

        story = self._build_story(content)
        doc.build(story)

        pdf_bytes = buffer.getvalue()
        logger.info(
            "Generated response PDF for '%s' (%d bytes)",
            content.note_title,
            len(pdf_bytes),
        )
        return pdf_bytes

    def _build_story(self, content: ResponseContent) -> list:
        """Build the PDF content as a list of flowable elements."""
        story = []

        # Title
        story.append(
            Paragraph(
                _escape(content.note_title),
                EINK_STYLES["title"],
            )
        )

        # Date subtitle
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        story.append(
            Paragraph(
                f"Generated {now}",
                EINK_STYLES["subtitle"],
            )
        )

        # Summary section
        if content.summary:
            story.append(Paragraph("Summary", EINK_STYLES["heading"]))
            story.append(Paragraph(_escape(content.summary), EINK_STYLES["body"]))

        # Key points
        if content.key_points:
            story.append(Paragraph("Key Points", EINK_STYLES["heading"]))
            for point in content.key_points:
                story.append(
                    Paragraph(
                        f"• {_escape(point)}",
                        EINK_STYLES["bullet"],
                    )
                )

        # Action items
        if content.action_items:
            story.append(Paragraph("Action Items", EINK_STYLES["heading"]))
            for item in content.action_items:
                task = item.get("task", "")
                priority = item.get("priority", "medium")
                assignee = item.get("assignee", "")
                deadline = item.get("deadline", "")
                item_type = item.get("type", "task")

                # Checkbox style
                marker = "[ ]" if item_type == "task" else "[?]"
                priority_mark = " (!)" if priority == "high" else ""

                line = f"{marker} {_escape(task)}{priority_mark}"
                story.append(Paragraph(line, EINK_STYLES["checkbox"]))

                # Metadata line
                meta_parts = []
                if assignee:
                    meta_parts.append(f"@{assignee}")
                if deadline:
                    meta_parts.append(f"Due: {deadline}")
                if meta_parts:
                    story.append(
                        Paragraph(
                            " · ".join(meta_parts),
                            EINK_STYLES["context"],
                        )
                    )

        # Analysis
        if content.analysis:
            story.append(Paragraph("Analysis", EINK_STYLES["heading"]))
            for paragraph in content.analysis.split("\n\n"):
                if paragraph.strip():
                    story.append(
                        Paragraph(
                            _escape(paragraph.strip()),
                            EINK_STYLES["body"],
                        )
                    )

        # Related notes
        if content.related_notes:
            story.append(Paragraph("Related Notes", EINK_STYLES["heading"]))
            for note in content.related_notes:
                story.append(
                    Paragraph(
                        f"→ {_escape(note)}",
                        EINK_STYLES["bullet"],
                    )
                )

        # Footer
        story.append(Spacer(1, 15 * mm))
        story.append(
            Paragraph(
                "reMark — synced from reMarkable",
                EINK_STYLES["footer"],
            )
        )

        return story


def _escape(text: str) -> str:
    """Escape special XML/HTML chars for ReportLab paragraphs."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
