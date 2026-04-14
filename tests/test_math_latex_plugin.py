"""Tests for the example math_latex plugin."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PLUGIN_PATH = (
    Path(__file__).resolve().parent.parent
    / "examples" / "plugins" / "math_latex_plugin.py"
)


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "math_latex_plugin_test", _PLUGIN_PATH,
    )
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[spec.name] = module  # type: ignore[union-attr]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@pytest.fixture
def processor():
    mod = _load_plugin_module()
    return mod.MathLatexProcessor()


class TestMathLatexProcessor:
    @pytest.mark.asyncio
    async def test_wraps_inline_fragment(self, processor):
        content = "The area is \\frac{1}{2} h b."
        out, _ = await processor.process(content, {})
        assert "$\\frac{1}{2}$" in out

    @pytest.mark.asyncio
    async def test_skips_existing_math(self, processor):
        content = "Already wrapped: $\\frac{a}{b}$ here."
        out, _ = await processor.process(content, {})
        # Should not double-wrap
        assert out.count("$") == 2

    @pytest.mark.asyncio
    async def test_skips_code_fences(self, processor):
        content = (
            "Before.\n"
            "```python\n"
            "# \\alpha is not math here\n"
            "print(r'\\frac{1}{2}')\n"
            "```\n"
            "After: \\alpha should be math."
        )
        out, _ = await processor.process(content, {})
        # Fragment inside fence stays untouched
        assert "```python\n# \\alpha is not math here" in out
        assert "print(r'\\frac{1}{2}')" in out
        # Fragment outside gets wrapped
        assert "After: $\\alpha$ should be math" in out

    @pytest.mark.asyncio
    async def test_block_environment_wrapped_with_dollar_dollar(self, processor):
        content = (
            "See below:\n"
            "\\begin{equation}\n"
            "E = mc^2\n"
            "\\end{equation}\n"
            "done."
        )
        out, _ = await processor.process(content, {})
        assert "$$\n\\begin{equation}" in out
        assert "\\end{equation}\n$$" in out

    @pytest.mark.asyncio
    async def test_no_latex_returns_unchanged(self, processor):
        content = "Just plain prose, no math at all."
        out, fm = await processor.process(content, {})
        assert out == content
        assert "math_fragments" not in fm

    @pytest.mark.asyncio
    async def test_disabled_is_noop(self, processor):
        processor.configure({"wrap_inline": False, "wrap_block": False})
        content = "Has \\alpha fragment."
        out, _ = await processor.process(content, {})
        assert out == content

    @pytest.mark.asyncio
    async def test_frontmatter_records_count(self, processor):
        content = "Two fragments: \\alpha and \\beta done."
        _, fm = await processor.process(content, {})
        assert fm["math_fragments"] == 2
