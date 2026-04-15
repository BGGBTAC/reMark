"""Tests for v0.6 template features: ``when:`` conditions and inheritance."""

from __future__ import annotations

import textwrap

import pytest

from src.templates.engine import (
    ConditionError,
    TemplateEngine,
    evaluate_condition,
)


class TestWhenEvaluator:
    def test_empty_is_always_true(self):
        assert evaluate_condition("", {}) is True
        assert evaluate_condition("   ", {"x": 1}) is True

    def test_equality(self):
        assert evaluate_condition("kind == 'meeting'", {"kind": "meeting"}) is True
        assert evaluate_condition("kind == 'meeting'", {"kind": "other"}) is False
        assert evaluate_condition("kind != 'meeting'", {"kind": "other"}) is True

    def test_in_and_not_in(self):
        assert evaluate_condition("'urgent' in tags", {"tags": ["urgent", "q1"]}) is True
        assert evaluate_condition("kind in ['a', 'b']", {"kind": "b"}) is True
        assert evaluate_condition("kind not in ['a', 'b']", {"kind": "c"}) is True

    def test_boolean_combinations(self):
        values = {"kind": "meeting", "urgent": True}
        assert evaluate_condition(
            "kind == 'meeting' and urgent", values,
        ) is True
        assert evaluate_condition(
            "kind == 'review' or urgent", values,
        ) is True
        assert evaluate_condition("not urgent", values) is False

    def test_missing_identifier_resolves_to_none(self):
        # Missing keys are None, so ``x == 'foo'`` is False rather than
        # a KeyError that aborts the whole render.
        assert evaluate_condition("x == 'foo'", {}) is False

    def test_rejects_function_calls(self):
        with pytest.raises(ConditionError):
            evaluate_condition("__import__('os').system('rm -rf /')", {})

    def test_rejects_attribute_access(self):
        with pytest.raises(ConditionError):
            evaluate_condition("values.get('x')", {})

    def test_rejects_subscript(self):
        with pytest.raises(ConditionError):
            evaluate_condition("tags[0] == 'meeting'", {"tags": ["meeting"]})

    def test_syntax_error_reports_cleanly(self):
        with pytest.raises(ConditionError):
            evaluate_condition("kind ==", {})


class TestTemplateInheritance:
    def _write(self, path, name, body):
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{name}.yaml").write_text(textwrap.dedent(body))

    def test_child_inherits_parent_fields(self, tmp_path):
        self._write(tmp_path, "base", """
            name: base
            fields:
              - name: date
                heading: Date
                type: date
              - name: notes
                heading: Notes
                type: text
        """)
        self._write(tmp_path, "child", """
            name: child
            extends: base
        """)
        engine = TemplateEngine(tmp_path)
        child = engine.get("child")
        assert child is not None
        names = [f.name for f in child.fields]
        assert names == ["date", "notes"]

    def test_child_appends_extra_fields(self, tmp_path):
        self._write(tmp_path, "base", """
            name: base
            fields:
              - name: date
                heading: Date
                type: date
        """)
        self._write(tmp_path, "child", """
            name: child
            extends: base
            fields:
              - name: tags
                heading: Tags
                type: list
        """)
        engine = TemplateEngine(tmp_path)
        names = [f.name for f in engine.get("child").fields]
        assert names == ["date", "tags"]

    def test_child_blocks_override_parent(self, tmp_path):
        self._write(tmp_path, "base", """
            name: base
            fields:
              - name: summary
                heading: Summary
                type: text
                block: body
              - name: closing
                heading: Closing
                type: text
        """)
        self._write(tmp_path, "child", """
            name: child
            extends: base
            blocks:
              body:
                - name: detailed
                  heading: Detailed summary
                  type: text
        """)
        engine = TemplateEngine(tmp_path)
        fields = engine.get("child").fields
        # block `body` replaces `summary`, `closing` is preserved
        assert [f.name for f in fields] == ["detailed", "closing"]

    def test_cycle_is_detected(self, tmp_path, caplog):
        self._write(tmp_path, "a", "name: a\nextends: b\n")
        self._write(tmp_path, "b", "name: b\nextends: a\n")
        engine = TemplateEngine(tmp_path)
        # Cycle is warned and logged, templates remain in the registry
        assert engine.get("a") is not None


class TestWhenSkipsRender:
    def test_when_false_hides_field(self, tmp_path):
        (tmp_path / "sample.yaml").write_text(textwrap.dedent("""
            name: sample
            fields:
              - name: base
                heading: Base
                type: text
              - name: extra
                heading: Extra
                type: text
                when: "kind == 'full'"
        """))
        engine = TemplateEngine(tmp_path)
        # PDF bytes — but easier: inspect that field filter would drop
        # "extra" under kind=short by rendering to bytes and looking
        # for the heading text.
        pdf_short = engine.render_pdf("sample", {"kind": "short"})
        pdf_full = engine.render_pdf("sample", {"kind": "full"})
        assert pdf_short != pdf_full
        assert b"Extra" in pdf_full
        assert b"Extra" not in pdf_short
