"""Tests for the OCR pipeline and engines."""

import json
from pathlib import Path

import pytest
from rmscene import simple_text_document, write_blocks

from src.config import OCRConfig
from src.ocr.pipeline import (
    OCREngine,
    OCRPipeline,
    OCRResult,
    PageText,
    _merge_texts,
)
from src.ocr.remarkable_builtin import RemarkableBuiltinOCR
from src.remarkable.formats import parse_rm_file

# -- Helpers --


def create_rm_fixture(path: Path, text: str) -> Path:
    blocks = list(simple_text_document(text))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        write_blocks(f, blocks)
    return path


class MockOCREngine(OCREngine):
    """Configurable mock OCR engine for testing."""

    def __init__(self, name: str, text: str, confidence: float):
        self._name = name
        self._text = text
        self._confidence = confidence
        self.call_count = 0

    @property
    def name(self) -> str:
        return self._name

    async def recognize_page(self, page_image: bytes) -> OCRResult:
        self.call_count += 1
        return OCRResult(text=self._text, confidence=self._confidence, engine=self._name)


class FailingOCREngine(OCREngine):
    """OCR engine that always raises."""

    @property
    def name(self) -> str:
        return "failing"

    async def recognize_page(self, page_image: bytes) -> OCRResult:
        raise RuntimeError("OCR engine exploded")


# -- _merge_texts --


class TestMergeTexts:
    def test_empty_typed(self):
        assert _merge_texts("", "OCR text here") == "OCR text here"

    def test_empty_ocr(self):
        assert _merge_texts("Typed text", "") == "Typed text"

    def test_both_empty(self):
        assert _merge_texts("", "") == ""

    def test_ocr_subset_of_typed(self):
        result = _merge_texts("Full typed text here", "typed text")
        assert result == "Full typed text here"

    def test_typed_subset_of_ocr(self):
        result = _merge_texts("part", "Full part with more text")
        assert result == "Full part with more text"

    def test_both_unique_merged(self):
        result = _merge_texts("Typed content", "Handwritten content")
        assert "Typed content" in result
        assert "Handwritten content" in result
        assert "---" in result


# -- OCRPipeline --


class TestOCRPipeline:
    @pytest.mark.asyncio
    async def test_crdt_text_only(self, tmp_path):
        """Pages with only CRDT text should return immediately without OCR."""
        doc_id = "doc-crdt"
        doc_dir = tmp_path / doc_id
        doc_dir.mkdir()

        create_rm_fixture(doc_dir / "page-1.rm", "Typed meeting notes")

        page = parse_rm_file(doc_dir / "page-1.rm")
        primary = MockOCREngine("mock_primary", "should not be called", 0.9)

        pipeline = OCRPipeline(OCRConfig(), primary=primary)
        results = await pipeline.recognize(
            pages=[page],
            doc_dir=doc_dir,
            doc_id=doc_id,
            page_ids=["page-1"],
        )

        assert len(results) == 1
        assert results[0].engine_used == "crdt"
        assert "Typed meeting notes" in results[0].text
        assert results[0].confidence == 1.0
        assert primary.call_count == 0  # should NOT call OCR

    @pytest.mark.asyncio
    async def test_builtin_conversion(self, tmp_path):
        """Built-in MyScript conversion should be used when available."""
        doc_id = "doc-builtin"
        doc_dir = tmp_path / doc_id
        doc_dir.mkdir()

        # No .rm file with text, but a text conversion exists
        create_rm_fixture(doc_dir / "page-1.rm", "")

        conv_dir = doc_dir / f"{doc_id}.textconversion"
        conv_dir.mkdir()
        (conv_dir / "page-1.json").write_text(json.dumps({"text": "MyScript converted text"}))

        page = parse_rm_file(doc_dir / "page-1.rm")
        pipeline = OCRPipeline(OCRConfig())

        results = await pipeline.recognize(
            pages=[page],
            doc_dir=doc_dir,
            doc_id=doc_id,
            page_ids=["page-1"],
        )

        assert len(results) == 1
        assert "MyScript converted text" in results[0].text
        assert results[0].engine_used == "builtin"

    @pytest.mark.asyncio
    async def test_empty_page_returns_empty(self, tmp_path):
        """Pages with no content should return empty results."""
        doc_id = "doc-empty"
        doc_dir = tmp_path / doc_id
        doc_dir.mkdir()

        create_rm_fixture(doc_dir / "page-1.rm", "")
        page = parse_rm_file(doc_dir / "page-1.rm")

        pipeline = OCRPipeline(OCRConfig())
        results = await pipeline.recognize(
            pages=[page],
            doc_dir=doc_dir,
            doc_id=doc_id,
            page_ids=["page-1"],
        )

        assert len(results) == 1
        assert results[0].text == ""
        assert results[0].confidence == 0.0

    @pytest.mark.asyncio
    async def test_multiple_pages(self, tmp_path):
        """Pipeline should process all pages."""
        doc_id = "doc-multi"
        doc_dir = tmp_path / doc_id
        doc_dir.mkdir()

        page_ids = ["p1", "p2", "p3"]
        for pid in page_ids:
            create_rm_fixture(doc_dir / f"{pid}.rm", f"Text on {pid}")

        pages = [parse_rm_file(doc_dir / f"{pid}.rm") for pid in page_ids]

        pipeline = OCRPipeline(OCRConfig())
        results = await pipeline.recognize(
            pages=pages,
            doc_dir=doc_dir,
            doc_id=doc_id,
            page_ids=page_ids,
        )

        assert len(results) == 3
        assert "Text on p1" in results[0].text
        assert "Text on p2" in results[1].text
        assert "Text on p3" in results[2].text


# -- RemarkableBuiltinOCR --


class TestRemarkableBuiltinOCR:
    def test_reads_text_format(self, tmp_path):
        conv_file = tmp_path / "page-1.json"
        conv_file.write_text(json.dumps({"text": "Handwritten text here"}))

        engine = RemarkableBuiltinOCR()
        result = engine.recognize_from_file(conv_file)

        assert result is not None
        assert result.text == "Handwritten text here"
        assert result.confidence == 1.0
        assert result.engine == "remarkable_builtin"

    def test_reads_paragraph_format(self, tmp_path):
        data = {"paragraphs": [{"text": "Para 1"}, {"text": "Para 2"}]}
        conv_file = tmp_path / "page-2.json"
        conv_file.write_text(json.dumps(data))

        engine = RemarkableBuiltinOCR()
        result = engine.recognize_from_file(conv_file)

        assert result is not None
        assert "Para 1" in result.text
        assert "Para 2" in result.text

    def test_missing_file_returns_none(self, tmp_path):
        engine = RemarkableBuiltinOCR()
        result = engine.recognize_from_file(tmp_path / "nope.json")
        assert result is None

    def test_empty_text_returns_none(self, tmp_path):
        conv_file = tmp_path / "empty.json"
        conv_file.write_text(json.dumps({"text": ""}))

        engine = RemarkableBuiltinOCR()
        result = engine.recognize_from_file(conv_file)
        assert result is None

    def test_malformed_json_returns_none(self, tmp_path):
        conv_file = tmp_path / "bad.json"
        conv_file.write_text("not json {{{")

        engine = RemarkableBuiltinOCR()
        result = engine.recognize_from_file(conv_file)
        assert result is None

    def test_word_boxes_extracted(self, tmp_path):
        data = {
            "text": "Hello World",
            "words": [
                {
                    "label": "Hello",
                    "confidence": 0.95,
                    "boundingBox": {"x": 10, "y": 20, "width": 50, "height": 15},
                },
                {
                    "label": "World",
                    "confidence": 0.92,
                    "boundingBox": {"x": 70, "y": 20, "width": 55, "height": 15},
                },
            ],
        }
        conv_file = tmp_path / "boxes.json"
        conv_file.write_text(json.dumps(data))

        engine = RemarkableBuiltinOCR()
        result = engine.recognize_from_file(conv_file)

        assert result is not None
        assert result.word_boxes is not None
        assert len(result.word_boxes) == 2
        assert result.word_boxes[0].text == "Hello"
        assert result.word_boxes[1].text == "World"


# -- PageText dataclass --


class TestPageText:
    def test_basic_creation(self):
        pt = PageText(
            page_id="p1",
            text="Some text",
            confidence=0.95,
            engine_used="google_vision",
            sources=["crdt", "google_vision"],
        )
        assert pt.page_id == "p1"
        assert pt.confidence == 0.95
        assert len(pt.sources) == 2
