"""OCR orchestrator with strategy pattern and fallback chain.

Tries extraction methods in order of cost/quality:
1. CRDT typed text (free, instant)
2. reMarkable built-in MyScript conversion (free, high quality)
3. Primary OCR engine (configurable)
4. Fallback OCR engine (if primary confidence is too low)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from src.config import AppConfig, OCRConfig
from src.remarkable.formats import (
    PageContent,
    extract_typed_text,
    get_builtin_text_conversion,
    render_page_to_png,
)

logger = logging.getLogger(__name__)


@dataclass
class OCRResult:
    """Result from a single OCR engine for one page."""

    text: str
    confidence: float  # 0.0 - 1.0
    engine: str
    word_boxes: list[BoundingBox] | None = None


@dataclass
class BoundingBox:
    """Bounding box for a recognized word."""

    x: float
    y: float
    width: float
    height: float
    text: str = ""
    confidence: float = 0.0


@dataclass
class PageText:
    """Final merged text for a single page after the full OCR pipeline."""

    page_id: str
    text: str
    confidence: float
    engine_used: str
    sources: list[str] = field(default_factory=list)  # e.g. ["crdt", "google_vision"]


class OCREngine(ABC):
    """Protocol for OCR engine implementations."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def recognize_page(self, page_image: bytes) -> OCRResult:
        """Recognize text from a rendered page image (PNG bytes)."""
        ...


class OCRPipeline:
    """Orchestrates text extraction with fallback chain.

    Extraction order:
    1. CRDT typed text (if available in .rm file)
    2. MyScript built-in conversion (if user triggered on tablet)
    3. Primary OCR engine on rendered image
    4. Fallback engine (if primary confidence < threshold)
    """

    def __init__(
        self,
        config: OCRConfig,
        primary: OCREngine | None = None,
        fallback: OCREngine | None = None,
    ):
        self._config = config
        self._primary = primary
        self._fallback = fallback
        self._threshold = config.confidence_threshold

    async def recognize(
        self,
        pages: list[PageContent],
        doc_dir: Path,
        doc_id: str,
        page_ids: list[str],
    ) -> list[PageText]:
        """Run the full OCR pipeline across all pages of a notebook.

        Args:
            pages: Parsed page content from formats.parse_notebook().
            doc_dir: Path to downloaded document directory.
            doc_id: Document UUID.
            page_ids: Ordered page UUIDs.
        """
        # Pre-fetch bulk data
        typed_text = extract_typed_text(doc_dir, doc_id, page_ids)
        builtin_text = get_builtin_text_conversion(doc_dir, doc_id) or {}

        results: list[PageText] = []

        for page, page_id in zip(pages, page_ids, strict=False):
            page_result = await self._process_page(
                page,
                page_id,
                doc_dir,
                doc_id,
                typed_text.get(page_id, ""),
                builtin_text.get(page_id, ""),
            )
            results.append(page_result)

        engines_used = {r.engine_used for r in results if r.text.strip()}
        avg_confidence = sum(r.confidence for r in results) / len(results) if results else 0
        logger.info(
            "OCR complete: %d pages, avg confidence %.2f, engines: %s",
            len(results),
            avg_confidence,
            engines_used,
        )

        return results

    async def _process_page(
        self,
        page: PageContent,
        page_id: str,
        doc_dir: Path,
        doc_id: str,
        typed_text: str,
        builtin_text: str,
    ) -> PageText:
        """Process a single page through the extraction pipeline."""
        sources: list[str] = []

        # Step 1: CRDT typed text (always best if available)
        if typed_text.strip():
            sources.append("crdt")
            # If there are no strokes, typed text is all we need
            if not page.has_strokes:
                return PageText(
                    page_id=page_id,
                    text=typed_text,
                    confidence=1.0,
                    engine_used="crdt",
                    sources=sources,
                )

        # Step 2: MyScript built-in conversion
        if builtin_text.strip():
            sources.append("builtin")
            merged = _merge_texts(typed_text, builtin_text)
            # Built-in conversion is high quality, trust it
            if not page.has_strokes or not self._primary:
                return PageText(
                    page_id=page_id,
                    text=merged,
                    confidence=1.0,
                    engine_used="builtin",
                    sources=sources,
                )

        # Step 3: Run primary OCR engine on rendered image
        if self._primary and page.has_strokes:
            image = render_page_to_png(doc_dir, doc_id, page_id)
            if image:
                try:
                    primary_result = await self._primary.recognize_page(image)
                    sources.append(self._primary.name)

                    # Good enough? Merge with typed text and return
                    if primary_result.confidence >= self._threshold:
                        merged = _merge_texts(typed_text, primary_result.text)
                        return PageText(
                            page_id=page_id,
                            text=merged,
                            confidence=primary_result.confidence,
                            engine_used=self._primary.name,
                            sources=sources,
                        )

                    # Step 4: Fallback if primary confidence is low
                    if self._fallback:
                        try:
                            fallback_result = await self._fallback.recognize_page(image)
                            sources.append(self._fallback.name)

                            # Pick whichever has higher confidence
                            best = (
                                fallback_result
                                if fallback_result.confidence > primary_result.confidence
                                else primary_result
                            )
                            merged = _merge_texts(typed_text, best.text)
                            return PageText(
                                page_id=page_id,
                                text=merged,
                                confidence=best.confidence,
                                engine_used=best.engine,
                                sources=sources,
                            )
                        except Exception as e:
                            logger.warning("Fallback OCR failed for page %s: %s", page_id[:8], e)

                    # Use primary result even if below threshold
                    merged = _merge_texts(typed_text, primary_result.text)
                    return PageText(
                        page_id=page_id,
                        text=merged,
                        confidence=primary_result.confidence,
                        engine_used=self._primary.name,
                        sources=sources,
                    )

                except Exception as e:
                    logger.warning("Primary OCR failed for page %s: %s", page_id[:8], e)

        # Nothing worked or no strokes — return whatever text we have
        text = typed_text or builtin_text or page.plain_text
        return PageText(
            page_id=page_id,
            text=text,
            confidence=1.0 if text.strip() else 0.0,
            engine_used=sources[0] if sources else "none",
            sources=sources,
        )


def build_pipeline(
    config: AppConfig,
    llm_client=None,
) -> OCRPipeline:
    """Construct an OCRPipeline from AppConfig.

    ``llm_client`` is optional — callers that already hold an LLMClient
    (e.g. SyncEngine) pass it here so VLMOcr reuses the same connection
    rather than building a second one.  When None, a client is built from
    config on demand (Anthropic if api key is set, etc.).
    """

    primary: OCREngine | None = None
    fallback: OCREngine | None = None

    def _make_engine(engine_name: str) -> OCREngine | None:
        if engine_name == "vlm":
            from src.ocr.vlm import VLMOcr

            client = llm_client
            if client is None:
                import os

                from src.llm.factory import build_llm_client

                client = build_llm_client(
                    config.llm,
                    anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
                )

            # When running Ollama, use the dedicated vision model rather than
            # whatever is set in ocr.vlm.model (which is an Anthropic model name).
            if config.llm.provider == "ollama":
                vlm_model = config.llm.ollama.vision_model
            else:
                vlm_model = config.ocr.vlm.model

            return VLMOcr(llm=client, model=vlm_model)

        if engine_name == "google_vision":
            from src.ocr.google_vision import GoogleVisionOCR

            return GoogleVisionOCR(config.ocr.google_vision)

        if engine_name == "remarkable_builtin":
            from src.ocr.remarkable_builtin import RemarkableBuiltinOCR

            return RemarkableBuiltinOCR()

        return None  # "none" or unknown

    primary = _make_engine(config.ocr.primary)
    fallback = _make_engine(config.ocr.fallback)

    return OCRPipeline(config.ocr, primary=primary, fallback=fallback)


def _merge_texts(typed: str, ocr: str) -> str:
    """Merge typed text (CRDT) with OCR text, deduplicating overlaps.

    Typed text is authoritative — if both sources cover the same content,
    prefer the typed version. OCR text fills in handwritten parts.
    """
    if not typed:
        return ocr
    if not ocr:
        return typed

    # Simple heuristic: if OCR text is a subset of typed text, skip it
    typed_stripped = typed.strip().lower()
    ocr_stripped = ocr.strip().lower()

    if ocr_stripped in typed_stripped:
        return typed

    if typed_stripped in ocr_stripped:
        return ocr

    # Both have unique content — combine with separator
    return f"{typed.strip()}\n\n---\n\n{ocr.strip()}"
