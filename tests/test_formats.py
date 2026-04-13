"""Tests for .rm file format parsing and document handling."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from rmscene import simple_text_document, write_blocks
from rmscene.scene_items import ParagraphStyle, PenColor

from src.remarkable.documents import DocumentManager
from src.remarkable.formats import (
    COLOR_INDEX_MAP,
    INDEX_COLOR_MAP,
    Notebook,
    PageContent,
    TextBlock,
    extract_typed_text,
    get_builtin_text_conversion,
    parse_notebook,
    parse_rm_file,
)

FIXTURES = Path(__file__).parent / "fixtures"


# -- Helpers --

def create_rm_fixture(path: Path, text: str) -> Path:
    """Create a .rm file with the given text content."""
    blocks = list(simple_text_document(text))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        write_blocks(f, blocks)
    return path


def create_content_file(doc_dir: Path, doc_id: str, page_ids: list[str]) -> None:
    """Create a .content JSON file for a document."""
    content = {"cPages": {"pages": [{"id": pid} for pid in page_ids]}}
    (doc_dir / f"{doc_id}.content").write_text(json.dumps(content))


def create_metadata_file(doc_dir: Path, doc_id: str, name: str) -> None:
    """Create a .metadata JSON file for a document."""
    meta = {"visibleName": name, "type": "DocumentType"}
    (doc_dir / f"{doc_id}.metadata").write_text(json.dumps(meta))


# -- parse_rm_file --

class TestParseRmFile:
    def test_parse_text_content(self):
        page = parse_rm_file(FIXTURES / "test-page-001.rm")

        assert page.has_text
        assert not page.has_strokes
        assert len(page.text_blocks) == 1
        assert "Meeting Notes" in page.plain_text
        assert "TODO: send report to team" in page.plain_text

    def test_parse_text_preserves_lines(self):
        page = parse_rm_file(FIXTURES / "test-page-001.rm")
        lines = page.plain_text.split("\n")
        assert len(lines) == 4
        assert lines[0] == "Meeting Notes"
        assert lines[3] == "Q: What is the deadline?"

    def test_parse_generated_fixture(self, tmp_path):
        rm_path = create_rm_fixture(
            tmp_path / "page-abc.rm",
            "Hello World\nThis is a test",
        )
        page = parse_rm_file(rm_path)

        assert page.page_id == "page-abc"
        assert page.has_text
        assert "Hello World" in page.plain_text
        assert "This is a test" in page.plain_text

    def test_parse_empty_text(self, tmp_path):
        rm_path = create_rm_fixture(tmp_path / "empty.rm", "")
        page = parse_rm_file(rm_path)
        assert not page.has_text
        assert page.plain_text == ""

    def test_page_id_from_filename(self):
        page = parse_rm_file(FIXTURES / "test-page-001.rm")
        assert page.page_id == "test-page-001"


# -- parse_notebook --

class TestParseNotebook:
    def test_parse_multipage_notebook(self, tmp_path):
        doc_id = "doc-123"
        doc_dir = tmp_path / doc_id
        doc_dir.mkdir()

        page_ids = ["page-a", "page-b", "page-c"]

        # Create .rm files for each page
        for i, pid in enumerate(page_ids):
            create_rm_fixture(
                doc_dir / f"{pid}.rm",
                f"Page {i + 1} content\nSome notes here",
            )

        pages = parse_notebook(doc_dir, doc_id, page_ids)

        assert len(pages) == 3
        assert "Page 1 content" in pages[0].plain_text
        assert "Page 2 content" in pages[1].plain_text
        assert "Page 3 content" in pages[2].plain_text

    def test_missing_page_returns_empty(self, tmp_path):
        doc_dir = tmp_path / "doc-456"
        doc_dir.mkdir()

        pages = parse_notebook(doc_dir, "doc-456", ["nonexistent-page"])

        assert len(pages) == 1
        assert not pages[0].has_text
        assert not pages[0].has_strokes


# -- extract_typed_text --

class TestExtractTypedText:
    def test_extracts_text_from_pages(self, tmp_path):
        doc_id = "doc-text"
        doc_dir = tmp_path / doc_id
        doc_dir.mkdir()

        page_ids = ["p1", "p2"]
        create_rm_fixture(doc_dir / "p1.rm", "First page text")
        create_rm_fixture(doc_dir / "p2.rm", "Second page text")

        result = extract_typed_text(doc_dir, doc_id, page_ids)

        assert "p1" in result
        assert "p2" in result
        assert result["p1"] == "First page text"
        assert result["p2"] == "Second page text"

    def test_skips_empty_pages(self, tmp_path):
        doc_id = "doc-empty"
        doc_dir = tmp_path / doc_id
        doc_dir.mkdir()

        create_rm_fixture(doc_dir / "p1.rm", "Has text")
        create_rm_fixture(doc_dir / "p2.rm", "")

        result = extract_typed_text(doc_dir, doc_id, ["p1", "p2"])

        assert "p1" in result
        assert "p2" not in result


# -- get_builtin_text_conversion --

class TestBuiltinTextConversion:
    def test_reads_text_conversion(self, tmp_path):
        doc_id = "doc-conv"
        conv_dir = tmp_path / f"{doc_id}.textconversion"
        conv_dir.mkdir()

        (conv_dir / "page-1.json").write_text(json.dumps({"text": "Converted text here"}))
        (conv_dir / "page-2.json").write_text(json.dumps({"text": "More converted text"}))

        result = get_builtin_text_conversion(tmp_path, doc_id)

        assert result is not None
        assert result["page-1"] == "Converted text here"
        assert result["page-2"] == "More converted text"

    def test_reads_paragraph_format(self, tmp_path):
        doc_id = "doc-para"
        conv_dir = tmp_path / f"{doc_id}.textconversion"
        conv_dir.mkdir()

        data = {"paragraphs": [{"text": "First paragraph"}, {"text": "Second paragraph"}]}
        (conv_dir / "page-1.json").write_text(json.dumps(data))

        result = get_builtin_text_conversion(tmp_path, doc_id)

        assert result is not None
        assert "First paragraph" in result["page-1"]
        assert "Second paragraph" in result["page-1"]

    def test_returns_none_when_no_conversion(self, tmp_path):
        result = get_builtin_text_conversion(tmp_path, "no-such-doc")
        assert result is None

    def test_skips_empty_text(self, tmp_path):
        doc_id = "doc-empty"
        conv_dir = tmp_path / f"{doc_id}.textconversion"
        conv_dir.mkdir()

        (conv_dir / "page-1.json").write_text(json.dumps({"text": ""}))

        result = get_builtin_text_conversion(tmp_path, doc_id)
        assert result is None


# -- Color mapping --

class TestColorMapping:
    def test_color_index_map_covers_basic_colors(self):
        assert COLOR_INDEX_MAP[PenColor.BLACK] == 0
        assert COLOR_INDEX_MAP[PenColor.RED] == 6
        assert COLOR_INDEX_MAP[PenColor.BLUE] == 5
        assert COLOR_INDEX_MAP[PenColor.YELLOW] == 3

    def test_index_color_roundtrip(self):
        for color, index in COLOR_INDEX_MAP.items():
            assert INDEX_COLOR_MAP[index] == color


# -- TextBlock --

class TestTextBlock:
    def test_plain_text(self):
        block = TextBlock(text="plain text")
        assert block.to_markdown() == "plain text"

    def test_heading(self):
        block = TextBlock(text="Title", style=ParagraphStyle.HEADING)
        assert block.to_markdown() == "# Title"

    def test_bold(self):
        block = TextBlock(text="important", style=ParagraphStyle.BOLD)
        assert block.to_markdown() == "**important**"

    def test_bullet(self):
        block = TextBlock(text="item one", style=ParagraphStyle.BULLET)
        assert block.to_markdown() == "- item one"

    def test_checkbox(self):
        block = TextBlock(text="task", style=ParagraphStyle.CHECKBOX)
        assert block.to_markdown() == "- [ ] task"

    def test_checkbox_checked(self):
        block = TextBlock(text="done", style=ParagraphStyle.CHECKBOX_CHECKED)
        assert block.to_markdown() == "- [x] done"


# -- DocumentManager --

class TestDocumentManager:
    @pytest.mark.asyncio
    async def test_list_documents_filters_by_folder(self, tmp_path):
        from src.remarkable.cloud import DocumentMetadata

        mock_cloud = AsyncMock()
        mock_cloud.list_items = AsyncMock(return_value=[
            DocumentMetadata(id="f1", name="Work", parent="", doc_type="CollectionType", version=1, hash="a", modified=""),
            DocumentMetadata(id="f2", name="Trash", parent="", doc_type="CollectionType", version=1, hash="b", modified=""),
            DocumentMetadata(id="d1", name="Note 1", parent="f1", doc_type="DocumentType", version=1, hash="c", modified=""),
            DocumentMetadata(id="d2", name="Note 2", parent="f2", doc_type="DocumentType", version=1, hash="d", modified=""),
            DocumentMetadata(id="d3", name="Note 3", parent="f1", doc_type="DocumentType", version=1, hash="e", modified=""),
        ])

        mgr = DocumentManager(mock_cloud, tmp_path)
        docs = await mgr.list_documents(sync_folders=["Work"], ignore_folders=["Trash"])

        names = [d.name for d in docs]
        assert "Note 1" in names
        assert "Note 3" in names
        assert "Note 2" not in names

    def test_read_page_ids_cpages_format(self, tmp_path):

        doc_id = "doc-pages"
        doc_dir = tmp_path / doc_id
        doc_dir.mkdir()

        content = {"cPages": {"pages": [{"id": "p1"}, {"id": "p2"}, {"id": "p3"}]}}
        (doc_dir / f"{doc_id}.content").write_text(json.dumps(content))

        mgr = DocumentManager(MagicMock(), tmp_path)
        page_ids = mgr._read_page_ids(doc_dir, doc_id)

        assert page_ids == ["p1", "p2", "p3"]

    def test_read_page_ids_legacy_format(self, tmp_path):
        doc_id = "doc-legacy"
        doc_dir = tmp_path / doc_id
        doc_dir.mkdir()

        content = {"pages": ["page-a", "page-b"]}
        (doc_dir / f"{doc_id}.content").write_text(json.dumps(content))

        mgr = DocumentManager(MagicMock(), tmp_path)
        page_ids = mgr._read_page_ids(doc_dir, doc_id)

        assert page_ids == ["page-a", "page-b"]

    def test_read_doc_name(self, tmp_path):
        doc_id = "doc-meta"
        doc_dir = tmp_path / doc_id
        doc_dir.mkdir()

        meta = {"visibleName": "My Important Notes"}
        (doc_dir / f"{doc_id}.metadata").write_text(json.dumps(meta))

        mgr = DocumentManager(MagicMock(), tmp_path)
        name = mgr._read_doc_name(doc_dir, doc_id)

        assert name == "My Important Notes"

    def test_read_doc_name_missing(self, tmp_path):
        mgr = DocumentManager(MagicMock(), tmp_path)
        name = mgr._read_doc_name(tmp_path / "no-dir", "no-doc")
        assert name is None


# -- Notebook dataclass --

class TestNotebook:
    def test_all_text_concatenation(self):
        pages = [
            PageContent(page_id="p1", text_blocks=[TextBlock(text="Page one")]),
            PageContent(page_id="p2"),  # empty page
            PageContent(page_id="p3", text_blocks=[TextBlock(text="Page three")]),
        ]

        nb = Notebook(id="nb1", name="Test", folder="", modified="", pages=pages)

        assert nb.page_count == 3
        assert "Page one" in nb.all_text
        assert "Page three" in nb.all_text

    def test_empty_notebook(self):
        nb = Notebook(id="nb2", name="Empty", folder="", modified="", pages=[])
        assert nb.page_count == 0
        assert nb.all_text == ""
