"""Tests for response PDF generation, notebook writing, and uploading."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.response.notebook_writer import NotebookWriter
from src.response.pdf_generator import ResponseContent, ResponsePDFGenerator, _escape
from src.response.uploader import ResponseUploader

# =====================
# ResponsePDFGenerator
# =====================


class TestResponsePDFGenerator:
    def test_generate_basic_pdf(self):
        gen = ResponsePDFGenerator()
        content = ResponseContent(
            note_title="Weekly Standup",
            summary="Team discussed Q2 goals and timeline.",
        )

        pdf_bytes = gen.generate(content)

        assert isinstance(pdf_bytes, bytes)
        assert len(pdf_bytes) > 100
        assert pdf_bytes[:5] == b"%PDF-"

    def test_generate_full_content(self):
        gen = ResponsePDFGenerator()
        content = ResponseContent(
            note_title="Project Review",
            summary="Reviewed architecture decisions for the backend rewrite.",
            key_points=["Migrate to async", "Replace ORM with raw SQL", "Add caching layer"],
            action_items=[
                {
                    "task": "Write migration plan",
                    "priority": "high",
                    "assignee": "Alice",
                    "deadline": "2026-04-20",
                },
                {"task": "What about the legacy API?", "type": "question", "priority": "medium"},
                {"task": "Set up staging", "priority": "low"},
            ],
            analysis="The current architecture has scaling issues at >10k req/s.\n\nMoving to async should resolve the bottleneck.",
            related_notes=["Sprint Planning 2026-04", "Architecture Decision Records"],
        )

        pdf_bytes = gen.generate(content)
        assert pdf_bytes[:5] == b"%PDF-"
        assert len(pdf_bytes) > 500

    def test_generate_empty_content(self):
        gen = ResponsePDFGenerator()
        content = ResponseContent(note_title="Empty Note")

        pdf_bytes = gen.generate(content)
        assert pdf_bytes[:5] == b"%PDF-"

    def test_generate_special_chars(self):
        gen = ResponsePDFGenerator()
        content = ResponseContent(
            note_title='Test <with> & special "chars"',
            summary="Content with <html> & ampersands",
        )

        # Should not raise
        pdf_bytes = gen.generate(content)
        assert pdf_bytes[:5] == b"%PDF-"


class TestEscape:
    def test_ampersand(self):
        assert _escape("A & B") == "A &amp; B"

    def test_angle_brackets(self):
        assert _escape("<tag>") == "&lt;tag&gt;"

    def test_no_special(self):
        assert _escape("plain text") == "plain text"

    def test_all_together(self):
        assert _escape("a < b & c > d") == "a &lt; b &amp; c &gt; d"


# =====================
# NotebookWriter
# =====================


class TestNotebookWriter:
    def test_generate_single_page(self):
        writer = NotebookWriter()
        files = writer.generate("Test Note", "Hello from reMark")

        # Should have .metadata, .content, .pagedata, and a .rm file
        extensions = {Path(k).suffix for k in files}
        assert ".metadata" in extensions
        assert ".content" in extensions
        assert ".rm" in extensions
        assert ".pagedata" in extensions

        # .rm file should be valid rmscene data
        rm_files = [v for k, v in files.items() if k.endswith(".rm")]
        assert len(rm_files) == 1
        assert rm_files[0][:43] == b"reMarkable .lines file, version=6          "

    def test_generate_multipage(self):
        writer = NotebookWriter()
        pages = ["Page one content", "Page two content", "Page three"]
        files = writer.generate_multipage("Multi Note", pages)

        rm_files = [k for k in files if k.endswith(".rm")]
        assert len(rm_files) == 3

    def test_metadata_has_title(self):
        import json

        writer = NotebookWriter()
        files = writer.generate("My Title", "Content")

        meta_files = {k: v for k, v in files.items() if k.endswith(".metadata")}
        assert len(meta_files) == 1

        meta = json.loads(list(meta_files.values())[0])
        assert meta["visibleName"] == "My Title"
        assert meta["type"] == "DocumentType"

    def test_content_has_page_ids(self):
        import json

        writer = NotebookWriter()
        files = writer.generate("Test", "Content")

        content_files = {k: v for k, v in files.items() if k.endswith(".content")}
        content = json.loads(list(content_files.values())[0])

        pages = content["cPages"]["pages"]
        assert len(pages) == 1
        assert "id" in pages[0]


# =====================
# ResponseUploader
# =====================


class TestResponseUploader:
    @pytest.mark.asyncio
    async def test_upload_creates_folder_if_missing(self):

        cloud = AsyncMock()
        cloud.list_items = AsyncMock(return_value=[])  # no existing folders
        cloud.create_folder = AsyncMock(return_value="new-folder-id")
        cloud.upload_document = AsyncMock(return_value="new-doc-id")

        uploader = ResponseUploader(cloud, response_folder="Responses")
        doc_id = await uploader.upload_pdf(b"%PDF-fake", "Test Response")

        assert doc_id == "new-doc-id"
        cloud.create_folder.assert_called_once_with("Responses")
        cloud.upload_document.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_reuses_existing_folder(self):
        from src.remarkable.cloud import DocumentMetadata

        cloud = AsyncMock()
        cloud.list_items = AsyncMock(
            return_value=[
                DocumentMetadata(
                    id="existing-folder",
                    name="Responses",
                    parent="",
                    doc_type="CollectionType",
                    version=1,
                    hash="",
                    modified="",
                ),
            ]
        )
        cloud.upload_document = AsyncMock(return_value="new-doc-id")

        uploader = ResponseUploader(cloud, response_folder="Responses")
        await uploader.upload_pdf(b"%PDF-fake", "Test")

        cloud.create_folder.assert_not_called()

    @pytest.mark.asyncio
    async def test_upload_caches_folder_id(self):
        cloud = AsyncMock()
        cloud.list_items = AsyncMock(return_value=[])
        cloud.create_folder = AsyncMock(return_value="folder-id")
        cloud.upload_document = AsyncMock(return_value="doc-id")

        uploader = ResponseUploader(cloud, "Responses")

        await uploader.upload_pdf(b"%PDF-1", "First")
        await uploader.upload_pdf(b"%PDF-2", "Second")

        # Should only create folder once
        cloud.create_folder.assert_called_once()
