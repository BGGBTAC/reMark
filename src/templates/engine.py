"""On-device template engine.

Templates describe PDFs to push to the tablet and how to parse the filled
version back into structured data. A template definition has:

  - name, description
  - fields: list of {name, heading, type (text|list|date), required}
  - pdf: either a builtin name or a YAML definition with form sections

The engine provides three operations:
  - list_templates() → all discoverable templates
  - render_pdf(template, values) → PDF bytes to push to tablet
  - extract_fields(template, markdown) → {field_name: value}
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

import yaml
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm, mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

logger = logging.getLogger(__name__)


@dataclass
class TemplateField:
    """A single field in a template."""
    name: str
    heading: str
    type: str = "text"       # "text" | "list" | "date" | "checklist"
    required: bool = False
    hint: str = ""


@dataclass
class Template:
    """A template definition."""
    name: str
    description: str
    fields: list[TemplateField] = field(default_factory=list)
    title_prefix: str = ""   # prepended to PDF title when pushed


class TemplateEngine:
    """Load, render, and parse on-device templates."""

    def __init__(self, user_templates_dir: str | Path):
        self._user_dir = Path(user_templates_dir).expanduser()
        self._templates: dict[str, Template] = {}
        self._load_all()

    def _load_all(self) -> None:
        """Load builtin templates and any user-defined ones."""
        # Builtin — ships with the package
        builtin_dir = Path(__file__).parent / "builtin"
        self._load_dir(builtin_dir)

        # User overrides / additions
        if self._user_dir.exists():
            self._load_dir(self._user_dir)

    def _load_dir(self, directory: Path) -> None:
        for yaml_file in directory.glob("*.yaml"):
            try:
                data = yaml.safe_load(yaml_file.read_text())
                template = _parse_template(data)
                self._templates[template.name] = template
            except Exception as e:
                logger.warning("Failed to load template %s: %s", yaml_file.name, e)

    def list_templates(self) -> list[Template]:
        return list(self._templates.values())

    def get(self, name: str) -> Template | None:
        return self._templates.get(name)

    def render_pdf(self, template_name: str, extra_values: dict | None = None) -> bytes:
        """Render a template as an empty fillable PDF for the tablet.

        extra_values can pre-populate specific fields (e.g. the date).
        """
        template = self._templates.get(template_name)
        if template is None:
            raise ValueError(f"Template '{template_name}' not found")

        values = extra_values or {}
        return _render_template_pdf(template, values)

    def extract_fields(
        self,
        template_name: str,
        markdown: str,
    ) -> dict[str, Any]:
        """Parse a filled template's Markdown content into structured fields."""
        template = self._templates.get(template_name)
        if template is None:
            raise ValueError(f"Template '{template_name}' not found")

        return _extract_fields(template, markdown)

    def detect_template(self, frontmatter: dict, content: str) -> Template | None:
        """Try to identify which template a note corresponds to.

        Primary signal: frontmatter.template == <name>.
        Fallback: heading pattern match.
        """
        explicit = frontmatter.get("template")
        if isinstance(explicit, str) and explicit in self._templates:
            return self._templates[explicit]

        # Heading-based fallback: first heading mentions a known template
        first_heading = _first_heading(content)
        if first_heading:
            lower = first_heading.lower()
            for template in self._templates.values():
                if template.name.lower() in lower:
                    return template

        return None


# -- PDF rendering --

_TEMPLATE_STYLES = {
    "title": ParagraphStyle(
        "TemplateTitle", fontSize=20, leading=26, spaceAfter=10 * mm,
        fontName="Helvetica-Bold",
    ),
    "heading": ParagraphStyle(
        "TemplateHeading", fontSize=14, leading=18,
        spaceBefore=6 * mm, spaceAfter=2 * mm,
        fontName="Helvetica-Bold",
    ),
    "hint": ParagraphStyle(
        "TemplateHint", fontSize=9, leading=12, textColor="#888888",
        spaceAfter=2 * mm,
    ),
    "line": ParagraphStyle(
        "TemplateLine", fontSize=11, leading=22,
        spaceAfter=4 * mm,
    ),
    "prefilled": ParagraphStyle(
        "TemplatePrefilled", fontSize=11, leading=16, spaceAfter=4 * mm,
        textColor="#333333",
    ),
}


def _render_template_pdf(template: Template, values: dict) -> bytes:
    """Render a template to PDF bytes, with writing space for each field."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
    )

    story: list = [
        Paragraph(template.title_prefix or template.name.title(),
                  _TEMPLATE_STYLES["title"]),
    ]

    if template.description:
        story.append(Paragraph(template.description, _TEMPLATE_STYLES["hint"]))
        story.append(Spacer(1, 4 * mm))

    for field_def in template.fields:
        story.append(Paragraph(field_def.heading, _TEMPLATE_STYLES["heading"]))

        prefilled = values.get(field_def.name)
        if prefilled:
            story.append(Paragraph(str(prefilled), _TEMPLATE_STYLES["prefilled"]))
        elif field_def.hint:
            story.append(Paragraph(field_def.hint, _TEMPLATE_STYLES["hint"]))

        # Allocate writing space depending on field type
        if field_def.type in ("list", "checklist"):
            for _ in range(5):
                story.append(Paragraph("• ___________________________",
                                       _TEMPLATE_STYLES["line"]))
        elif field_def.type == "date":
            story.append(Paragraph("____-____-____", _TEMPLATE_STYLES["line"]))
        else:
            for _ in range(3):
                story.append(Paragraph("_" * 60, _TEMPLATE_STYLES["line"]))

    doc.build(story)
    return buffer.getvalue()


# -- Field extraction --

_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)$")


def _extract_fields(template: Template, markdown: str) -> dict[str, Any]:
    """Split markdown by headings that match the template fields."""
    result: dict[str, Any] = {}
    lines = markdown.split("\n")

    # Build heading → field map
    heading_map: dict[str, TemplateField] = {}
    for f in template.fields:
        heading_map[f.heading.lower().strip()] = f

    current_field: TemplateField | None = None
    buffer: list[str] = []

    def _flush() -> None:
        if current_field is None:
            return
        raw = "\n".join(buffer).strip()
        if current_field.type == "list" or current_field.type == "checklist":
            items = []
            for line in raw.split("\n"):
                stripped = line.strip().lstrip("-•*").strip()
                if stripped:
                    items.append(stripped)
            result[current_field.name] = items
        elif current_field.type == "date":
            result[current_field.name] = raw.split("\n", 1)[0].strip()
        else:
            result[current_field.name] = raw

    for line in lines:
        m = _HEADING_RE.match(line.strip())
        if m:
            _flush()
            heading_text = m.group(1).lower().strip()
            current_field = heading_map.get(heading_text)
            buffer = []
            continue
        if current_field is not None:
            buffer.append(line)

    _flush()
    return result


def _first_heading(content: str) -> str | None:
    for line in content.split("\n"):
        stripped = line.strip()
        m = _HEADING_RE.match(stripped)
        if m:
            return m.group(1)
    return None


def _parse_template(data: dict) -> Template:
    """Build a Template from a parsed YAML dict."""
    fields = []
    for f in data.get("fields", []):
        fields.append(TemplateField(
            name=f["name"],
            heading=f.get("heading", f["name"].title()),
            type=f.get("type", "text"),
            required=f.get("required", False),
            hint=f.get("hint", ""),
        ))
    return Template(
        name=data["name"],
        description=data.get("description", ""),
        fields=fields,
        title_prefix=data.get("title_prefix", ""),
    )
