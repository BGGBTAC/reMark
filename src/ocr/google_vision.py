"""OCR engine using Google Cloud Vision API.

Uses DOCUMENT_TEXT_DETECTION with handwriting hint for best results
on handwritten content. Falls back to TEXT_DETECTION for simpler pages.

Cost: first 1,000 units/month free, then ~$1.50/1,000 images.
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.ocr.pipeline import BoundingBox, OCREngine, OCRResult

logger = logging.getLogger(__name__)


class GoogleVisionOCR(OCREngine):
    """Google Cloud Vision handwriting recognition."""

    def __init__(self, credentials_path: str, language_hints: list[str] | None = None):
        self._credentials_path = Path(credentials_path).expanduser()
        self._language_hints = language_hints or ["en"]
        self._client = None

    @property
    def name(self) -> str:
        return "google_vision"

    def _get_client(self):
        """Lazy-init the Vision API client."""
        if self._client is None:
            import os
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(self._credentials_path)
            from google.cloud import vision
            self._client = vision.ImageAnnotatorClient()
        return self._client

    async def recognize_page(self, page_image: bytes) -> OCRResult:
        """Run document text detection on a page image.

        Uses DOCUMENT_TEXT_DETECTION which is optimized for dense text
        and handles handwriting better than plain TEXT_DETECTION.
        """
        import asyncio
        # google-cloud-vision is sync, run in executor
        return await asyncio.get_event_loop().run_in_executor(
            None, self._recognize_sync, page_image
        )

    def _recognize_sync(self, page_image: bytes) -> OCRResult:
        """Synchronous recognition (runs in thread pool)."""
        from google.cloud import vision

        client = self._get_client()

        image = vision.Image(content=page_image)

        # Use handwriting hint for better recognition
        hints = [f"en-t-i0-handwrit"] + self._language_hints
        context = vision.ImageContext(
            language_hints=hints,
        )

        response = client.document_text_detection(image=image, image_context=context)

        if response.error.message:
            raise RuntimeError(f"Google Vision API error: {response.error.message}")

        if not response.full_text_annotation:
            return OCRResult(text="", confidence=0.0, engine=self.name)

        full_text = response.full_text_annotation.text

        # Calculate average word-level confidence
        confidences = []
        boxes = []

        for page in response.full_text_annotation.pages:
            for block in page.blocks:
                for paragraph in block.paragraphs:
                    for word in paragraph.words:
                        word_text = "".join(
                            symbol.text for symbol in word.symbols
                        )
                        word_conf = word.confidence
                        confidences.append(word_conf)

                        # Extract bounding box
                        verts = word.bounding_box.vertices
                        if len(verts) >= 4:
                            x = min(v.x for v in verts)
                            y = min(v.y for v in verts)
                            w = max(v.x for v in verts) - x
                            h = max(v.y for v in verts) - y
                            boxes.append(BoundingBox(
                                x=x, y=y, width=w, height=h,
                                text=word_text, confidence=word_conf,
                            ))

        avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0

        logger.debug(
            "Google Vision: %d words, avg confidence %.2f",
            len(confidences), avg_confidence,
        )

        return OCRResult(
            text=full_text,
            confidence=avg_confidence,
            engine=self.name,
            word_boxes=boxes if boxes else None,
        )
