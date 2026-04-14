"""Read/modify/write ``config.yaml`` with comment-preserving YAML.

The web-based settings UI mutates the on-disk config one section at a
time. PyYAML (``safe_dump``) loses comments and reflows formatting, so
we use ``ruamel.yaml`` which does a true round-trip. Writes are atomic:
we dump to a sibling tempfile and ``os.replace`` over the target.

Secrets:
- Any field whose name matches ``_SECRET_FIELD_RE`` is masked on read
  with the sentinel ``MASK``. A POST that leaves the value as the
  sentinel means "don't touch the existing value"; any other string
  overwrites it. This lets the form render without ever leaking a
  secret back to the browser.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

logger = logging.getLogger(__name__)

MASK = "__keep_existing__"

_SECRET_FIELD_RE = re.compile(
    r"(password|secret|token|api_key|private_key|client_secret)",
    re.IGNORECASE,
)


def is_secret_field(name: str) -> bool:
    return bool(_SECRET_FIELD_RE.search(name))


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 120
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load_yaml(path: str | Path) -> dict:
    """Load the YAML file as a round-trippable mapping. Empty → {}."""
    path = Path(path).expanduser()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = _yaml().load(fh)
    return data or {}


def write_yaml(path: str | Path, data: Any) -> None:
    """Atomically write ``data`` back to ``path`` preserving comments."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            _yaml().dump(data, fh)
        os.replace(tmp_name, path)
    except Exception:
        # Best-effort cleanup — ignore secondary errors so the caller
        # still sees the real failure.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def update_section(
    path: str | Path,
    section: str,
    updates: dict[str, Any],
    secret_keys: set[str] | None = None,
) -> dict:
    """Merge ``updates`` into ``path``'s ``section`` and save.

    ``secret_keys`` lists field names whose POST value should be
    treated as a "leave alone" sentinel when equal to ``MASK``. They're
    simply dropped from ``updates`` before the merge.
    """
    secret_keys = secret_keys or set()
    data = load_yaml(path)

    section_data = data.get(section) or {}
    if not isinstance(section_data, dict):
        raise ValueError(
            f"Config section '{section}' is not a mapping — refusing to "
            "write over a scalar/list from the web UI."
        )

    for key, value in updates.items():
        if key in secret_keys and value == MASK:
            # Keep the existing secret untouched.
            continue
        _deep_set(section_data, key.split("."), value)

    data[section] = section_data
    write_yaml(path, data)
    return section_data


def _deep_set(target: dict, keys: list[str], value: Any) -> None:
    """Write ``value`` into ``target`` at a dotted-key path."""
    for key in keys[:-1]:
        next_obj = target.get(key)
        if not isinstance(next_obj, dict):
            next_obj = {}
            target[key] = next_obj
        target = next_obj
    target[keys[-1]] = value


__all__ = [
    "MASK", "is_secret_field", "load_yaml", "write_yaml", "update_section",
]
