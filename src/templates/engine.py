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
    type: str = "text"  # "text" | "list" | "date" | "checklist"
    required: bool = False
    hint: str = ""
    # Optional conditional. When set, the field is only rendered if the
    # expression evaluates truthy against the values passed to
    # render_pdf(). Accepted operators: ==, !=, in, not in, and, or,
    # not. Identifiers resolve against the values dict. See
    # _eval_condition for the exact grammar.
    when: str = ""
    block: str = ""  # named block for templates that `extends`


@dataclass
class Template:
    """A template definition."""

    name: str
    description: str
    fields: list[TemplateField] = field(default_factory=list)
    title_prefix: str = ""  # prepended to PDF title when pushed
    extends: str = ""  # name of parent template (optional)
    blocks: dict[str, list[TemplateField]] = field(default_factory=dict)


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

        # Resolve `extends` chains after every template is loaded so
        # parents are available regardless of file order.
        self._resolve_inheritance()

    def _load_dir(self, directory: Path) -> None:
        for yaml_file in directory.glob("*.yaml"):
            try:
                data = yaml.safe_load(yaml_file.read_text())
                template = _parse_template(data)
                self._templates[template.name] = template
            except Exception as e:
                logger.warning("Failed to load template %s: %s", yaml_file.name, e)

    def _resolve_inheritance(self) -> None:
        """Flatten ``extends`` chains into concrete field lists.

        Each template that references a parent has its parent's fields
        prepended, with child-defined ``blocks`` replacing parent
        blocks of the same name. Cycles are detected and abort loading
        of the offending child.
        """
        resolved: dict[str, Template] = {}

        def _resolve(name: str, path: list[str]) -> Template:
            if name in resolved:
                return resolved[name]
            if name in path:
                raise ValueError(f"Template cycle: {' -> '.join(path + [name])}")
            template = self._templates.get(name)
            if template is None or not template.extends:
                resolved[name] = template  # type: ignore[assignment]
                return template  # type: ignore[return-value]

            parent = _resolve(template.extends, path + [name])
            if parent is None:
                logger.warning(
                    "Template '%s' extends unknown '%s' — keeping as-is",
                    name,
                    template.extends,
                )
                resolved[name] = template
                return template

            merged_fields: list[TemplateField] = []
            for pf in parent.fields:
                # If a child block overrides a parent block, swap in
                # the child's fields here instead of the parent's.
                if pf.block and pf.block in template.blocks:
                    merged_fields.extend(template.blocks[pf.block])
                else:
                    merged_fields.append(pf)
            # Append any child fields that don't belong to a named
            # block (i.e. additions not overrides).
            merged_fields.extend(f for f in template.fields if not f.block)

            flattened = Template(
                name=template.name,
                description=template.description or parent.description,
                fields=merged_fields,
                title_prefix=template.title_prefix or parent.title_prefix,
                extends=template.extends,
                blocks=template.blocks,
            )
            resolved[name] = flattened
            return flattened

        for name in list(self._templates):
            try:
                self._templates[name] = _resolve(name, [])
            except ValueError as exc:
                logger.warning("Skipping template '%s': %s", name, exc)

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
        "TemplateTitle",
        fontSize=20,
        leading=26,
        spaceAfter=10 * mm,
        fontName="Helvetica-Bold",
    ),
    "heading": ParagraphStyle(
        "TemplateHeading",
        fontSize=14,
        leading=18,
        spaceBefore=6 * mm,
        spaceAfter=2 * mm,
        fontName="Helvetica-Bold",
    ),
    "hint": ParagraphStyle(
        "TemplateHint",
        fontSize=9,
        leading=12,
        textColor="#888888",
        spaceAfter=2 * mm,
    ),
    "line": ParagraphStyle(
        "TemplateLine",
        fontSize=11,
        leading=22,
        spaceAfter=4 * mm,
    ),
    "prefilled": ParagraphStyle(
        "TemplatePrefilled",
        fontSize=11,
        leading=16,
        spaceAfter=4 * mm,
        textColor="#333333",
    ),
}


def _render_template_pdf(template: Template, values: dict) -> bytes:
    """Render a template to PDF bytes, with writing space for each field."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )

    story: list = [
        Paragraph(template.title_prefix or template.name.title(), _TEMPLATE_STYLES["title"]),
    ]

    if template.description:
        story.append(Paragraph(template.description, _TEMPLATE_STYLES["hint"]))
        story.append(Spacer(1, 4 * mm))

    for field_def in template.fields:
        if field_def.when and not evaluate_condition(field_def.when, values):
            continue
        story.append(Paragraph(field_def.heading, _TEMPLATE_STYLES["heading"]))

        prefilled = values.get(field_def.name)
        if prefilled:
            story.append(Paragraph(str(prefilled), _TEMPLATE_STYLES["prefilled"]))
        elif field_def.hint:
            story.append(Paragraph(field_def.hint, _TEMPLATE_STYLES["hint"]))

        # Allocate writing space depending on field type
        if field_def.type in ("list", "checklist"):
            for _ in range(5):
                story.append(Paragraph("• ___________________________", _TEMPLATE_STYLES["line"]))
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
    """Build a Template from a parsed YAML dict.

    Supports ``extends:`` for inheritance and ``blocks:`` as a mapping
    of ``block_name → [field, ...]`` for override-style composition.
    Fields can carry a ``when:`` expression — see
    :func:`evaluate_condition` for the accepted grammar.
    """

    def _field_from_dict(f: dict, block: str = "") -> TemplateField:
        return TemplateField(
            name=f["name"],
            heading=f.get("heading", f["name"].title()),
            type=f.get("type", "text"),
            required=f.get("required", False),
            hint=f.get("hint", ""),
            when=f.get("when", ""),
            block=f.get("block", block),
        )

    fields = [_field_from_dict(f) for f in data.get("fields", [])]

    blocks: dict[str, list[TemplateField]] = {}
    for name, entries in (data.get("blocks") or {}).items():
        blocks[name] = [_field_from_dict(f, block=name) for f in entries]

    return Template(
        name=data["name"],
        description=data.get("description", ""),
        fields=fields,
        title_prefix=data.get("title_prefix", ""),
        extends=data.get("extends", ""),
        blocks=blocks,
    )


# ---------------------------------------------------------------------------
# when: expression sandbox
# ---------------------------------------------------------------------------

import ast  # noqa: E402  (imported here to keep sandbox code co-located)


class ConditionError(ValueError):
    """Raised when a ``when:`` expression contains unsafe syntax."""


# The only AST nodes permitted inside a ``when:`` expression. Anything
# else (function calls, attribute access, imports, comprehensions, ...)
# is rejected before evaluation. Keeps the sandbox tiny and auditable.
_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.In,
    ast.NotIn,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.List,
    ast.Tuple,
    ast.Set,
)


MAX_WHEN_EXPR_LEN = 500
MAX_WHEN_AST_NODES = 200


def evaluate_condition(expr: str, values: dict) -> bool:
    """Check a ``when:`` expression against the values mapping.

    Grammar (intentionally narrow — no function calls, no attribute
    access, no subscripting):

        expr    := bool_or
        bool_or := bool_and ('or' bool_and)*
        bool_and:= not_expr ('and' not_expr)*
        not_expr:= 'not' cmp | cmp
        cmp     := primary (('=='|'!='|'in'|'not in') primary)*
        primary := IDENT | STRING | NUMBER | '[' ... ']' | '(' expr ')'

    Identifier lookups resolve against ``values``; missing keys yield
    ``None``. Unsupported syntax raises ``ConditionError``.

    DoS guards: the expression is capped at ``MAX_WHEN_EXPR_LEN`` chars
    and ``MAX_WHEN_AST_NODES`` nodes. Both keep ``ast.parse`` and the
    recursive walker from blowing the stack on adversarial templates.
    """
    if not expr.strip():
        return True
    if len(expr) > MAX_WHEN_EXPR_LEN:
        raise ConditionError(
            f"when: expression too long ({len(expr)} chars, max {MAX_WHEN_EXPR_LEN})"
        )

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ConditionError(f"Invalid when expression: {exc}") from exc
    except (RecursionError, MemoryError) as exc:
        raise ConditionError("when: expression too deeply nested") from exc

    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_WHEN_AST_NODES:
        raise ConditionError(
            f"when: expression too complex ({len(nodes)} nodes, max {MAX_WHEN_AST_NODES})"
        )
    for node in nodes:
        if not isinstance(node, _ALLOWED_NODES):
            raise ConditionError(f"Disallowed syntax in when expression: {type(node).__name__}")

    try:
        return bool(_walk_node(tree.body, values))
    except ConditionError:
        raise
    except RecursionError as exc:
        raise ConditionError("when: expression too deeply nested") from exc
    except Exception as exc:
        raise ConditionError(f"Condition evaluation failed: {exc}") from exc


def _walk_node(node: ast.AST, values: dict):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return values.get(node.id)
    if isinstance(node, ast.BoolOp):
        items = [_walk_node(v, values) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(items)
        return any(items)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _walk_node(node.operand, values)
    if isinstance(node, ast.Compare):
        left = _walk_node(node.left, values)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _walk_node(comparator, values)
            if isinstance(op, ast.Eq) and left != right:
                return False
            if isinstance(op, ast.NotEq) and left == right:
                return False
            if isinstance(op, ast.In) and left not in (right or []):
                return False
            if isinstance(op, ast.NotIn) and left in (right or []):
                return False
            left = right
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [_walk_node(e, values) for e in node.elts]
    raise ConditionError(f"Unsupported node in when expression: {type(node).__name__}")


__all__ = [
    "Template",
    "TemplateField",
    "TemplateEngine",
    "evaluate_condition",
    "ConditionError",
]
