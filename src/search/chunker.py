"""Markdown-aware chunking for semantic search.

Splits notes into semantically coherent chunks that respect heading
structure and paragraph boundaries. Keeps chunks under configured
character limit with optional overlap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Skip frontmatter and code fences when chunking
FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n?", re.DOTALL)
CODE_FENCE_RE = re.compile(r"```[\s\S]*?```", re.MULTILINE)


@dataclass
class Chunk:
    """A single retrievable piece of a note."""

    index: int
    content: str
    heading_path: list[str]
    start_offset: int

    @property
    def heading_context(self) -> str:
        return " › ".join(self.heading_path) if self.heading_path else ""


def chunk_markdown(
    text: str,
    chunk_size: int = 512,
    chunk_overlap: int = 50,
) -> list[Chunk]:
    """Split Markdown content into chunks for embedding.

    Strategy:
    1. Strip frontmatter
    2. Walk lines, tracking current heading path
    3. Group paragraphs under the same heading
    4. Split when accumulated text > chunk_size, with heading context repeated
    """
    cleaned = FRONTMATTER_RE.sub("", text, count=1).lstrip()
    if not cleaned.strip():
        return []

    # Extract code fences and replace with placeholders so we don't split inside
    fences: list[str] = []

    def _extract_fence(match: re.Match) -> str:
        fences.append(match.group(0))
        return f"__FENCE_{len(fences) - 1}__"

    placeholdered = CODE_FENCE_RE.sub(_extract_fence, cleaned)

    # Parse lines into (heading_path, paragraph) tuples
    sections = _parse_sections(placeholdered)

    # Restore code fences in paragraph text
    def _restore(text: str) -> str:
        def _sub(match: re.Match) -> str:
            idx = int(match.group(1))
            return fences[idx] if 0 <= idx < len(fences) else match.group(0)

        return re.sub(r"__FENCE_(\d+)__", _sub, text)

    chunks: list[Chunk] = []
    offset = [0]
    index = [0]

    for heading_path, paragraphs in sections:
        _process_section(
            paragraphs,
            heading_path,
            chunks,
            offset,
            index,
            chunk_size,
            chunk_overlap,
            _restore,
        )

    return chunks


def _process_section(
    paragraphs: list[str],
    heading_path: list[str],
    chunks: list[Chunk],
    offset: list[int],
    index: list[int],
    chunk_size: int,
    chunk_overlap: int,
    restore: callable,
) -> None:
    """Emit chunks for one section of paragraphs under a heading path."""
    buffer_parts: list[str] = []
    buffer_len = 0

    def emit(force: bool) -> None:
        nonlocal buffer_parts, buffer_len
        if not buffer_parts:
            return
        if not force and buffer_len < chunk_size // 2:
            return

        body = "\n\n".join(buffer_parts).strip()
        if body:
            chunks.append(
                Chunk(
                    index=index[0],
                    content=restore(body),
                    heading_path=list(heading_path),
                    start_offset=offset[0],
                )
            )
            index[0] += 1
            offset[0] += len(body)

            if chunk_overlap > 0 and len(body) > chunk_overlap:
                tail = body[-chunk_overlap:]
                buffer_parts = [tail]
                buffer_len = len(tail)
                return

        buffer_parts = []
        buffer_len = 0

    for paragraph in paragraphs:
        para_len = len(paragraph)
        if buffer_len + para_len > chunk_size and buffer_parts:
            emit(force=True)

        buffer_parts.append(paragraph)
        buffer_len += para_len + 2

        if buffer_len >= chunk_size:
            emit(force=True)

    emit(force=True)


def _parse_sections(text: str) -> list[tuple[list[str], list[str]]]:
    """Parse markdown text into sections grouped by heading path.

    Returns a list of (heading_path, paragraphs) tuples where heading_path
    is the stack of headings leading to that section.
    """
    sections: list[tuple[list[str], list[str]]] = []
    current_headings: list[tuple[int, str]] = []  # (level, text)
    current_paragraphs: list[str] = []
    buffer_lines: list[str] = []

    def _flush_buffer() -> None:
        nonlocal buffer_lines
        if buffer_lines:
            text = "\n".join(buffer_lines).strip()
            if text:
                current_paragraphs.append(text)
            buffer_lines = []

    def _flush_section() -> None:
        nonlocal current_paragraphs
        _flush_buffer()
        if current_paragraphs:
            heading_path = [h[1] for h in current_headings]
            sections.append((heading_path, list(current_paragraphs)))
            current_paragraphs = []

    for line in text.split("\n"):
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            _flush_section()
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            # Pop headings at or below this level
            current_headings = [h for h in current_headings if h[0] < level]
            current_headings.append((level, title))
            continue

        if not line.strip():
            _flush_buffer()
            continue

        buffer_lines.append(line)

    _flush_section()

    return sections
