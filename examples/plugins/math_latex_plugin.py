"""Math / LaTeX auto-delimiter plugin.

Drops into ``~/.config/remark/plugins/`` (or wherever
``plugins.plugin_dir`` in your ``config.yaml`` points) and runs as a
``NoteProcessorHook``: whenever the engine finishes building a note it
looks for bare LaTeX fragments and wraps them in ``$...$`` so Obsidian
(and any Markdown renderer with MathJax/KaTeX) picks them up as math.

It works purely on text — no image access required — so it's safe to
enable for any vault. For heavier lifting (image → LaTeX OCR) see the
``plugins.settings.math_latex.use_pix2text`` toggle below: when enabled
the plugin registers a secondary ``OCRBackendHook`` that delegates to
`pix2text`_ or MathPix for any image-like payload the pipeline hands
it. Both are opt-in and optional; plain Python installs see only the
NoteProcessor path.

.. _pix2text: https://github.com/breezedeus/Pix2Text
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.plugins.hooks import NoteProcessorHook, OCRBackendHook, PluginMetadata

logger = logging.getLogger(__name__)


# Commands and symbols that strongly suggest a LaTeX fragment. The list
# is deliberately conservative — false positives wrap normal prose in
# dollar signs, which is worse than missing a match.
_LATEX_MARKERS = [
    r"\\frac\{[^{}]+\}\{[^{}]+\}",            # \frac{a}{b}
    r"\\sqrt\{[^{}]+\}",                       # \sqrt{...}
    r"\\(sum|int|prod|oint|iint)_[^\s]+\^[^\s]+",  # \sum_{i=0}^{n}
    r"\\(alpha|beta|gamma|delta|theta|lambda|mu|pi|sigma|omega)\b",
    r"\\(mathbb|mathcal|mathbf|mathrm)\{[A-Za-z]+\}",
    r"\\begin\{equation\}[\s\S]+?\\end\{equation\}",
    r"\\begin\{align\*?\}[\s\S]+?\\end\{align\*?\}",
]

_LATEX_RE = re.compile("|".join(_LATEX_MARKERS))

# Skip matches that are already inside math delimiters or code blocks.
_FENCED_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_EXISTING_MATH_RE = re.compile(r"\$[^$\n]+\$|\$\$[\s\S]+?\$\$")


class MathLatexProcessor(NoteProcessorHook):
    """Wrap bare LaTeX fragments in $...$ so Obsidian renders them."""

    def __init__(self) -> None:
        self._wrap_inline = True
        self._wrap_block = True

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="math_latex",
            version="0.1.0",
            description=(
                "Auto-wraps bare LaTeX fragments in $...$ for Obsidian math "
                "rendering. Opt-in pix2text/MathPix OCR backend available."
            ),
            author="BGGBTAC",
        )

    def configure(self, settings: dict[str, Any]) -> None:
        self._wrap_inline = bool(settings.get("wrap_inline", True))
        self._wrap_block = bool(settings.get("wrap_block", True))

    async def process(
        self, content: str, frontmatter: dict,
    ) -> tuple[str, dict]:
        if not self._wrap_inline and not self._wrap_block:
            return content, frontmatter

        # Build a set of ranges we must not touch: existing math
        # delimiters, inline code, fenced code blocks.
        forbidden: list[tuple[int, int]] = []
        for pattern in (_FENCED_BLOCK_RE, _INLINE_CODE_RE, _EXISTING_MATH_RE):
            for m in pattern.finditer(content):
                forbidden.append(m.span())

        def _is_forbidden(start: int, end: int) -> bool:
            return any(
                fs <= start and end <= fe for fs, fe in forbidden
            )

        # Collect non-overlapping LaTeX matches in one pass, emit the
        # wrapped output in a single rebuild — string slicing keeps us
        # O(N) in content length.
        pieces: list[str] = []
        cursor = 0
        wrapped_count = 0
        for match in _LATEX_RE.finditer(content):
            start, end = match.span()
            if _is_forbidden(start, end):
                continue
            is_block = "\\begin" in match.group(0)
            if is_block and not self._wrap_block:
                continue
            if not is_block and not self._wrap_inline:
                continue
            pieces.append(content[cursor:start])
            if is_block:
                pieces.append(f"$$\n{match.group(0)}\n$$")
            else:
                pieces.append(f"${match.group(0)}$")
            cursor = end
            wrapped_count += 1

        if wrapped_count == 0:
            return content, frontmatter

        pieces.append(content[cursor:])
        new_content = "".join(pieces)
        frontmatter.setdefault("math_fragments", wrapped_count)
        logger.debug("math_latex: wrapped %d fragment(s)", wrapped_count)
        return new_content, frontmatter


class MathLatexOCR(OCRBackendHook):
    """Optional OCR backend that delegates to pix2text or MathPix.

    Registration is gated on the ``backend`` setting so users without
    either library installed see no runtime error. The engine can pick
    this backend up via ``plugins`` discovery and call it when an image
    is handed to the OCR pipeline — the wiring for that call site is
    planned for a follow-up release; today the hook is a stub that
    returns empty text if invoked.
    """

    def __init__(self) -> None:
        self._backend = "pix2text"
        self._mathpix_app_id_env = "MATHPIX_APP_ID"
        self._mathpix_app_key_env = "MATHPIX_APP_KEY"

    @property
    def metadata(self) -> PluginMetadata:
        return PluginMetadata(
            name="math_latex_ocr",
            version="0.1.0",
            description="Math OCR via pix2text (default) or MathPix API.",
            author="BGGBTAC",
        )

    def configure(self, settings: dict[str, Any]) -> None:
        self._backend = settings.get("backend", "pix2text")
        self._mathpix_app_id_env = settings.get(
            "mathpix_app_id_env", self._mathpix_app_id_env,
        )
        self._mathpix_app_key_env = settings.get(
            "mathpix_app_key_env", self._mathpix_app_key_env,
        )

    async def recognize_page(self, page_image: bytes) -> dict:
        if self._backend == "mathpix":
            return await self._mathpix(page_image)
        return await self._pix2text(page_image)

    async def _pix2text(self, page_image: bytes) -> dict:
        try:
            from pix2text import Pix2Text  # type: ignore[import-not-found]
        except ImportError:
            return {
                "text": "",
                "confidence": 0.0,
                "engine": "math_latex_ocr/pix2text(not-installed)",
            }
        import asyncio
        import tempfile
        from pathlib import Path

        def _run() -> str:
            p2t = Pix2Text.from_config()
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(page_image)
                tmp_path = tmp.name
            try:
                return p2t.recognize_text_formula(tmp_path, return_text=True)
            finally:
                Path(tmp_path).unlink(missing_ok=True)

        text = await asyncio.to_thread(_run)
        return {
            "text": text,
            "confidence": 0.85,
            "engine": "math_latex_ocr/pix2text",
        }

    async def _mathpix(self, page_image: bytes) -> dict:
        import base64
        import os

        import httpx

        app_id = os.environ.get(self._mathpix_app_id_env)
        app_key = os.environ.get(self._mathpix_app_key_env)
        if not app_id or not app_key:
            logger.warning("MathPix credentials missing; returning empty")
            return {
                "text": "",
                "confidence": 0.0,
                "engine": "math_latex_ocr/mathpix(unauthenticated)",
            }
        encoded = base64.b64encode(page_image).decode("ascii")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.mathpix.com/v3/text",
                headers={"app_id": app_id, "app_key": app_key},
                json={"src": f"data:image/png;base64,{encoded}"},
                timeout=30,
            )
        if resp.status_code != 200:
            return {
                "text": "",
                "confidence": 0.0,
                "engine": f"math_latex_ocr/mathpix(http-{resp.status_code})",
            }
        data = resp.json()
        return {
            "text": data.get("text", ""),
            "confidence": data.get("confidence", 0.9),
            "engine": "math_latex_ocr/mathpix",
        }
