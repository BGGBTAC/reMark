"""reMarkable file format parsing.

Wraps the rmscene library to provide higher-level access to
.rm v6 files, extracting text, strokes, and rendering pages.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from rmscene import RootTextBlock, read_blocks, read_tree
from rmscene.scene_items import (
    GlyphRange,
    Group,
    Line,
    ParagraphStyle,
    PenColor,
    Text,
)

logger = logging.getLogger(__name__)

# Map rmscene PenColor enum to the integer indices used in config
COLOR_INDEX_MAP: dict[PenColor, int] = {
    PenColor.BLACK: 0,
    PenColor.GRAY: 1,
    PenColor.WHITE: 2,
    PenColor.YELLOW: 3,
    PenColor.GREEN: 4,
    PenColor.BLUE: 5,
    PenColor.RED: 6,
    PenColor.PINK: 7,
}

# Reverse map for lookup by index
INDEX_COLOR_MAP: dict[int, PenColor] = {v: k for k, v in COLOR_INDEX_MAP.items()}

# Map ParagraphStyle to Markdown
STYLE_MAP: dict[ParagraphStyle, str] = {
    ParagraphStyle.HEADING: "# ",
    ParagraphStyle.BOLD: "**",
    ParagraphStyle.BULLET: "- ",
    ParagraphStyle.BULLET2: "  - ",
    ParagraphStyle.CHECKBOX: "- [ ] ",
    ParagraphStyle.CHECKBOX_CHECKED: "- [x] ",
}


@dataclass
class StrokeGroup:
    """A group of strokes with the same color in a spatial region."""

    color: PenColor
    color_index: int
    lines: list[Line]
    bbox: tuple[float, float, float, float]  # min_x, min_y, max_x, max_y
    page_id: str = ""

    @property
    def color_name(self) -> str:
        return self.color.name.lower()


@dataclass
class PageContent:
    """Parsed content from a single .rm page."""

    page_id: str
    text_blocks: list[TextBlock] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    glyph_ranges: list[GlyphRange] = field(default_factory=list)

    @property
    def has_text(self) -> bool:
        return bool(self.text_blocks) or bool(self.glyph_ranges)

    @property
    def has_strokes(self) -> bool:
        return bool(self.lines)

    @property
    def plain_text(self) -> str:
        """All text content concatenated."""
        parts = []
        for block in self.text_blocks:
            parts.append(block.to_markdown())
        for glyph in self.glyph_ranges:
            parts.append(glyph.text)
        return "\n".join(parts)


@dataclass
class TextBlock:
    """A block of text extracted from a .rm file's CRDT data."""

    text: str
    style: ParagraphStyle | None = None
    pos_x: float = 0
    pos_y: float = 0
    width: float = 0

    def to_markdown(self) -> str:
        if self.style and self.style in STYLE_MAP:
            prefix = STYLE_MAP[self.style]
            if self.style == ParagraphStyle.BOLD:
                return f"**{self.text}**"
            return f"{prefix}{self.text}"
        return self.text


@dataclass
class Notebook:
    """A fully parsed reMarkable notebook."""

    id: str
    name: str
    folder: str
    modified: str
    pages: list[PageContent]
    file_type: str = "notebook"  # "notebook" | "pdf" | "epub"

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def all_text(self) -> str:
        """Concatenated text from all pages."""
        return "\n\n".join(
            page.plain_text for page in self.pages if page.plain_text.strip()
        )


def parse_rm_file(rm_path: Path) -> PageContent:
    """Parse a single .rm v6 file into structured content.

    Text content lives in RootTextBlock (CRDT), not in the scene tree.
    Strokes (Lines) and GlyphRanges come from the scene tree via walk().
    We need both read_blocks() and read_tree() for full extraction.
    """
    page_id = rm_path.stem

    text_blocks: list[TextBlock] = []
    lines: list[Line] = []
    glyph_ranges: list[GlyphRange] = []

    # Pass 1: read blocks for RootTextBlock (CRDT text)
    with open(rm_path, "rb") as f:
        for block in read_blocks(f):
            if isinstance(block, RootTextBlock) and isinstance(block.value, Text):
                tb = _extract_text_block(block.value)
                if tb and tb.text.strip():
                    text_blocks.append(tb)

    # Pass 2: read scene tree for Lines and GlyphRanges (strokes, handwriting)
    with open(rm_path, "rb") as f:
        tree = read_tree(f)

    for item in tree.walk():
        if isinstance(item, Line):
            lines.append(item)
        elif isinstance(item, GlyphRange):
            glyph_ranges.append(item)

    logger.debug(
        "Parsed %s: %d text blocks, %d lines, %d glyphs",
        rm_path.name, len(text_blocks), len(lines), len(glyph_ranges),
    )

    return PageContent(
        page_id=page_id,
        text_blocks=text_blocks,
        lines=lines,
        glyph_ranges=glyph_ranges,
    )


def parse_notebook(doc_dir: Path, doc_id: str, page_ids: list[str]) -> list[PageContent]:
    """Parse all pages of a notebook.

    Args:
        doc_dir: Directory containing the downloaded document files.
        doc_id: The document UUID.
        page_ids: Ordered list of page UUIDs.

    Returns:
        List of PageContent, one per page.
    """
    pages = []

    for page_id in page_ids:
        # .rm files can be at different paths depending on download format
        rm_path = _find_rm_file(doc_dir, doc_id, page_id)

        if rm_path is None:
            logger.debug("No .rm file for page %s, skipping", page_id[:8])
            pages.append(PageContent(page_id=page_id))
            continue

        try:
            page = parse_rm_file(rm_path)
            pages.append(page)
        except Exception as e:
            logger.warning("Failed to parse page %s: %s", page_id[:8], e)
            pages.append(PageContent(page_id=page_id))

    return pages


def extract_typed_text(doc_dir: Path, doc_id: str, page_ids: list[str]) -> dict[str, str]:
    """Extract CRDT text from all pages (no OCR needed).

    Returns {page_id: text_content} for pages that have typed text.
    """
    result: dict[str, str] = {}

    for page_id in page_ids:
        rm_path = _find_rm_file(doc_dir, doc_id, page_id)
        if rm_path is None:
            continue

        try:
            page = parse_rm_file(rm_path)
            text = page.plain_text.strip()
            if text:
                result[page_id] = text
        except Exception as e:
            logger.debug("No text from page %s: %s", page_id[:8], e)

    return result


def extract_strokes_by_color(
    doc_dir: Path, doc_id: str, page_ids: list[str], colors: list[int]
) -> dict[str, list[StrokeGroup]]:
    """Extract strokes filtered by color index.

    Args:
        colors: List of color indices (0=black, 1=grey, ..., 6=red, 7=pink).

    Returns {page_id: [StrokeGroup]} for pages with matching strokes.
    """
    target_colors = {INDEX_COLOR_MAP[c] for c in colors if c in INDEX_COLOR_MAP}

    if not target_colors:
        return {}

    result: dict[str, list[StrokeGroup]] = {}

    for page_id in page_ids:
        rm_path = _find_rm_file(doc_dir, doc_id, page_id)
        if rm_path is None:
            continue

        try:
            page = parse_rm_file(rm_path)
        except Exception:
            continue

        groups = _group_strokes_by_color(page.lines, target_colors, page_id)
        if groups:
            result[page_id] = groups

    return result


def get_builtin_text_conversion(doc_dir: Path, doc_id: str) -> dict[str, str] | None:
    """Check for reMarkable's MyScript "Convert to text" results.

    When a user triggers conversion on the tablet, results are stored
    in {doc_id}.textconversion/{page_id}.json.

    Returns {page_id: converted_text} or None if no conversion exists.
    """
    conv_dir = doc_dir / f"{doc_id}.textconversion"

    if not conv_dir.exists():
        # Also try without doc_id prefix
        for d in doc_dir.glob("*.textconversion"):
            conv_dir = d
            break

    if not conv_dir.exists():
        return None

    result: dict[str, str] = {}

    for json_file in sorted(conv_dir.glob("*.json")):
        try:
            data = json.loads(json_file.read_text())
            text = data.get("text", "")
            if not text and "paragraphs" in data:
                # Alternative format: array of paragraph objects
                paragraphs = data["paragraphs"]
                text = "\n".join(p.get("text", "") for p in paragraphs if isinstance(p, dict))
            if text.strip():
                result[json_file.stem] = text.strip()
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to parse text conversion %s: %s", json_file.name, e)

    return result if result else None


def render_page_to_png(
    doc_dir: Path, doc_id: str, page_id: str, dpi: int = 300
) -> bytes | None:
    """Render a .rm page to PNG for image-based OCR.

    Uses rmscene to get stroke data, renders to SVG, converts to PNG via cairosvg.
    """
    rm_path = _find_rm_file(doc_dir, doc_id, page_id)
    if rm_path is None:
        return None

    try:
        page = parse_rm_file(rm_path)
    except Exception as e:
        logger.warning("Failed to parse %s for rendering: %s", page_id[:8], e)
        return None

    if not page.lines:
        logger.debug("No strokes to render for page %s", page_id[:8])
        return None

    svg = _render_strokes_to_svg(page.lines, dpi)
    return _svg_to_png(svg, dpi)


# -- Internal helpers --


def _extract_text_block(text_item: Text) -> TextBlock | None:
    """Extract a TextBlock from an rmscene Text item.

    CrdtSequence.values() yields the actual string/int values.
    Strings are text content, ints represent special chars (0 = newline).
    """
    chars: list[str] = []
    for value in text_item.items.values():
        if isinstance(value, str):
            chars.append(value)
        elif isinstance(value, int):
            if value == 0:
                chars.append("\n")

    text = "".join(chars).strip()
    if not text:
        return None

    # Get paragraph style from the first style entry
    style = None
    if text_item.styles:
        first_style = next(iter(text_item.styles.values()), None)
        if first_style is not None:
            style = first_style.value

    return TextBlock(
        text=text,
        style=style,
        pos_x=text_item.pos_x,
        pos_y=text_item.pos_y,
        width=text_item.width,
    )


def _group_strokes_by_color(
    lines: list[Line], target_colors: set[PenColor], page_id: str
) -> list[StrokeGroup]:
    """Group strokes by color, only keeping those in target_colors."""
    groups_by_color: dict[PenColor, list[Line]] = {}

    for line in lines:
        if line.color in target_colors:
            groups_by_color.setdefault(line.color, []).append(line)

    result = []
    for color, color_lines in groups_by_color.items():
        bbox = _compute_bbox(color_lines)
        result.append(StrokeGroup(
            color=color,
            color_index=COLOR_INDEX_MAP.get(color, -1),
            lines=color_lines,
            bbox=bbox,
            page_id=page_id,
        ))

    return result


def _compute_bbox(lines: list[Line]) -> tuple[float, float, float, float]:
    """Compute the bounding box for a list of lines."""
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")

    for line in lines:
        for point in line.points:
            min_x = min(min_x, point.x)
            min_y = min(min_y, point.y)
            max_x = max(max_x, point.x)
            max_y = max(max_y, point.y)

    if min_x == float("inf"):
        return (0, 0, 0, 0)

    return (min_x, min_y, max_x, max_y)


def _find_rm_file(doc_dir: Path, doc_id: str, page_id: str) -> Path | None:
    """Locate the .rm file for a given page, trying common path patterns."""
    candidates = [
        doc_dir / f"{doc_id}" / f"{page_id}.rm",
        doc_dir / f"{page_id}.rm",
        doc_dir / doc_id / f"{page_id}.rm",
    ]

    for path in candidates:
        if path.exists():
            return path

    # Fallback: search recursively
    for rm_file in doc_dir.rglob(f"{page_id}.rm"):
        return rm_file

    return None


def _render_strokes_to_svg(lines: list[Line], dpi: int = 300) -> str:
    """Render stroke data to an SVG string.

    reMarkable canvas is 1404 x 1872 units at 226 DPI.
    """
    rm_width = 1404
    rm_height = 1872
    scale = dpi / 226.0

    width = rm_width * scale
    height = rm_height * scale

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {rm_width} {rm_height}">'
    ]

    # White background
    svg_parts.append(f'<rect width="{rm_width}" height="{rm_height}" fill="white"/>')

    color_map = {
        PenColor.BLACK: "#000000",
        PenColor.GRAY: "#808080",
        PenColor.WHITE: "#ffffff",
        PenColor.YELLOW: "#ffff00",
        PenColor.GREEN: "#00cc00",
        PenColor.BLUE: "#0000ff",
        PenColor.RED: "#ff0000",
        PenColor.PINK: "#ff69b4",
    }

    for line in lines:
        if len(line.points) < 2:
            continue

        color = color_map.get(line.color, "#000000")
        base_width = line.thickness_scale * 2.0

        path_d = []
        for i, point in enumerate(line.points):
            cmd = "M" if i == 0 else "L"
            path_d.append(f"{cmd}{point.x:.1f},{point.y:.1f}")

        svg_parts.append(
            f'<path d="{" ".join(path_d)}" '
            f'stroke="{color}" stroke-width="{base_width:.1f}" '
            f'fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        )

    svg_parts.append("</svg>")
    return "\n".join(svg_parts)


def _svg_to_png(svg: str, dpi: int = 300) -> bytes:
    """Convert SVG string to PNG bytes using cairosvg."""
    import cairosvg

    return cairosvg.svg2png(bytestring=svg.encode("utf-8"), dpi=dpi)
