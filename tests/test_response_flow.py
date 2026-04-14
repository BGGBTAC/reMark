"""Tests for the response generation and push flow."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.config import ResponseConfig
from src.obsidian.vault import ObsidianVault
from src.response.generator import (
    ResponseGenerator,
    _extract_action_items,
    _extract_questions,
    _extract_wiki_links,
    _format_as_markdown,
    should_auto_trigger,
)
from src.response.pdf_generator import ResponseContent
from src.response.uploader import ResponseUploader

# =====================
# _extract_questions
# =====================

class TestExtractQuestions:
    def test_q_pattern(self):
        text = "Some notes\nQ: What is the deadline?\nMore notes"
        qs = _extract_questions(text)
        assert "What is the deadline?" in qs

    def test_question_pattern(self):
        text = "Question: How do we solve X?\nRegular text"
        qs = _extract_questions(text)
        assert "How do we solve X?" in qs

    def test_standalone_question(self):
        text = "Should we migrate to the new framework?"
        qs = _extract_questions(text)
        assert qs == ["Should we migrate to the new framework?"]

    def test_short_questions_ignored(self):
        text = "Why? Who?"
        qs = _extract_questions(text)
        assert qs == []

    def test_deduplicates(self):
        text = "Q: Same question?\nSame question?"
        qs = _extract_questions(text)
        assert len(qs) == 1

    def test_empty(self):
        assert _extract_questions("") == []
        assert _extract_questions("No questions here at all.") == []


# =====================
# _extract_action_items
# =====================

class TestExtractActionItems:
    def test_simple_checkbox(self):
        items = _extract_action_items("- [ ] Do the thing")
        assert len(items) == 1
        assert items[0]["task"] == "Do the thing"
        assert items[0]["priority"] == "medium"

    def test_checkbox_with_assignee(self):
        items = _extract_action_items("- [ ] Review PR @alice")
        assert len(items) == 1
        assert items[0]["assignee"] == "alice"
        assert "alice" not in items[0]["task"]

    def test_checkbox_with_deadline_paren(self):
        items = _extract_action_items("- [ ] Write report (due: Friday)")
        assert items[0]["deadline"] == "Friday"

    def test_checkbox_with_emoji_deadline(self):
        items = _extract_action_items("- [ ] Ship feature 📅 2026-05-01")
        assert items[0]["deadline"] == "2026-05-01"

    def test_high_priority(self):
        items = _extract_action_items("- [ ] Fix bug #priority-high")
        assert items[0]["priority"] == "high"
        assert "#priority-high" not in items[0]["task"]

    def test_question_checkbox(self):
        items = _extract_action_items("- [?] What should we do?")
        assert items[0]["type"] == "question"

    def test_ignores_checked(self):
        items = _extract_action_items("- [x] Already done")
        assert items == []

    def test_mixed_items(self):
        text = "- [ ] Task one\n- [x] Done thing\n- [?] A question\n- [ ] Task two"
        items = _extract_action_items(text)
        assert len(items) == 3


# =====================
# _extract_wiki_links
# =====================

class TestExtractWikiLinks:
    def test_single_link(self):
        links = _extract_wiki_links("See [[Meeting Notes]] for context")
        assert links == ["Meeting Notes"]

    def test_multiple_links(self):
        text = "Related: [[Note A]] and [[Note B]]"
        links = _extract_wiki_links(text)
        assert "Note A" in links
        assert "Note B" in links

    def test_deduplicates(self):
        text = "[[Same]] and [[Same]] again"
        links = _extract_wiki_links(text)
        assert len(links) == 1

    def test_no_links(self):
        assert _extract_wiki_links("plain text") == []


# =====================
# _format_as_markdown
# =====================

class TestFormatAsMarkdown:
    def test_full_content(self):
        content = ResponseContent(
            note_title="Test Note",
            summary="Brief summary",
            key_points=["Point A", "Point B"],
            action_items=[
                {"task": "Do X", "priority": "high", "type": "task"},
                {"task": "Ask Y", "type": "question"},
            ],
            analysis="Some analysis",
            related_notes=["Other Note"],
        )
        md = _format_as_markdown(content)

        assert "# Test Note" in md
        assert "## Summary" in md
        assert "Brief summary" in md
        assert "## Key Points" in md
        assert "- Point A" in md
        assert "## Action Items" in md
        assert "[ ] Do X" in md
        assert "[?] Ask Y" in md
        assert "## Analysis" in md
        assert "## Related Notes" in md

    def test_minimal_content(self):
        content = ResponseContent(note_title="Just Title")
        md = _format_as_markdown(content)
        assert md.startswith("# Just Title")

    def test_action_with_assignee_deadline(self):
        content = ResponseContent(
            note_title="N",
            action_items=[{"task": "Ship", "assignee": "bob", "deadline": "Friday"}],
        )
        md = _format_as_markdown(content)
        assert "@bob" in md
        assert "(due: Friday)" in md


# =====================
# should_auto_trigger
# =====================

class TestShouldAutoTrigger:
    def test_disabled(self):
        config = ResponseConfig(auto_trigger=False)
        assert not should_auto_trigger(config, "Q: anything?")

    def test_triggers_on_question_text(self):
        config = ResponseConfig(auto_trigger=True, trigger_on_questions=True)
        assert should_auto_trigger(config, "Q: What is X?")

    def test_triggers_on_blue_color(self):
        config = ResponseConfig(auto_trigger=True, trigger_on_questions=True)
        assert should_auto_trigger(config, "No text questions", has_color_questions=True)

    def test_triggers_on_actions(self):
        config = ResponseConfig(
            auto_trigger=True,
            trigger_on_questions=False,
            trigger_on_actions=True,
        )
        assert should_auto_trigger(config, "No questions", action_count=3)

    def test_no_trigger(self):
        config = ResponseConfig(auto_trigger=True, trigger_on_questions=True)
        assert not should_auto_trigger(config, "Just regular notes")


# =====================
# ResponseGenerator
# =====================

class TestResponseGenerator:
    @pytest.mark.asyncio
    async def test_generate_pdf_from_note(self, tmp_path):
        vault = ObsidianVault(tmp_path, {"_default": "Inbox"})
        note_path = tmp_path / "Inbox" / "test.md"
        vault.write_note(
            note_path,
            {"title": "Test Note", "summary": "Quick summary"},
            "# Test Note\n\nSome content here.\n\n- [ ] Do a thing",
        )

        config = ResponseConfig(format="pdf", include_analysis=False, trigger_on_questions=False)
        gen = ResponseGenerator(vault, config, anthropic_client=None)

        result = await gen.generate_from_note(note_path)

        assert result is not None
        assert result.format == "pdf"
        assert result.pdf_bytes is not None
        assert result.pdf_bytes[:5] == b"%PDF-"
        assert result.action_count == 1

    @pytest.mark.asyncio
    async def test_generate_notebook_from_note(self, tmp_path):
        vault = ObsidianVault(tmp_path, {"_default": "Inbox"})
        note_path = tmp_path / "Inbox" / "nb.md"
        vault.write_note(
            note_path,
            {"title": "NB Test"},
            "# NB\n\nContent",
        )

        config = ResponseConfig(format="notebook", include_analysis=False)
        gen = ResponseGenerator(vault, config, anthropic_client=None)

        result = await gen.generate_from_note(note_path)

        assert result is not None
        assert result.format == "notebook"
        assert result.notebook_files is not None
        assert any(k.endswith(".rm") for k in result.notebook_files)

    @pytest.mark.asyncio
    async def test_missing_note_returns_none(self, tmp_path):
        vault = ObsidianVault(tmp_path, {})
        config = ResponseConfig()
        gen = ResponseGenerator(vault, config, anthropic_client=None)

        result = await gen.generate_from_note(tmp_path / "nope.md")
        assert result is None

    @pytest.mark.asyncio
    async def test_qa_generation(self, tmp_path):
        import json
        vault = ObsidianVault(tmp_path, {"_default": "Inbox"})
        note_path = tmp_path / "Inbox" / "q.md"
        vault.write_note(
            note_path,
            {"title": "Q Note"},
            "Meeting notes.\n\nQ: What is the budget?",
        )

        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps([
            {"question": "What is the budget?", "answer": "$50k for Q2"},
        ]))]
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        config = ResponseConfig(
            format="pdf",
            trigger_on_questions=True,
            include_analysis=False,
        )
        gen = ResponseGenerator(vault, config, anthropic_client=mock_client)

        result = await gen.generate_from_note(note_path)

        assert result is not None
        assert result.question_count == 1

    @pytest.mark.asyncio
    async def test_qa_handles_code_block_response(self, tmp_path):
        import json
        vault = ObsidianVault(tmp_path, {"_default": "Inbox"})
        note_path = tmp_path / "Inbox" / "qcb.md"
        vault.write_note(
            note_path,
            {"title": "QCB"},
            "Q: Quick question?",
        )

        mock_client = AsyncMock()
        mock_response = MagicMock()
        wrapped = "```json\n" + json.dumps([{"question": "Quick question?", "answer": "Yes"}]) + "\n```"
        mock_response.content = [MagicMock(text=wrapped)]
        mock_client.messages.create = AsyncMock(return_value=mock_response)

        config = ResponseConfig(format="pdf", trigger_on_questions=True, include_analysis=False)
        gen = ResponseGenerator(vault, config, anthropic_client=mock_client)

        result = await gen.generate_from_note(note_path)
        assert result.question_count == 1


# =====================
# ResponseUploader notebook
# =====================

class TestResponseUploaderNotebook:
    @pytest.mark.asyncio
    async def test_upload_notebook_bundle(self):

        from src.remarkable.cloud import DocumentMetadata

        cloud = AsyncMock()
        cloud.list_items = AsyncMock(return_value=[
            DocumentMetadata(
                id="folder-1",
                name="Responses",
                parent="",
                doc_type="CollectionType",
                version=1,
                hash="",
                modified="",
            ),
        ])
        cloud.upload_document = AsyncMock(return_value="new-nb-id")

        uploader = ResponseUploader(cloud, response_folder="Responses")

        files = {
            "doc-id.metadata": b'{"type":"DocumentType"}',
            "doc-id.content": b'{"pages":[]}',
            "doc-id.pagedata": b"Blank\n",
            "doc-id/page.rm": b"fake rm bytes",
        }

        doc_id = await uploader.upload_notebook(files, "Test Notebook")

        assert doc_id == "new-nb-id"
        assert cloud.upload_document.called
