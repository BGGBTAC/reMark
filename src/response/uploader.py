"""Upload response documents to reMarkable Cloud.

Handles uploading generated PDFs and placing them in the
configured response folder on the tablet.
"""

from __future__ import annotations

import logging
from pathlib import Path
from tempfile import NamedTemporaryFile

from src.remarkable.cloud import RemarkableCloud

logger = logging.getLogger(__name__)


class ResponseUploader:
    """Upload response PDFs to reMarkable Cloud."""

    def __init__(self, cloud: RemarkableCloud, response_folder: str = "Responses"):
        self._cloud = cloud
        self._response_folder = response_folder
        self._folder_id: str | None = None

    async def upload_pdf(
        self,
        pdf_bytes: bytes,
        title: str,
    ) -> str:
        """Upload a PDF to the response folder on reMarkable.

        Creates the response folder if it doesn't exist.
        Returns the new document UUID.
        """
        folder_id = await self._ensure_folder()

        # Write PDF to a temp file for the upload API
        with NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = Path(tmp.name)

        try:
            doc_id = await self._cloud.upload_document(tmp_path, parent_folder=folder_id)
            logger.info("Uploaded response '%s' (%s) to %s", title, doc_id[:8], self._response_folder)
            return doc_id
        finally:
            tmp_path.unlink(missing_ok=True)

    async def _ensure_folder(self) -> str:
        """Get or create the response folder on reMarkable."""
        if self._folder_id:
            return self._folder_id

        # Check if folder already exists
        items = await self._cloud.list_items()
        for item in items:
            if item.is_folder and item.name == self._response_folder:
                self._folder_id = item.id
                return self._folder_id

        # Create it
        self._folder_id = await self._cloud.create_folder(self._response_folder)
        logger.info("Created response folder '%s' on reMarkable", self._response_folder)
        return self._folder_id
