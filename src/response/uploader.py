"""Upload response documents to reMarkable Cloud.

Handles uploading generated PDFs and native notebooks, placing them
in the configured response folder on the tablet.
"""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path
from tempfile import NamedTemporaryFile

from src.remarkable.cloud import RemarkableCloud

logger = logging.getLogger(__name__)


class ResponseUploader:
    """Upload response documents (PDF or native notebook) to reMarkable Cloud."""

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

        with NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = Path(tmp.name)

        try:
            doc_id = await self._cloud.upload_document(tmp_path, parent_folder=folder_id)
            logger.info(
                "Uploaded PDF response '%s' (%s) to %s",
                title,
                doc_id[:8],
                self._response_folder,
            )
            return doc_id
        finally:
            tmp_path.unlink(missing_ok=True)

    async def upload_notebook(
        self,
        files: dict[str, bytes],
        title: str,
    ) -> str:
        """Upload a native reMarkable notebook bundle.

        The files dict should be the output of NotebookWriter.generate() —
        containing .metadata, .content, .pagedata, and .rm page files.

        The bundle is packaged as a zip archive and uploaded to the
        response folder on reMarkable.
        """
        folder_id = await self._ensure_folder()

        # Build a zip archive of the bundle
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, data in files.items():
                zf.writestr(name, data)
        bundle_bytes = buffer.getvalue()

        with NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
            tmp.write(bundle_bytes)
            tmp_path = Path(tmp.name)

        try:
            doc_id = await self._cloud.upload_document(tmp_path, parent_folder=folder_id)
            logger.info(
                "Uploaded notebook response '%s' (%s) to %s",
                title,
                doc_id[:8],
                self._response_folder,
            )
            return doc_id
        finally:
            tmp_path.unlink(missing_ok=True)

    async def _ensure_folder(self) -> str:
        """Get or create the response folder on reMarkable."""
        if self._folder_id:
            return self._folder_id

        items = await self._cloud.list_items()
        for item in items:
            if item.is_folder and item.name == self._response_folder:
                self._folder_id = item.id
                return self._folder_id

        self._folder_id = await self._cloud.create_folder(self._response_folder)
        logger.info("Created response folder '%s' on reMarkable", self._response_folder)
        return self._folder_id
