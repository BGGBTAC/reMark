"""Page rendering for image-based OCR.

Renders .rm stroke data to PNG images suitable for OCR engines.
Delegates the actual SVG generation to formats.py and adds
OCR-specific optimizations (contrast, resolution, preprocessing).
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.remarkable.formats import parse_rm_file

logger = logging.getLogger(__name__)

# reMarkable canvas dimensions at native DPI (226)
RM_WIDTH = 1404
RM_HEIGHT = 1872
RM_NATIVE_DPI = 226


def render_page(
    doc_dir: Path,
    doc_id: str,
    page_id: str,
    dpi: int = 300,
    high_contrast: bool = True,
) -> bytes | None:
    """Render a .rm page to PNG, optimized for OCR.

    Args:
        doc_dir: Document directory containing .rm files.
        doc_id: Document UUID.
        page_id: Page UUID.
        dpi: Output resolution. 300 DPI is good for most OCR engines.
        high_contrast: If True, render all strokes in black for better OCR.

    Returns PNG bytes, or None if the page has no renderable content.
    """
    from src.remarkable.formats import _find_rm_file, _svg_to_png

    rm_path = _find_rm_file(doc_dir, doc_id, page_id)
    if rm_path is None:
        return None

    try:
        page = parse_rm_file(rm_path)
    except Exception as e:
        logger.warning("Failed to parse %s for rendering: %s", page_id[:8], e)
        return None

    if not page.lines:
        return None

    svg = _render_ocr_svg(page.lines, dpi, high_contrast)
    return _svg_to_png(svg, dpi)


def render_page_region(
    doc_dir: Path,
    doc_id: str,
    page_id: str,
    bbox: tuple[float, float, float, float],
    dpi: int = 300,
    padding: float = 50,
) -> bytes | None:
    """Render a cropped region of a page (e.g. around colored strokes).

    Args:
        bbox: (min_x, min_y, max_x, max_y) in reMarkable coordinates.
        padding: Extra pixels around the bbox.
    """
    from src.remarkable.formats import _find_rm_file, _svg_to_png

    rm_path = _find_rm_file(doc_dir, doc_id, page_id)
    if rm_path is None:
        return None

    try:
        page = parse_rm_file(rm_path)
    except Exception:
        return None

    if not page.lines:
        return None

    min_x, min_y, max_x, max_y = bbox
    min_x = max(0, min_x - padding)
    min_y = max(0, min_y - padding)
    max_x = min(RM_WIDTH, max_x + padding)
    max_y = min(RM_HEIGHT, max_y + padding)

    region_w = max_x - min_x
    region_h = max_y - min_y
    if region_w <= 0 or region_h <= 0:
        return None

    scale = dpi / RM_NATIVE_DPI
    svg_w = region_w * scale
    svg_h = region_h * scale

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{svg_w:.0f}" height="{svg_h:.0f}" '
        f'viewBox="{min_x} {min_y} {region_w} {region_h}">'
        f'<rect x="{min_x}" y="{min_y}" width="{region_w}" height="{region_h}" fill="white"/>'
    ]

    for line in page.lines:
        if len(line.points) < 2:
            continue

        # Check if any points are in the region
        in_region = any(
            min_x <= p.x <= max_x and min_y <= p.y <= max_y
            for p in line.points
        )
        if not in_region:
            continue

        path_d = []
        for i, point in enumerate(line.points):
            cmd = "M" if i == 0 else "L"
            path_d.append(f"{cmd}{point.x:.1f},{point.y:.1f}")

        width = line.thickness_scale * 2.0
        svg_parts.append(
            f'<path d="{" ".join(path_d)}" '
            f'stroke="#000000" stroke-width="{width:.1f}" '
            f'fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        )

    svg_parts.append("</svg>")
    svg = "\n".join(svg_parts)

    return _svg_to_png(svg, dpi)


def _render_ocr_svg(lines: list, dpi: int, high_contrast: bool) -> str:
    """Render strokes to SVG optimized for OCR (high contrast, clean lines)."""
    from rmscene.scene_items import PenColor

    scale = dpi / RM_NATIVE_DPI
    width = RM_WIDTH * scale
    height = RM_HEIGHT * scale

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {RM_WIDTH} {RM_HEIGHT}">'
        f'<rect width="{RM_WIDTH}" height="{RM_HEIGHT}" fill="white"/>'
    ]

    # For OCR, we want maximum contrast — everything in black
    # unless high_contrast is off (for visual rendering)
    color_map = {
        PenColor.BLACK: "#000000",
        PenColor.GRAY: "#000000" if high_contrast else "#808080",
        PenColor.WHITE: "#ffffff",
        PenColor.YELLOW: "#000000" if high_contrast else "#cccc00",
        PenColor.GREEN: "#000000" if high_contrast else "#00aa00",
        PenColor.BLUE: "#000000" if high_contrast else "#0000ff",
        PenColor.RED: "#000000" if high_contrast else "#ff0000",
        PenColor.PINK: "#000000" if high_contrast else "#ff69b4",
    }

    for line in lines:
        if len(line.points) < 2:
            continue

        # Skip eraser strokes
        from rmscene.scene_items import Pen
        if line.tool in (Pen.ERASER, Pen.ERASER_AREA):
            continue

        color = color_map.get(line.color, "#000000")
        base_width = max(line.thickness_scale * 2.0, 1.0)

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
