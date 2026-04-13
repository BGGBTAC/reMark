"""Generate native reMarkable notebooks (.rm format) for responses.

Alternative to PDF — creates a notebook with typed text that
appears as editable content on the tablet. Uses rmscene to
generate valid v6 .rm files.
"""

from __future__ import annotations

import json
import logging
from io import BytesIO
from uuid import uuid4

from rmscene import simple_text_document, write_blocks

logger = logging.getLogger(__name__)


class NotebookWriter:
    """Generate reMarkable-native notebook files."""

    def generate(self, title: str, content: str) -> dict[str, bytes]:
        """Generate all files needed for a reMarkable notebook.

        Returns a dict of {filename: bytes} that can be uploaded
        as a document bundle.
        """
        doc_id = str(uuid4())
        page_id = str(uuid4())

        files: dict[str, bytes] = {}

        # .metadata
        metadata = {
            "deleted": False,
            "lastModified": "",
            "lastOpened": "",
            "lastOpenedPage": 0,
            "metadatamodified": False,
            "modified": True,
            "parent": "",
            "pinned": False,
            "synced": False,
            "type": "DocumentType",
            "version": 1,
            "visibleName": title,
        }
        files[f"{doc_id}.metadata"] = json.dumps(metadata).encode()

        # .content
        content_data = {
            "cPages": {
                "pages": [{"id": page_id}],
            },
            "fileType": "notebook",
        }
        files[f"{doc_id}.content"] = json.dumps(content_data).encode()

        # .rm file (the actual page content)
        rm_bytes = self._generate_rm_page(content)
        files[f"{doc_id}/{page_id}.rm"] = rm_bytes

        # .pagedata (page template)
        files[f"{doc_id}.pagedata"] = b"Blank\n"

        logger.info("Generated notebook '%s' (%s, 1 page)", title, doc_id[:8])
        return files

    def generate_multipage(
        self, title: str, pages: list[str]
    ) -> dict[str, bytes]:
        """Generate a multi-page notebook.

        Args:
            title: Document title.
            pages: List of text content, one per page.
        """
        doc_id = str(uuid4())
        page_ids = [str(uuid4()) for _ in pages]

        files: dict[str, bytes] = {}

        metadata = {
            "deleted": False,
            "lastModified": "",
            "type": "DocumentType",
            "version": 1,
            "visibleName": title,
        }
        files[f"{doc_id}.metadata"] = json.dumps(metadata).encode()

        content_data = {
            "cPages": {
                "pages": [{"id": pid} for pid in page_ids],
            },
            "fileType": "notebook",
        }
        files[f"{doc_id}.content"] = json.dumps(content_data).encode()

        pagedata_lines = ["Blank\n"] * len(pages)
        files[f"{doc_id}.pagedata"] = "".join(pagedata_lines).encode()

        for page_id, page_text in zip(page_ids, pages, strict=True):
            rm_bytes = self._generate_rm_page(page_text)
            files[f"{doc_id}/{page_id}.rm"] = rm_bytes

        logger.info(
            "Generated notebook '%s' (%s, %d pages)",
            title, doc_id[:8], len(pages),
        )
        return files

    def _generate_rm_page(self, text: str) -> bytes:
        """Generate a single .rm page with typed text content."""
        blocks = list(simple_text_document(text))
        buffer = BytesIO()
        write_blocks(buffer, blocks)
        return buffer.getvalue()
