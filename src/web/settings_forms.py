"""Auto-render HTML forms from Pydantic config models.

The web settings UI introspects ``AppConfig`` at request time so new
config keys show up in the form immediately — no duplicated schema, no
hand-written HTML per section. Complex types are mapped to the
simplest reasonable input:

======================  ===============================================
Python annotation       Form control
======================  ===============================================
bool                    checkbox
int / float             <input type=number>
str                     <input type=text> (or password for secrets)
Literal["a", "b"]       <select>
list[str]               <textarea> — one value per line
nested BaseModel        recursive fieldset
dict[str, ...]          <textarea> — JSON
======================  ===============================================

``parse_form()`` takes the submitted raw form dict back into the shape
Pydantic expects so validation can run against the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from src.web.config_writer import MASK, is_secret_field


@dataclass
class FormField:
    """Rendering instructions for a single field in a settings form."""

    name: str              # dotted path, e.g. "git.auto_push"
    label: str             # human label derived from the field name
    kind: str              # "text" | "password" | "number" | "bool"
                           # | "select" | "textarea" | "json"
    value: Any             # current value (possibly masked for secrets)
    help: str = ""         # from Pydantic field description
    choices: list[str] = field(default_factory=list)  # for select
    is_secret: bool = False


@dataclass
class FormGroup:
    """A set of fields grouped under a heading (one per nested model)."""

    title: str
    fields: list[FormField]
    subgroups: list[FormGroup] = field(default_factory=list)


def build_form(
    model_cls: type[BaseModel],
    values: Any,
    path_prefix: str = "",
    title: str | None = None,
) -> FormGroup:
    """Walk ``model_cls`` and build a FormGroup describing the inputs.

    ``values`` can be either a Pydantic model instance or a plain dict
    — whichever is convenient for the caller.
    """
    if isinstance(values, BaseModel):
        values = values.model_dump()
    values = values or {}

    group = FormGroup(
        title=title or model_cls.__name__.replace("Config", ""),
        fields=[],
        subgroups=[],
    )

    for name, info in model_cls.model_fields.items():
        annotation = info.annotation
        current = values.get(name)
        dotted = f"{path_prefix}{name}"
        label = name.replace("_", " ").capitalize()
        help_text = info.description or ""

        nested_cls = _nested_model(annotation)
        if nested_cls is not None:
            sub = build_form(
                nested_cls, current or {},
                path_prefix=f"{dotted}.",
                title=label,
            )
            group.subgroups.append(sub)
            continue

        kind, choices = _map_control(annotation)
        secret = is_secret_field(name)

        if secret and current:
            display_value = MASK
        elif kind == "textarea":
            display_value = "\n".join(str(v) for v in (current or []))
        elif kind == "json":
            display_value = json.dumps(
                current if current is not None else [],
                indent=2,
            )
        elif kind == "bool":
            display_value = bool(current)
        elif current is None:
            display_value = ""
        else:
            display_value = current

        group.fields.append(FormField(
            name=dotted,
            label=label,
            kind="password" if secret else kind,
            value=display_value,
            help=help_text,
            choices=choices,
            is_secret=secret,
        ))

    return group


def parse_form(
    model_cls: type[BaseModel],
    raw: dict[str, Any],
    path_prefix: str = "",
) -> dict[str, Any]:
    """Convert browser-submitted strings back to model-ready types.

    Returns a dict shaped like ``model_cls.model_dump()`` so the caller
    can feed it into the matching Pydantic model for validation before
    persisting.
    """
    out: dict[str, Any] = {}

    for name, info in model_cls.model_fields.items():
        annotation = info.annotation
        dotted = f"{path_prefix}{name}"

        nested_cls = _nested_model(annotation)
        if nested_cls is not None:
            out[name] = parse_form(nested_cls, raw, path_prefix=f"{dotted}.")
            continue

        kind, _ = _map_control(annotation)
        submitted = raw.get(dotted)

        if is_secret_field(name) and submitted == MASK:
            # Sentinel — tell the writer to leave this key untouched.
            out[name] = MASK
            continue

        out[name] = _coerce(kind, submitted, annotation)

    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    """Return ``annotation`` if it's a BaseModel subclass, else None.

    Handles ``Optional[Foo]`` by unwrapping the ``None``.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    origin = get_origin(annotation)
    if origin is Union:
        for arg in get_args(annotation):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                return arg
    return None


def _map_control(annotation: Any) -> tuple[str, list[str]]:
    """Map a Python annotation to (input kind, select choices)."""
    origin = get_origin(annotation)

    # Literal[...] → <select>
    if origin is Literal:
        return ("select", [str(v) for v in get_args(annotation)])

    # Unwrap Optional[T]
    if origin is Union:
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        if len(non_none) == 1:
            return _map_control(non_none[0])

    if annotation is bool:
        return ("bool", [])
    if annotation in (int, float):
        return ("number", [])
    if annotation is str:
        return ("text", [])

    if origin in (list, set, tuple):
        # list[BaseModel] must round-trip as structured JSON, not a
        # free-text one-per-line list. Anything else — list[str],
        # list[int] — stays the convenient textarea.
        args = get_args(annotation)
        if args and isinstance(args[0], type) and issubclass(args[0], BaseModel):
            return ("json", [])
        return ("textarea", [])
    if origin is dict:
        return ("json", [])

    # Enums
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return ("select", [e.value for e in annotation])

    # Fallback — let the user edit as free text, parsed as JSON.
    return ("json", [])


def _coerce(kind: str, raw: Any, annotation: Any) -> Any:
    """Convert a raw form value into the Python type Pydantic wants."""
    if raw is None:
        return None

    if kind == "bool":
        # Checkboxes submit "on" when checked, absent otherwise.
        return bool(raw) and str(raw).lower() not in ("", "false", "0", "off")

    if kind == "number":
        if raw == "" or raw is None:
            return None
        origin = get_origin(annotation)
        if origin is Union:
            types = [a for a in get_args(annotation) if a is not type(None)]
            base = types[0] if types else float
        else:
            base = annotation
        try:
            return base(raw) if base in (int, float) else float(raw)
        except (TypeError, ValueError):
            return raw

    if kind == "textarea":
        if isinstance(raw, list):
            items = [line for line in raw if line]
        else:
            items = [
                line.strip() for line in str(raw).splitlines() if line.strip()
            ]
        # Honour the inner type when the annotation is e.g. list[int].
        inner_args = get_args(annotation)
        inner = inner_args[0] if inner_args else str
        if inner in (int, float):
            coerced: list[Any] = []
            for item in items:
                try:
                    coerced.append(inner(item))
                except (TypeError, ValueError):
                    coerced.append(item)
            return coerced
        return items

    if kind == "json":
        if isinstance(raw, (dict, list)):
            return raw
        text = str(raw).strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return raw

    if kind == "select":
        return str(raw)

    # text, password
    return str(raw) if raw is not None else ""


__all__ = ["FormField", "FormGroup", "build_form", "parse_form"]
