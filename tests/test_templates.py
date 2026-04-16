"""Tests for the on-device template engine."""

import pytest
import yaml

from src.templates.engine import (
    Template,
    TemplateEngine,
    TemplateField,
    _extract_fields,
    _first_heading,
    _parse_template,
)

# =====================
# _parse_template
# =====================


class TestParseTemplate:
    def test_minimal(self):
        t = _parse_template({"name": "t", "fields": []})
        assert t.name == "t"
        assert t.description == ""
        assert t.fields == []

    def test_with_fields(self):
        t = _parse_template(
            {
                "name": "meeting",
                "description": "Meeting notes",
                "fields": [
                    {"name": "date", "heading": "Date", "type": "date"},
                    {
                        "name": "actions",
                        "heading": "Actions",
                        "type": "checklist",
                        "required": True,
                    },
                ],
            }
        )
        assert len(t.fields) == 2
        assert t.fields[0].type == "date"
        assert t.fields[1].type == "checklist"
        assert t.fields[1].required

    def test_default_heading(self):
        t = _parse_template(
            {
                "name": "x",
                "fields": [{"name": "agenda"}],
            }
        )
        assert t.fields[0].heading == "Agenda"


# =====================
# TemplateEngine
# =====================


class TestTemplateEngine:
    def test_loads_builtins(self, tmp_path):
        engine = TemplateEngine(tmp_path)
        names = {t.name for t in engine.list_templates()}
        assert "meeting" in names
        assert "daily" in names
        assert "project-review" in names

    def test_user_template_overrides(self, tmp_path):
        user_dir = tmp_path / "templates"
        user_dir.mkdir()
        (user_dir / "custom.yaml").write_text(
            yaml.dump(
                {
                    "name": "custom",
                    "description": "User-defined",
                    "fields": [{"name": "note", "heading": "Note"}],
                }
            )
        )

        engine = TemplateEngine(user_dir)
        custom = engine.get("custom")
        assert custom is not None
        assert custom.description == "User-defined"

    def test_malformed_template_skipped(self, tmp_path):
        user_dir = tmp_path / "bad"
        user_dir.mkdir()
        (user_dir / "bad.yaml").write_text(":::not valid yaml:::")

        engine = TemplateEngine(user_dir)
        # Should load builtins regardless
        assert engine.get("meeting") is not None

    def test_render_pdf(self, tmp_path):
        engine = TemplateEngine(tmp_path)
        pdf = engine.render_pdf("meeting")
        assert pdf[:5] == b"%PDF-"
        assert len(pdf) > 500

    def test_render_pdf_with_prefilled(self, tmp_path):
        engine = TemplateEngine(tmp_path)
        pdf = engine.render_pdf("daily", extra_values={"date": "2026-04-14"})
        assert pdf[:5] == b"%PDF-"

    def test_render_pdf_unknown_raises(self, tmp_path):
        engine = TemplateEngine(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            engine.render_pdf("nonexistent")

    def test_extract_fields_basic(self, tmp_path):
        engine = TemplateEngine(tmp_path)
        content = (
            "# Meeting\n\n"
            "## Date\n\n2026-04-14\n\n"
            "## Attendees\n\n- Alice\n- Bob\n\n"
            "## Agenda\n\n- Intro\n- Review\n\n"
            "## Discussion\n\nWe discussed things.\n\n"
            "## Decisions\n\n- Launch next week\n\n"
            "## Action items\n\n- [ ] Send report\n- [ ] Update docs\n"
        )
        fields = engine.extract_fields("meeting", content)

        assert fields["date"] == "2026-04-14"
        assert fields["attendees"] == ["Alice", "Bob"]
        assert fields["agenda"] == ["Intro", "Review"]
        assert "discussed things" in fields["discussion"]
        assert fields["decisions"] == ["Launch next week"]
        assert len(fields["actions"]) == 2

    def test_extract_unknown_raises(self, tmp_path):
        engine = TemplateEngine(tmp_path)
        with pytest.raises(ValueError):
            engine.extract_fields("nope", "# x")

    def test_detect_template_by_frontmatter(self, tmp_path):
        engine = TemplateEngine(tmp_path)
        template = engine.detect_template({"template": "meeting"}, "Content")
        assert template is not None
        assert template.name == "meeting"

    def test_detect_template_by_heading(self, tmp_path):
        engine = TemplateEngine(tmp_path)
        template = engine.detect_template({}, "# Daily Review\n\nContent")
        assert template is not None
        assert template.name == "daily"

    def test_detect_no_match(self, tmp_path):
        engine = TemplateEngine(tmp_path)
        template = engine.detect_template({}, "# Some Random Heading\n\nContent")
        assert template is None


# =====================
# _extract_fields low-level
# =====================


class TestExtractFieldsLowLevel:
    def test_empty_content(self):
        t = Template(
            name="t",
            description="",
            fields=[
                TemplateField(name="x", heading="X"),
            ],
        )
        assert _extract_fields(t, "") == {}

    def test_checklist_items_lose_marker(self):
        t = Template(
            name="t",
            description="",
            fields=[
                TemplateField(name="tasks", heading="Tasks", type="checklist"),
            ],
        )
        content = "# Tasks\n\n- [ ] Alpha\n- [ ] Beta\n"
        result = _extract_fields(t, content)
        # Items contain the "[ ]" notation; we just strip leading bullets/stars
        assert len(result["tasks"]) == 2

    def test_date_keeps_first_line(self):
        t = Template(
            name="t",
            description="",
            fields=[
                TemplateField(name="d", heading="Date", type="date"),
            ],
        )
        content = "## Date\n\n2026-04-14\n\nExtra noise\n"
        result = _extract_fields(t, content)
        assert result["d"] == "2026-04-14"


class TestFirstHeading:
    def test_finds_h1(self):
        assert _first_heading("# Title\n\nBody") == "Title"

    def test_finds_deeper_heading(self):
        assert _first_heading("## Sub\n\nBody") == "Sub"

    def test_no_heading(self):
        assert _first_heading("Just text") is None


# =====================
# State integration
# =====================


class TestTemplateState:
    def test_record_and_extract_roundtrip(self, tmp_path):
        from src.sync.state import SyncState

        state = SyncState(tmp_path / "t.db")
        state.record_template_push("doc-abc", "meeting")

        entry = state.get_template_for_doc("doc-abc")
        assert entry["template_name"] == "meeting"
        assert entry["filled_at"] is None

        state.mark_template_filled("doc-abc", "/vault/Notes/meeting.md")
        entry = state.get_template_for_doc("doc-abc")
        assert entry["vault_path"] == "/vault/Notes/meeting.md"
        state.close()
